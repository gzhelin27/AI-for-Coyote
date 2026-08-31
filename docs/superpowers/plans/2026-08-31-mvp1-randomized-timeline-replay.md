# MVP1 Randomized Timeline and Exact Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-plot-event waveform/strength variation, independent A/B raw-cycle gap scheduling, completed-session archives, and exact replay to the existing automatic mode.

**Architecture:** A plot event resolves waveform and `base ±4` strength once. Independent `ChannelCycleRunner` workers repeatedly send one complete raw waveform frame sequence through `GameLoop.execute_actions()`, sample and record a cycle-relative gap, and apply normal changes only at cycle boundaries; stop-class actions remain immediate. Exact replay consumes recorded cycle starts and gap decisions without sampling again.

**Tech Stack:** Python 3.12, stdlib `dataclasses`/`enum`/`random`/`asyncio`/`zipfile`/`hashlib`, FastAPI, React 19, TypeScript, Zustand, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

## Global Constraints

- Plot/source intent precedes randomness; safety overrides both.
- All output goes through `GameLoop.execute_actions()` and `SafetyManager.validate()`.
- Strength is sampled once per plot event as an integer in `base_strength ±4`, then clipped to the effective cap.
- One raw waveform cycle is the complete preset frame sequence; each frame is 100 ms.
- After each completed cycle, sample per channel: 40% `0.0`, 30% uniformly `0.1..1.0`, 30% uniformly `1.1..2.0`, in 0.1-cycle steps.
- A/B random streams are deterministic and independent.
- Pattern and strength remain fixed within one plot event; only the cycle gap is resampled.
- Normal changes wait for the current cycle boundary. A new event during a gap ends that gap immediately. Stop, pause, disconnect, and estop are immediate.
- Existing AI/autopilot turn timing is unchanged; cycle completion never triggers an LLM call.
- Generated gaps retain strength but send no waveform frames.
- Manual waveform test and manual continuous controls keep existing behavior.
- MVP1 has one project-wide policy with no UI or DLC override.
- Runtime cap 40 remains ignored local state; committed code never raises it.
- Manual pause duration is omitted from replay time; generated cycle gaps are retained.
- Runtime data stays under ignored `data/`; never commit keys, DLC private data, imported content, or replays.
- Do not run `tests/probe_llm.py` in automated verification.

---

## File Structure

- Create `backend/timeline/models.py`: project-wide profile, plot events, cycle records, timelines, manifests, session state.
- Create `backend/timeline/randomizer.py`: stable channel seed derivation plus plot-event waveform/strength resolution.
- Create `backend/timeline/cycle_runner.py`: one per-channel boundary-aware cycle worker.
- Create `backend/timeline/replay_store.py`: secure `.coyote-replay` persistence.
- Create `backend/timeline/player.py`: recorded-cycle replay.
- Create `backend/timeline/session.py`: live lifecycle, recording, and replay orchestration.
- Create `backend/timeline/__init__.py`: public timeline interfaces.
- Create `tests/timeline_fakes.py`: controlled sleepers, fake executors/relays, and reusable cycle/session/replay harnesses.
- Modify `backend/safety.py`: validate a single-cycle waveform action.
- Modify `backend/game_loop.py`: execute a raw cycle, expose non-estop clear, and attach timeline session without changing autopilot timing.
- Modify `backend/main.py`: service construction, APIs, and state broadcast.
- Modify `backend/config.py`, `config/config.example.yaml`, and `.gitignore`: fixed policy and runtime storage.
- Create `frontend/src/components/ReplayPanel.tsx`; modify the existing app/API/state/control files.
- Add focused tests under `tests/`.

### Task 1: Timeline domain model and project-wide cycle-gap policy

**Files:**
- Create: `backend/timeline/__init__.py`
- Create: `backend/timeline/models.py`
- Test: `tests/test_timeline_models.py`
- Modify: `backend/config.py:22-106`
- Modify: `config/config.example.yaml`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `DirectiveMode`, `ChannelDirective`, `CycleGapPolicy`, `PlotEvent`, `CycleRecord`, `Timeline`, `ReplayManifest`, `SessionStatus`, `SessionState`.
- Produces: `CycleGapPolicy.sample_tenths(rng: random.Random) -> int` and `gap_ms(frame_count: int, tenths: int) -> int`.

- [ ] **Step 1: Write failing model and probability-boundary tests**

```python
import unittest

from backend.timeline.models import (
    ChannelDirective, CycleGapPolicy, CycleRecord, DirectiveMode, PlotEvent, Timeline,
)


class FixedRng:
    def __init__(self, rolls: list[int], values: list[int]):
        self.rolls = iter(rolls)
        self.values = iter(values)

    def randrange(self, stop: int) -> int:
        self.asserted_stop = stop
        return next(self.rolls)

    def randint(self, low: int, high: int) -> int:
        value = next(self.values)
        if not low <= value <= high:
            raise AssertionError((low, value, high))
        return value


class TimelineModelTests(unittest.TestCase):
    def test_gap_policy_uses_exact_three_bands(self):
        policy = CycleGapPolicy()
        rng = FixedRng([0, 39, 40, 69, 70, 99], [1, 10, 11, 20])
        self.assertEqual([policy.sample_tenths(rng) for _ in range(6)], [0, 0, 1, 10, 11, 20])

    def test_gap_duration_uses_raw_frame_count(self):
        policy = CycleGapPolicy()
        self.assertEqual(policy.cycle_ms(frame_count=12), 1200)
        self.assertEqual(policy.gap_ms(frame_count=12, tenths=7), 840)

    def test_timeline_round_trip_preserves_plot_events_and_cycles(self):
        event = PlotEvent(
            event_id="evt-000001", scene_id="live-turn-1", offset_ms=0,
            channels={"A": ChannelDirective(
                channel="A", mode=DirectiveMode.SET, pattern="呼吸",
                base_strength=20, resolved_strength=24,
            )},
        )
        cycle = CycleRecord(
            channel="A", cycle_index=1, plot_event_id="evt-000001",
            pattern="呼吸", waveform_hash="wave-hash", requested_strength=24,
            effective_strength=24, active_start_offset_ms=0, raw_duration_ms=1200,
            gap_tenths=7, planned_gap_ms=840, actual_gap_ms=840,
            completed=True, interruption_reason=None,
        )
        timeline = Timeline(schema_version=1, session_id="session-1", seed=7,
                            plot_events=(event,), cycles=(cycle,))
        self.assertEqual(Timeline.from_dict(timeline.to_dict()), timeline)
```

- [ ] **Step 2: Run the focused test and verify missing module failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_models -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.timeline'`.

- [ ] **Step 3: Implement immutable policy and serializable records**

```python
@dataclass(frozen=True)
class CycleGapPolicy:
    zero_weight: int = 40
    short_weight: int = 30
    long_weight: int = 30
    frame_ms: int = 100

    def __post_init__(self) -> None:
        weights = (self.zero_weight, self.short_weight, self.long_weight)
        if any(weight < 0 for weight in weights) or sum(weights) != 100:
            raise ValueError("cycle-gap weights must be non-negative and total 100")
        if self.frame_ms != 100:
            raise ValueError("DG-LAB waveform frames must remain 100 ms")

    def sample_tenths(self, rng: random.Random) -> int:
        roll = rng.randrange(100)
        if roll < self.zero_weight:
            return 0
        if roll < self.zero_weight + self.short_weight:
            return rng.randint(1, 10)
        return rng.randint(11, 20)

    def cycle_ms(self, frame_count: int) -> int:
        if frame_count <= 0:
            raise ValueError("frame_count must be positive")
        return frame_count * self.frame_ms

    def gap_ms(self, frame_count: int, tenths: int) -> int:
        if not 0 <= tenths <= 20:
            raise ValueError("gap tenths must be in 0..20")
        return self.cycle_ms(frame_count) * tenths // 10
```

Define `ChannelDirective(channel, mode, pattern=None, base_strength=None, resolved_strength=None)` and explicit `to_dict()`/`from_dict()` readers with schema version and range checks. `CycleRecord` contains channel, cycle index, plot event ID, pattern, waveform-data hash, requested/effective strength, active start offset, raw duration, gap tenths, planned/actual gap milliseconds, completion flag, and interruption reason.

- [ ] **Step 4: Add fixed configuration and ignored runtime directory**

```yaml
timeline:
  strength_jitter: 4
  waveform_policy: all_allowed
  cycle_gap:
    zero_weight: 40
    short_weight: 30
    long_weight: 30
    frame_ms: 100
  replay_dir: data/replays
```

Add `data/` to `.gitignore`. Config validation accepts one project-wide weight set only when all weights are non-negative and total 100; it rejects any `frame_ms` other than 100. There is no per-DLC or runtime UI override.

- [ ] **Step 5: Run tests and config smoke check**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_models -v`

Run: `.venv\Scripts\python.exe -c "from backend.config import load_config; from backend.timeline.models import CycleGapPolicy; print(CycleGapPolicy(**load_config()['timeline']['cycle_gap']))"`

Expected: tests PASS and smoke output shows `40/30/30` plus `frame_ms=100`.

- [ ] **Step 6: Commit**

```bash
git add .gitignore backend/timeline backend/config.py config/config.example.yaml tests/test_timeline_models.py
git commit -m "feat: define cycle-aware timeline domain"
```

### Task 2: Deterministic plot-event resolver and independent channel seeds

**Files:**
- Create: `backend/timeline/randomizer.py`
- Test: `tests/test_timeline_randomizer.py`

**Interfaces:**
- Consumes: session seed, AI actions, current strength, caps, enabled channels, and allowed presets.
- Produces: `derive_stream_seed(session_seed: int, stream: str) -> int` and `TimelineResolver.resolve_plot_event(actions, current, caps, enabled, presets, event_id, scene_id, offset_ms) -> PlotEvent`.

- [ ] **Step 1: Write failing deterministic resolution tests**

```python
class TimelineResolverTests(unittest.TestCase):
    def setUp(self):
        self.event_args = {
            "actions": [{"op": "hold_strength", "channel": "A", "value": 20}],
            "current": {"A": 0, "B": 0}, "caps": {"A": 40, "B": 40},
            "enabled": {"A": True, "B": True}, "presets": ("呼吸", "潮汐"),
            "event_id": "evt-000001", "scene_id": "live-turn-1", "offset_ms": 0,
        }

    def resolver(self, seed: int):
        return TimelineResolver(strength_jitter=4, session_seed=seed)

    def test_stream_seed_is_stable_and_distinct(self):
        self.assertEqual(derive_stream_seed(99, "plot:A"), derive_stream_seed(99, "plot:A"))
        seeds = {derive_stream_seed(99, name) for name in ("plot:A", "plot:B", "cycle:A", "cycle:B")}
        self.assertEqual(len(seeds), 4)

    def test_same_seed_resolves_same_waveform_and_strength(self):
        first = self.resolver(7).resolve_plot_event(**self.event_args)
        second = self.resolver(7).resolve_plot_event(**self.event_args)
        self.assertEqual(first, second)

    def test_strength_stays_within_jitter_and_cap(self):
        event = self.resolver(8).resolve_plot_event(
            actions=[{"op": "hold_strength", "channel": "A", "value": 39}],
            current={"A": 0, "B": 0}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸", "潮汐"),
            event_id="evt-000002", scene_id="live-turn-2", offset_ms=12000,
        )
        self.assertGreaterEqual(event.channels["A"].strength, 35)
        self.assertLessEqual(event.channels["A"].strength, 40)

    def test_stop_is_never_randomized_into_output(self):
        event = self.resolver(9).resolve_plot_event(
            actions=[{"op": "clear", "channel": "B"}],
            current={"A": 10, "B": 10}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸",),
            event_id="evt-000003", scene_id="live-turn-3", offset_ms=24000,
        )
        self.assertEqual(event.channels["B"].mode, DirectiveMode.STOP)
```

- [ ] **Step 2: Run and verify missing resolver failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_randomizer -v`

- [ ] **Step 3: Implement stable seeds and one-time event resolution**

```python
def derive_stream_seed(session_seed: int, stream: str) -> int:
    if stream not in {"plot:A", "plot:B", "cycle:A", "cycle:B"}:
        raise ValueError("unsupported random stream")
    digest = hashlib.sha256(f"{session_seed}:{stream}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)
```

Construct resolver RNGs from `plot:A` and `plot:B`. Normalize `add_strength`, `hold_strength`, and `temp_strength` to base targets. For each enabled set channel, choose one allowed preset and one jittered strength. Preserve `clear/stop` exactly. Do not sample a cycle gap here and do not alter the autopilot interval.

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_randomizer -v`

```bash
git add backend/timeline/randomizer.py tests/test_timeline_randomizer.py
git commit -m "feat: resolve deterministic plot events"
```

### Task 3: Independent boundary-aware channel cycle runner

**Files:**
- Create: `backend/timeline/cycle_runner.py`
- Create: `tests/timeline_fakes.py`
- Test: `tests/test_cycle_runner.py`

**Interfaces:**
- Consumes: channel, `CycleGapPolicy`, channel RNG, async action executor, monotonic clock, interruptible sleeper, and cycle callback.
- Produces: `ChannelCycleRunner.submit(directive)`, `pause()`, `resume()`, `stop()`, `wait_stopped()`, `state()`, and emitted `CycleRecord` values.

- [ ] **Step 1: Write failing state-machine tests with controllable time**

In `tests/timeline_fakes.py`, define `ControlledSleeper` with `sleep(ms)`, `advance(ms)`, and cancellation tracking; `FakeCycleExecutor` with `execute(actions)`, `sent_cycles`, `sent_patterns`, `clear_calls`, and injectable failure; `CycleHarness` that wires those fakes to a runner and exposes `complete_cycle()`, `enter_gap()`, and `flush()` without wall-clock sleep; and `make_replay_bundle(gap_tenths, status)` for archive tests. Later tasks extend this file with `SessionHarness`, `ReplayHarness`, and `TimelineHarness` rather than creating production test helpers.

```python
class CycleRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_cycle_uses_complete_raw_frame_count(self):
        harness = CycleHarness(frames={"呼吸": ["f"] * 12}, gap_tenths=[7])
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 24))
        await harness.complete_cycle()
        self.assertEqual(harness.sent_cycles, [("A", "呼吸", 12)])
        self.assertEqual(harness.records[0].raw_duration_ms, 1200)
        self.assertEqual(harness.records[0].planned_gap_ms, 840)

    async def test_normal_change_waits_for_cycle_boundary(self):
        harness = CycleHarness(frames={"呼吸": ["f"] * 12, "潮汐": ["g"] * 23})
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 25))
        self.assertEqual(harness.sent_patterns, ["呼吸"])
        await harness.complete_cycle()
        self.assertEqual(harness.sent_patterns, ["呼吸", "潮汐"])

    async def test_new_event_during_gap_ends_gap_immediately(self):
        harness = CycleHarness(frames={"呼吸": ["f"] * 12, "潮汐": ["g"] * 23}, gap_tenths=[20])
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.enter_gap()
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 25))
        await harness.flush()
        self.assertEqual(harness.sent_patterns[-1], "潮汐")
        self.assertLess(harness.records[0].actual_gap_ms, harness.records[0].planned_gap_ms)

    async def test_stop_cancels_cycle_and_clears_immediately(self):
        harness = CycleHarness(frames={"呼吸": ["f"] * 12})
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.stop(clear=True, reason="estop")
        self.assertEqual(harness.clear_calls, ["A"])
        self.assertEqual(harness.records[0].interruption_reason, "estop")

    async def test_latest_pending_normal_change_wins(self):
        harness = CycleHarness(frames={"呼吸": ["f"], "潮汐": ["g"], "律动": ["h"]})
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 22))
        await harness.runner.submit(CycleDirective("A", "evt-3", "律动", 24))
        await harness.complete_cycle()
        self.assertEqual(harness.sent_patterns, ["呼吸", "律动"])

    async def test_executor_failure_stops_runner(self):
        harness = CycleHarness(frames={"呼吸": ["f"]}, fail_on_cycle=1)
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.wait_stopped()
        self.assertEqual(harness.runner.state().phase, RunnerPhase.STOPPED)
```

- [ ] **Step 2: Run and verify missing runner failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_cycle_runner -v`

- [ ] **Step 3: Implement serialized transitions**

Use one `asyncio.Lock`, one worker task, one generation integer, and one pending directive. The worker activates strength/pattern once, submits exactly one raw-cycle action, waits `frame_count × 100ms`, applies a pending normal directive at that boundary, otherwise samples and records a gap, and waits interruptibly. A directive arriving during gap cancels only the gap and starts immediately. `pause/stop` increments generation before cancellation so an old task cannot send again.

```python
class RunnerPhase(str, Enum):
    IDLE = "idle"
    CYCLE = "cycle"
    GAP = "gap"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class CycleDirective:
    channel: Literal["A", "B"]
    plot_event_id: str
    pattern: str
    requested_strength: int
```

The executor receives only these high-level actions:

```python
[
    {"op": "hold_strength", "channel": "A", "value": 24},
    {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"},
]
```

`pulse_cycle` is sent again for each raw cycle; strength is sent only when the directive activates. A zero gap begins the next cycle without calling the sleeper.

- [ ] **Step 4: Test A/B independence**

Add a test that runs two runners from `derive_stream_seed(session_seed, "cycle:A")` and `derive_stream_seed(session_seed, "cycle:B")`, advances A through one additional zero-gap cycle, and proves B's first ten gap tenths equal a standalone B runner's first ten values.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_cycle_runner -v`

```bash
git add backend/timeline/cycle_runner.py tests/test_cycle_runner.py
git commit -m "feat: schedule independent waveform cycles"
```

### Task 4: Single-cycle safety and GameLoop execution adapter

**Files:**
- Modify: `backend/safety.py`
- Modify: `backend/game_loop.py:453-590`
- Test: `tests/test_game_loop_cycle.py`

**Interfaces:**
- Consumes: `{"op":"pulse_cycle","channel":Channel,"pattern":str}`.
- Produces: one validated raw-frame device command, `GameLoop.clear_output(channel=None)`, and frame-free effective results.

- [ ] **Step 1: Write failing adapter tests**

```python
class GameLoopCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pulse_cycle_sends_one_unrepeated_frame_sequence(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])
        executed, dropped = await loop.execute_actions([
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        ])
        self.assertEqual(dropped, [])
        self.assertEqual(loop.ops.last_pulse_frames, ["a", "b", "c"])
        self.assertEqual(loop.ops.last_pulse_duration_ms, 300)
        self.assertNotIn("frames", executed[0]["effective"])

    async def test_clear_output_does_not_enter_estop(self):
        loop = make_game_loop_for_test()
        loop.execute_actions = AsyncMock(return_value=([], []))
        await loop.clear_output()
        loop.execute_actions.assert_awaited_once_with([{"op": "stop"}])
        self.assertFalse(loop.safety.estop_active)
```

- [ ] **Step 2: Run and verify unsupported-operation failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_cycle -v`

- [ ] **Step 3: Validate and build exactly one raw cycle**

Add `pulse_cycle` to safety normalization with enabled-channel, allowed-pattern, non-empty-frame, estop, and connection checks. Build a pulse command from the preset's raw frames without tiling to `default_duration_s`; duration is `len(frames) * 100`. Keep existing `pulse` and `pulse_hold` behavior unchanged for manual controls.

Add `effective` mappings containing operation, channel, pattern, requested/effective strength where relevant, and duration, but never frames. Add `clear_output(channel=None)` that uses `clear` for one channel or `stop` for both without entering estop.

- [ ] **Step 4: Run focused and existing safety tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_cycle tests.test_safety -v`

- [ ] **Step 5: Commit**

```bash
git add backend/safety.py backend/game_loop.py tests/test_game_loop_cycle.py
git commit -m "feat: execute one validated waveform cycle"
```

### Task 5: Secure completed-session replay store

**Files:**
- Create: `backend/timeline/replay_store.py`
- Test: `tests/test_replay_store.py`

**Interfaces:**
- Consumes: `ReplayManifest`, `Timeline`, optional scenes/source.
- Produces: `save(...) -> Path`, `load(replay_id) -> ReplayBundle`, `list() -> list[ReplaySummary]`, `delete(replay_id) -> None`.

- [ ] **Step 1: Write failing round-trip/security tests**

```python
class ReplayStoreTests(unittest.TestCase):
    def test_round_trip_preserves_cycle_gap_tenths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[0, 7, 20], status="completed")
            store.save(bundle.manifest, bundle.timeline)
            self.assertEqual(store.load(bundle.manifest.replay_id).timeline.cycles, bundle.timeline.cycles)

    def test_rejects_path_unsafe_archive_before_reading_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "evil.coyote-replay")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaises(ReplayStoreError):
                ReplayStore(Path(tmp)).load("evil")

    def test_incomplete_session_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ReplayStoreError, "completed"):
                bundle = make_replay_bundle(gap_tenths=[], status="paused")
                ReplayStore(Path(tmp)).save(bundle.manifest, bundle.timeline)
```

- [ ] **Step 2: Run and verify missing store failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_store -v`

- [ ] **Step 3: Implement atomic strict ZIP persistence**

Permit exactly `manifest.json`, `timeline.json`, optional `scenes.json`, and one allowed `source.<ext>`. Validate replay IDs against `[A-Za-z0-9_-]{1,128}`, entry count, normalized `PurePosixPath`, size limits, schema, and SHA-256 checksums before returning a bundle. Write a sibling temporary file, close it, then `Path.replace()` the final archive.

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_store -v`

```bash
git add backend/timeline/replay_store.py tests/test_replay_store.py
git commit -m "feat: persist cycle-aware replay archives"
```

### Task 6: Session controller and exact recorded-cycle player

**Files:**
- Create: `backend/timeline/player.py`
- Create: `backend/timeline/session.py`
- Test: `tests/test_timeline_player.py`
- Test: `tests/test_timeline_session.py`

**Interfaces:**
- Produces: `SessionController.start_live()`, `process_live_turn()`, `pause()`, `resume()`, `finish()`, `on_disconnect()`, `start_replay()`, `to_state()`.
- Produces: `RecordedCyclePlayer.load()`, `start()`, `pause()`, `resume(cursor)`, `stop()`, `wait()`.

- [ ] **Step 1: Write failing lifecycle tests**

Extend `tests/timeline_fakes.py` with `SessionHarness.create(seed)` backed by a temporary replay directory, two `CycleHarness` instances, a fake resolver, and active-time clock; expose the exact methods used below. Extend it with `ReplayHarness.from_cycles(...)` and `from_strength(...)` backed by a fake executor that counts RNG calls and records requested cycle actions.

```python
class SessionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_clears_both_channels_but_does_not_save(self):
        controller = SessionHarness.create(seed=9)
        await controller.start_live()
        await controller.process_live_turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await controller.pause()
        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])

    async def test_finish_saves_generated_gaps_without_manual_pause_time(self):
        controller = SessionHarness.create(seed=9)
        await controller.start_live()
        await controller.complete_cycles("A", gap_tenths=[0, 7, 20])
        await controller.pause(manual_elapsed_ms=600000)
        await controller.resume()
        summary = await controller.finish()
        timeline = controller.store.load(summary.replay_id).timeline
        self.assertEqual([c.gap_tenths for c in timeline.cycles], [0, 7, 20])
        self.assertNotIn(600000, [c.active_start_offset_ms for c in timeline.cycles])

    async def test_disconnect_pauses_and_resume_starts_complete_cycle(self):
        controller = SessionHarness.create(seed=10)
        await controller.start_live()
        await controller.begin_partial_cycle("A")
        await controller.on_disconnect()
        await controller.resume()
        self.assertEqual(controller.last_cycle_started_at_frame, 0)
        self.assertEqual(controller.store.list(), [])
```

- [ ] **Step 2: Write failing exact-replay tests**

```python
class RecordedCyclePlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_uses_recorded_cycle_starts_without_rng(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 7, 20])
        await harness.player.start()
        await harness.player.wait()
        self.assertEqual(harness.requested_gap_tenths, [0, 7, 20])
        self.assertEqual(harness.rng_calls, 0)

    async def test_current_safety_clamp_marks_adjusted(self):
        harness = ReplayHarness.from_strength(original=40, current_cap=30)
        await harness.player.start()
        await harness.player.wait()
        self.assertTrue(harness.player.adjusted)
```

- [ ] **Step 3: Run and verify missing classes**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_session tests.test_timeline_player -v`

- [ ] **Step 4: Implement live lifecycle**

Create A/B runners from the `cycle:A` and `cycle:B` streams. `process_live_turn()` resolves one plot event using the `plot:A`/`plot:B` streams and submits directives; it never changes `GameLoop.autopilot_interval`. Runner callbacks append records using active-time offsets that stop advancing during manual pause. `pause()` cancels both runners and clears both channels. `resume()` starts complete cycles from the retained plot event. `finish()` clears, finalizes only a normal completion, saves atomically, and returns to idle. Disconnect calls pause only.

- [ ] **Step 5: Implement exact recorded-cycle playback**

Sort recorded cycle starts by active-time offset while retaining per-channel order. Schedule them with monotonic time, send stored strength/pattern actions through `GameLoop.execute_actions()`, compare effective results with the original record, and mark adjusted on differences. Never instantiate or call a random generator during replay.

- [ ] **Step 6: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_session tests.test_timeline_player -v`

```bash
git add backend/timeline/player.py backend/timeline/session.py tests/test_timeline_player.py tests/test_timeline_session.py
git commit -m "feat: record and replay completed cycle sessions"
```

### Task 7: FastAPI integration and automatic-mode wiring

**Files:**
- Modify: `backend/game_loop.py:34-367`
- Modify: `backend/main.py:93-812`
- Test: `tests/test_game_loop_timeline.py`
- Test: `tests/test_session_endpoints.py`

**Interfaces:**
- Consumes: `SessionController` and existing automatic turns.
- Produces: session/replay routes plus session and runner state in HTTP/WebSocket state.

- [ ] **Step 1: Write failing integration tests**

Assert an active live session receives AI actions, while `_autopilot_loop` continues waiting exactly `self.autopilot_interval`. Assert manual `pulse`/`pulse_hold` bypass cycle-gap scheduling. Endpoint tests use temporary replay storage, fake relay, and fake LLM.

```python
class GameLoopTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_completion_never_invokes_llm(self):
        loop = make_game_loop_for_test()
        await loop.timeline_session.runners["A"].test_complete_one_cycle()
        loop.llm.complete.assert_not_called()

    async def test_autopilot_delay_remains_configured_value(self):
        loop = make_game_loop_for_test(autopilot_interval=12)
        self.assertEqual(loop.autopilot_interval, 12)
        self.assertFalse(hasattr(loop.timeline_session, "next_interval_s"))
```

- [ ] **Step 2: Run and verify missing integration**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints -v`

- [ ] **Step 3: Attach the session without changing automatic timing**

Route live AI actions through `SessionController.process_live_turn()` when automatic mode has an active session. Keep the existing `autopilot_interval` wait unchanged. Turning automatic mode off pauses/clears without finishing. Switching role/profile or clearing history normally finishes before the change.

- [ ] **Step 4: Implement routes**

```text
POST /api/session/start                         -> SessionState
POST /api/session/pause                         -> SessionState
POST /api/session/resume {"cursor":null|int}    -> SessionState
POST /api/session/finish                        -> ReplaySummary
GET  /api/replays                               -> ReplaySummary[]
GET  /api/replays/{id}/download                 -> FileResponse
POST /api/replays/{id}/play {"cursor":0}        -> SessionState
POST /api/replays/playback/pause                -> SessionState
POST /api/replays/playback/resume               -> SessionState
POST /api/replays/playback/stop                 -> SessionState
```

Expose per-channel runner phase, pattern, strength, cycle index, and next cycle start in state. Do not expose source text, RNG internals, or waveform frames.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints -v`

```bash
git add backend/game_loop.py backend/main.py tests/test_game_loop_timeline.py tests/test_session_endpoints.py
git commit -m "feat: expose cycle-aware sessions and replays"
```

### Task 8: Minimal session and replay UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/TopBar.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Create: `frontend/src/components/ReplayPanel.tsx`

**Interfaces:**
- Consumes: Task 7 state/routes.
- Produces: start/resume, pause, finish/save, current A/B cycle state, replay list/playback/download.

- [ ] **Step 1: Add exact TypeScript contracts**

```typescript
export interface ChannelCycleState {
  phase: "idle" | "cycle" | "gap" | "paused" | "stopped";
  pattern: string | null;
  strength: number;
  cycleIndex: number;
  nextCycleAtMs: number | null;
}

export interface TimelineSessionState {
  sessionId: string | null;
  status: "idle" | "running" | "paused" | "finishing" | "replaying";
  mode: "autopilot" | "replay" | null;
  cursor: number;
  adjusted: boolean;
  channels: { A: ChannelCycleState; B: ChannelCycleState };
}
```

- [ ] **Step 2: Add session-aware controls**

Idle shows `开始自动运行`; running shows `暂停` and `结束并保存`; paused shows `继续` and `结束并保存`; replaying shows `暂停重放` and `停止重放`. Display current waveform, strength, and `播放/间隔` phase for each channel. Do not expose seed, gap multiplier, or hidden reasoning in normal view.

- [ ] **Step 3: Add minimal replay view**

Add `历史` navigation. Each completed row shows title/date/DLC/cycle count/exact status plus `重放` and `下载`. Search/rating/delete remain Phase 3.

- [ ] **Step 4: Build and commit**

Run: `npm --prefix frontend run build`

Expected: TypeScript and Vite exit 0.

```bash
git add frontend/src
git commit -m "feat: add cycle session and replay controls"
```

### Task 9: Full verification, documentation, and MVP1 gate

**Files:**
- Modify: `README.md`
- Test: `tests/test_mvp1_timeline_integration.py`

**Interfaces:**
- Produces: deterministic dry-run proof and operator documentation.

- [ ] **Step 1: Write end-to-end dry-run test**

Extend `tests/timeline_fakes.py` with `TimelineHarness.create(seed, dry_run)`. It composes the real resolver, runners, controller, replay store, and recorded-cycle player with a fake relay and controlled clock; its `rng_calls` counter increments only when live runner gap RNG is sampled.

```python
class MVP1IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_cycle_session_round_trips_without_resampling(self):
        harness = await TimelineHarness.create(seed=20260831, dry_run=True)
        await harness.start()
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await harness.complete_cycles("A", count=3)
        await harness.turn([{"op": "hold_strength", "channel": "B", "value": 12}])
        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)
        result = await harness.replay(replay)
        self.assertEqual(result.requested_cycles, replay.timeline.cycles)
        self.assertEqual(result.rng_calls, 0)
        self.assertEqual(harness.relay.frames, [])
```

- [ ] **Step 2: Document behavior**

Document raw-cycle definition, 40/30/30 gap policy, A/B independence, unchanged AI turn timing, strength retention during generated gaps, boundary switching, immediate stop-class behavior, archive location, exact/adjusted semantics, and manual controls remaining unchanged.

- [ ] **Step 3: Run complete automated verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend run build
```

Expected: every command exits 0; no external LLM or relay request occurs.

- [ ] **Step 4: Run manual dry-run acceptance**

With `dry_run: true` and cap at or below 40:

1. start automatic play and confirm plot turns retain their configured cadence;
2. confirm A/B cycle and gap independently;
3. confirm pattern/strength stay fixed within one plot event;
4. send a normal change mid-cycle and confirm boundary application;
5. send a change during gap and confirm immediate new cycle;
6. pause and confirm both channels clear;
7. finish and replay; confirm identical cycle starts/gaps with zero RNG calls;
8. lower a cap and confirm adjusted replay.

- [ ] **Step 5: Commit and push the implementation branch**

```bash
git add README.md tests/test_mvp1_timeline_integration.py
git commit -m "test: verify cycle-aware timeline replay"
git push -u origin codex/mvp1-randomized-timeline-replay
```

- [ ] **Step 6: Run short real-device gate and tag only after acceptance**

With cap 40 or lower, verify independent cycle/gap behavior, boundary switching, immediate pause/estop clearing, and exact replay. Then:

```bash
git tag -a mvp1-randomized-timeline-replay -m "MVP1 cycle-aware timeline and exact replay accepted"
git push origin mvp1-randomized-timeline-replay
```
