# MVP1 Randomized Timeline and Exact Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seeded waveform/strength/interval variation to the existing automatic mode, record every normally completed session, and replay its fully resolved timeline exactly through the current safety layer.

**Architecture:** Add a focused `backend.timeline` package between AI actions and `GameLoop.execute_actions()`. Live turns are resolved into serializable timeline events, executed only through `SafetyManager`, recorded in `.coyote-replay` ZIP archives, and exposed through small session/replay APIs and UI controls.

**Tech Stack:** Python 3.12, stdlib `dataclasses`/`enum`/`random`/`asyncio`/`zipfile`, FastAPI, React 19, TypeScript, Zustand, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

## Global Constraints

- Plot intent precedes randomness; safety precedes both.
- All device output must pass through `GameLoop.execute_actions()` and `SafetyManager.validate()`.
- Strength jitter is an integer in `[-4, +4]` around the base target.
- Do not add cross-scene smoothing in the resolver.
- Default interval profile is 70% `6–12s`, 20% `2–5s`, and 10% `15–25s`.
- Current personal runtime cap is 40 per channel and remains in ignored local configuration; committed code must not raise it.
- Pausing clears both channels; only normally completed sessions enter permanent history.
- Exact replay uses resolved events and is marked adjusted when current safety changes an event.
- Runtime data belongs under ignored `data/`; no API keys, imported content, or replay files enter Git.
- Use the existing `unittest` suite; do not run `tests/probe_llm.py` in automated verification.

---

## File Structure

- Create `backend/timeline/models.py`: serializable directives, profiles, events, timelines, manifests, and session state.
- Create `backend/timeline/randomizer.py`: seeded live-turn resolution.
- Create `backend/timeline/player.py`: async event playback and pause/resume/stop.
- Create `backend/timeline/replay_store.py`: secure archive persistence.
- Create `backend/timeline/session.py`: session lifecycle and recording orchestration.
- Create `backend/timeline/__init__.py`: public timeline interfaces.
- Modify `backend/config.py` and `config/config.example.yaml`: timeline defaults.
- Modify `backend/game_loop.py`: timeline hook, randomized autopilot delay, effective execution result, and non-estop clear.
- Modify `backend/main.py`: construct services, expose session/replay API, and broadcast state.
- Create `frontend/src/components/ReplayPanel.tsx`: minimal completed-session list and replay controls.
- Modify `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/components/TopBar.tsx`, and `frontend/src/components/ChatPanel.tsx`.
- Create focused tests under `tests/` for every backend module.

### Task 1: Timeline domain model and configuration

**Files:**
- Create: `backend/timeline/__init__.py`
- Create: `backend/timeline/models.py`
- Create: `tests/test_timeline_models.py`
- Modify: `backend/config.py:22-106`
- Modify: `config/config.example.yaml`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `DirectiveMode`, `ChannelDirective`, `IntervalBand`, `RandomizationProfile`, `TimelineEvent`, `Timeline`, `ReplayManifest`, `SessionStatus`, and `SessionState`.
- Produces: `RandomizationProfile.from_mapping(data: dict) -> RandomizationProfile` and `Timeline.to_dict()/from_dict()`.

- [ ] **Step 1: Write serialization and validation tests**

```python
# tests/test_timeline_models.py
import unittest

from backend.timeline.models import (
    ChannelDirective,
    DirectiveMode,
    IntervalBand,
    RandomizationProfile,
    Timeline,
    TimelineEvent,
)


class TimelineModelTests(unittest.TestCase):
    def test_profile_rejects_non_positive_total_weight(self):
        with self.assertRaisesRegex(ValueError, "weight"):
            RandomizationProfile(
                strength_jitter=4,
                waveform_policy="all_allowed",
                intervals=(IntervalBand("bad", 0.0, 1.0, 2.0),),
            )

    def test_timeline_round_trip_preserves_resolved_actions(self):
        event = TimelineEvent(
            event_id="evt-000001",
            scene_id="live-turn-1",
            offset_ms=0,
            duration_ms=6000,
            next_gap_ms=7000,
            requested_actions=(
                {"op": "hold_strength", "channel": "A", "value": 24},
                {"op": "pulse", "channel": "A", "pattern": "呼吸", "duration_s": 6.0},
            ),
            source={"base_strength": 20, "random_range": [-4, 4]},
        )
        timeline = Timeline("session-1", 1234, (event,))
        self.assertEqual(Timeline.from_dict(timeline.to_dict()), timeline)

    def test_channel_directive_requires_values_only_for_set(self):
        directive = ChannelDirective("B", DirectiveMode.STOP)
        self.assertIsNone(directive.pattern)
        with self.assertRaises(ValueError):
            ChannelDirective("A", DirectiveMode.SET, pattern="", base_strength=None)
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_models -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.timeline'`.

- [ ] **Step 3: Implement the model contracts**

```python
# backend/timeline/models.py
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

ChannelName = Literal["A", "B"]


class DirectiveMode(str, Enum):
    KEEP = "keep"
    SET = "set"
    STOP = "stop"


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHING = "finishing"
    COMPLETED = "completed"
    REPLAYING = "replaying"


@dataclass(frozen=True)
class ChannelDirective:
    channel: ChannelName
    mode: DirectiveMode
    pattern: str | None = None
    base_strength: int | None = None

    def __post_init__(self) -> None:
        if self.channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        if self.mode is DirectiveMode.SET and (not self.pattern or self.base_strength is None):
            raise ValueError("set directive requires pattern and base_strength")


@dataclass(frozen=True)
class IntervalBand:
    name: str
    weight: float
    min_s: float
    max_s: float


@dataclass(frozen=True)
class RandomizationProfile:
    strength_jitter: int
    waveform_policy: str
    intervals: tuple[IntervalBand, ...]

    def __post_init__(self) -> None:
        if self.strength_jitter < 0:
            raise ValueError("strength_jitter must be non-negative")
        if not self.intervals or sum(b.weight for b in self.intervals) <= 0:
            raise ValueError("interval weight total must be positive")
        if any(b.min_s <= 0 or b.max_s < b.min_s for b in self.intervals):
            raise ValueError("invalid interval range")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RandomizationProfile":
        intervals = tuple(
            IntervalBand(
                name=str(item["name"]),
                weight=float(item["weight"]),
                min_s=float(item["min_s"]),
                max_s=float(item["max_s"]),
            )
            for item in data["intervals"]
        )
        return cls(
            strength_jitter=int(data["strength_jitter"]),
            waveform_policy=str(data["waveform_policy"]),
            intervals=intervals,
        )


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    scene_id: str
    offset_ms: int
    duration_ms: int
    next_gap_ms: int
    requested_actions: tuple[dict[str, Any], ...]
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Timeline:
    session_id: str
    seed: int
    events: tuple[TimelineEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Timeline":
        events = tuple(
            TimelineEvent(
                event_id=str(item["event_id"]),
                scene_id=str(item["scene_id"]),
                offset_ms=int(item["offset_ms"]),
                duration_ms=int(item["duration_ms"]),
                next_gap_ms=int(item["next_gap_ms"]),
                requested_actions=tuple(dict(action) for action in item["requested_actions"]),
                source=dict(item.get("source", {})),
            )
            for item in data["events"]
        )
        return cls(session_id=str(data["session_id"]), seed=int(data["seed"]), events=events)
```

Implement both constructors with explicit type/range checks; do not use unchecked `Timeline(**data)` for archive input.

- [ ] **Step 4: Add committed defaults and ignore runtime storage**

Add to `backend.config.DEFAULTS` and mirror it in `config/config.example.yaml`:

```yaml
timeline:
  strength_jitter: 4
  waveform_policy: all_allowed
  intervals:
    - {name: normal, weight: 0.70, min_s: 6, max_s: 12}
    - {name: short, weight: 0.20, min_s: 2, max_s: 5}
    - {name: long, weight: 0.10, min_s: 15, max_s: 25}
  replay_dir: data/replays
```

Append `data/` to `.gitignore`.

- [ ] **Step 5: Run model tests and configuration smoke test**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_models -v`

Run: `.venv\Scripts\python.exe -c "from backend.config import load_config; from backend.timeline.models import RandomizationProfile; print(RandomizationProfile.from_mapping(load_config()['timeline']))"`

Expected: all model tests PASS and the smoke command prints a validated profile with jitter `4`.

- [ ] **Step 6: Commit the domain model**

```bash
git add .gitignore backend/timeline backend/config.py config/config.example.yaml tests/test_timeline_models.py
git commit -m "feat: define timeline domain and randomization config"
```

### Task 2: Seeded live-turn resolver

**Files:**
- Create: `backend/timeline/randomizer.py`
- Create: `tests/test_timeline_randomizer.py`

**Interfaces:**
- Consumes: `RandomizationProfile`, current channel strength/caps, enabled channels, and allowed preset names.
- Produces: `TimelineResolver.resolve_live_segment(...) -> TimelineEvent` and `TimelineResolver.next_interval_ms() -> int`.

- [ ] **Step 1: Write deterministic resolver tests**

```python
# tests/test_timeline_randomizer.py
import random
import unittest

from backend.timeline.models import IntervalBand, RandomizationProfile
from backend.timeline.randomizer import TimelineResolver


class TimelineResolverTests(unittest.TestCase):
    def make_resolver(self, seed: int = 7) -> TimelineResolver:
        profile = RandomizationProfile(
            strength_jitter=4,
            waveform_policy="all_allowed",
            intervals=(IntervalBand("fixed", 1.0, 7.0, 7.0),),
        )
        return TimelineResolver(profile, random.Random(seed))

    def test_same_seed_produces_same_event(self):
        args = dict(
            actions=[{"op": "hold_strength", "channel": "A", "value": 20}],
            current={"A": 0, "B": 0},
            caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True},
            presets=("呼吸", "脉冲"),
            scene_id="live-turn-1",
            event_index=1,
            offset_ms=0,
        )
        self.assertEqual(
            self.make_resolver().resolve_live_segment(**args),
            self.make_resolver().resolve_live_segment(**args),
        )

    def test_strength_stays_inside_base_jitter_and_cap(self):
        event = self.make_resolver().resolve_live_segment(
            actions=[{"op": "hold_strength", "channel": "A", "value": 39}],
            current={"A": 0, "B": 0},
            caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True},
            presets=("呼吸",),
            scene_id="live-turn-2",
            event_index=2,
            offset_ms=7000,
        )
        strength = next(a["value"] for a in event.requested_actions if a["op"] == "hold_strength")
        self.assertLessEqual(strength, 40)
        self.assertGreaterEqual(strength, 35)

    def test_stop_is_preserved_without_random_output(self):
        event = self.make_resolver().resolve_live_segment(
            actions=[{"op": "clear", "channel": "B"}],
            current={"A": 10, "B": 10}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸",),
            scene_id="live-turn-3", event_index=3, offset_ms=14000,
        )
        self.assertEqual(event.requested_actions, ({"op": "clear", "channel": "B"},))
```

- [ ] **Step 2: Run the resolver tests and observe the missing class**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_randomizer -v`

Expected: FAIL importing `backend.timeline.randomizer`.

- [ ] **Step 3: Implement action normalization and seeded resolution**

```python
# backend/timeline/randomizer.py
class TimelineResolver:
    def __init__(self, profile: RandomizationProfile, rng: random.Random) -> None:
        self.profile = profile
        self.rng = rng

    def next_interval_ms(self) -> int:
        bands = self.profile.intervals
        band = self.rng.choices(bands, weights=[b.weight for b in bands], k=1)[0]
        return round(self.rng.uniform(band.min_s, band.max_s) * 1000)

    def resolve_live_segment(
        self, *, actions: list[dict], current: dict[str, int], caps: dict[str, int],
        enabled: dict[str, bool], presets: tuple[str, ...], scene_id: str,
        event_index: int, offset_ms: int,
    ) -> TimelineEvent:
        requested: list[dict] = []
        stopped = {a.get("channel") for a in actions if a.get("op") in {"stop", "clear"}}
        for action in actions:
            channel = action.get("channel")
            if action.get("op") in {"stop", "clear"}:
                requested.append(dict(action))
                continue
            if channel not in {"A", "B"} or channel in stopped or not enabled[channel]:
                continue
            if action.get("op") == "add_strength":
                base = current[channel] + int(action["delta"])
            elif action.get("op") in {"hold_strength", "temp_strength"}:
                base = int(action["value"])
            else:
                continue
            value = max(0, min(caps[channel], base + self.rng.randint(-self.profile.strength_jitter, self.profile.strength_jitter)))
            pattern = self.rng.choice(presets)
            requested.extend((
                {"op": "hold_strength", "channel": channel, "value": value},
                {"op": "pulse", "channel": channel, "pattern": pattern},
            ))
        gap_ms = self.next_interval_ms()
        return TimelineEvent(
            event_id=f"evt-{event_index:06d}", scene_id=scene_id,
            offset_ms=offset_ms, duration_ms=gap_ms, next_gap_ms=gap_ms,
            requested_actions=tuple(requested),
            source={"random_range": [-self.profile.strength_jitter, self.profile.strength_jitter]},
        )
```

Resolution rules, in order:

1. Preserve `stop` and `clear` actions exactly and do not add random output to the same channel.
2. Convert `add_strength` to a base target using `current + delta`; read `value` directly from `hold_strength`/`temp_strength`.
3. Ignore disabled channels.
4. For each set channel, choose one allowed preset using the seeded RNG, resolve `base + randint(-4, 4)`, clip to `0..cap`, and emit `hold_strength` followed by `pulse`.
5. Use the selected interval for `next_gap_ms`; use the waveform preset's default duration later in the session adapter.
6. Emit stable event IDs `evt-{event_index:06d}`.

- [ ] **Step 4: Run the resolver tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_randomizer -v`

Expected: all resolver tests PASS.

- [ ] **Step 5: Commit the resolver**

```bash
git add backend/timeline/randomizer.py tests/test_timeline_randomizer.py
git commit -m "feat: resolve seeded live timeline segments"
```

### Task 3: Secure replay archive store

**Files:**
- Create: `backend/timeline/replay_store.py`
- Create: `tests/test_replay_store.py`

**Interfaces:**
- Consumes: `ReplayManifest`, `Timeline`, optional `scenes` mapping, optional source bytes and extension.
- Produces: `ReplayStore.save(...) -> pathlib.Path`, `load(replay_id) -> ReplayBundle`, `list() -> list[ReplaySummary]`, `delete(replay_id) -> None`.

- [ ] **Step 1: Write archive round-trip and rejection tests**

```python
# tests/test_replay_store.py
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.timeline.models import ReplayManifest, Timeline
from backend.timeline.replay_store import ReplayStore, ReplayStoreError


class ReplayStoreTests(unittest.TestCase):
    def test_round_trip_preserves_manifest_and_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            manifest = ReplayManifest.for_test("r1", "completed")
            timeline = Timeline("r1", 42, ())
            path = store.save(manifest, timeline)
            self.assertEqual(path.suffix, ".coyote-replay")
            loaded = store.load("r1")
            self.assertEqual(loaded.manifest.replay_id, "r1")
            self.assertEqual(loaded.timeline.seed, 42)

    def test_rejects_archive_entry_outside_allowed_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "evil.coyote-replay")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("../escape.txt", "bad")
            with self.assertRaises(ReplayStoreError):
                ReplayStore(Path(tmp)).load("evil")

    def test_incomplete_manifest_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            with self.assertRaisesRegex(ReplayStoreError, "completed"):
                store.save(ReplayManifest.for_test("r2", "paused"), Timeline("r2", 1, ()))
```

- [ ] **Step 2: Run replay-store tests and verify failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_store -v`

Expected: FAIL importing `backend.timeline.replay_store`.

- [ ] **Step 3: Implement atomic archive writes and strict reads**

Use `tempfile.NamedTemporaryFile(delete=False, dir=replay_dir)` and `Path.replace()` only after the ZIP closes successfully. Permit exactly `manifest.json`, `timeline.json`, optional `scenes.json`, and one `source.<allowed-extension>` entry. Validate entry names with `PurePosixPath`, reject absolute paths, `..`, more than 8 entries, and files larger than configured limits before reading contents.

Implement `ReplayStore.__init__(root: Path)`, `save(manifest, timeline, *, scenes=None, source=None, source_extension=None) -> Path`, `load(replay_id) -> ReplayBundle`, `list() -> list[ReplaySummary]`, and `delete(replay_id) -> None`. Resolve every requested replay ID to `<root>/<safe-id>.coyote-replay` and reject IDs outside `[A-Za-z0-9_-]{1,128}` before filesystem access.

Calculate SHA-256 for `timeline.json`, optional scene/source entries, and store checksums in the manifest. Verify them on load.

- [ ] **Step 4: Run replay-store tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_store -v`

Expected: all replay-store tests PASS and no file appears outside the temporary replay directory.

- [ ] **Step 5: Commit the replay store**

```bash
git add backend/timeline/models.py backend/timeline/replay_store.py tests/test_replay_store.py
git commit -m "feat: persist secure coyote replay archives"
```

### Task 4: Async timeline player

**Files:**
- Create: `backend/timeline/player.py`
- Create: `tests/test_timeline_player.py`

**Interfaces:**
- Consumes: `Timeline`, async `executor(actions)`, async `clear_output()`, monotonic clock, and sleep function.
- Produces: `TimelinePlayer.start()`, `pause()`, `resume()`, `finish()`, `stop()`, cursor/status state, and per-event result callback.

- [ ] **Step 1: Write state-machine tests with fake time**

```python
# tests/test_timeline_player.py
import asyncio
import unittest

from backend.timeline.models import Timeline, TimelineEvent
from backend.timeline.player import TimelinePlayer


class TimelinePlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_clears_and_preserves_cursor(self):
        executed, clears = [], []

        async def execute(actions):
            executed.append(actions)
            return ([{"effective": actions[0]}], [])

        async def clear():
            clears.append(True)

        event = TimelineEvent("evt-000001", "s1", 0, 1000, 1000,
                              ({"op": "clear", "channel": "A"},), {})
        player = TimelinePlayer(execute, clear)
        await player.load(Timeline("session-1", 1, (event,)))
        await player.start()
        await player.pause()
        self.assertEqual(len(clears), 1)
        self.assertEqual(player.cursor, 1)

    async def test_effective_difference_marks_adjusted(self):
        async def execute(actions):
            return ([{"effective": {**actions[0], "value": 30}}], [])
        async def clear():
            return None
        event = TimelineEvent("evt-000001", "s1", 0, 1000, 0,
                              ({"op": "hold_strength", "channel": "A", "value": 40},), {})
        player = TimelinePlayer(execute, clear)
        await player.load(Timeline("session-1", 1, (event,)), expected_effective=[{"value": 40}])
        await player.start()
        await player.wait()
        self.assertTrue(player.adjusted)
```

- [ ] **Step 2: Run player tests and verify missing module failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_player -v`

Expected: FAIL importing `backend.timeline.player`.

- [ ] **Step 3: Implement serialized player transitions**

Guard every public transition with one `asyncio.Lock`. Maintain one worker task and one pause event. `pause()` must cancel any outstanding sleep, await `clear_output()`, and leave the cursor pointing after the last completed event. `finish()` and `stop()` must also clear output; only `finish()` may ask the session controller to persist.

Implement these exact public methods: `load(timeline, *, expected_effective=None)`, `start()`, `pause()`, `resume(cursor=None)`, `finish()`, `stop()`, and `wait()`. Expose read-only `cursor`, `status`, and `adjusted` properties. Invalid transitions raise `TimelineStateError`; loading or starting while a worker is active must fail instead of silently replacing the task.

- [ ] **Step 4: Run player tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_player -v`

Expected: all player tests PASS without real sleeps longer than a test event loop tick.

- [ ] **Step 5: Commit the player**

```bash
git add backend/timeline/player.py tests/test_timeline_player.py
git commit -m "feat: add pausable deterministic timeline player"
```

### Task 5: Session controller and completed-session recording

**Files:**
- Create: `backend/timeline/session.py`
- Create: `tests/test_timeline_session.py`

**Interfaces:**
- Consumes: resolver, player, replay store, state supplier, allowed preset supplier, DLC metadata supplier, and app commit supplier.
- Produces: `SessionController.start_live()`, `process_live_turn()`, `pause()`, `resume()`, `finish()`, `on_disconnect()`, `start_replay()`, and `to_state()`.

- [ ] **Step 1: Write lifecycle and persistence tests**

```python
# tests/test_timeline_session.py
import tempfile
import unittest
from pathlib import Path

from backend.timeline.session import SessionController


class SessionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_saves_but_pause_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = SessionController.for_test(Path(tmp), seed=9)
            await controller.start_live({"role": "触手", "profile": "调教"})
            await controller.process_live_turn(
                [{"op": "hold_strength", "channel": "A", "value": 20}],
                scene_id="live-turn-1",
            )
            await controller.pause()
            self.assertEqual(controller.replay_store.list(), [])
            await controller.resume()
            summary = await controller.finish()
            self.assertEqual(summary.status, "completed")
            self.assertEqual(len(controller.replay_store.list()), 1)

    async def test_disconnect_pauses_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = SessionController.for_test(Path(tmp), seed=9)
            await controller.start_live({"role": "触手", "profile": "调教"})
            await controller.on_disconnect()
            self.assertEqual(controller.to_state()["status"], "paused")
            self.assertEqual(controller.replay_store.list(), [])
```

- [ ] **Step 2: Run lifecycle tests and verify failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_session -v`

Expected: FAIL importing `backend.timeline.session`.

- [ ] **Step 3: Implement live recording and replay startup**

`process_live_turn()` resolves one segment, immediately executes it through the player executor, appends the resolved event plus effective results to the active recording, and returns the same `executed/dropped` structure expected by chat broadcast. Record session-relative monotonic offsets rather than wall-clock scheduling dates.

`finish()` must:

1. transition to `finishing`;
2. clear output;
3. create a `completed` manifest;
4. write the archive atomically;
5. transition to `idle` while returning the replay summary.

`on_disconnect()` calls `pause()` only. No archive is written.

- [ ] **Step 4: Run session tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline_session -v`

Expected: lifecycle tests PASS and only the completed test creates a replay.

- [ ] **Step 5: Commit the controller**

```bash
git add backend/timeline/session.py tests/test_timeline_session.py
git commit -m "feat: manage live timeline sessions and recording"
```

### Task 6: Integrate timeline services with GameLoop and FastAPI

**Files:**
- Modify: `backend/game_loop.py:299-367,453-530,711-733`
- Modify: `backend/main.py:93-310,347-812`
- Create: `tests/test_game_loop_timeline.py`
- Create: `tests/test_session_endpoints.py`

**Interfaces:**
- Consumes: `SessionController` and existing `GameLoop.execute_actions()`.
- Produces: `GameLoop.clear_output()`, execution result field `effective`, session/replay routes, and WebSocket session state.

- [ ] **Step 1: Write GameLoop adapter tests**

Use `unittest.mock.AsyncMock` to assert that an active controller receives AI actions and supplies the next delay. The test must also assert `clear_output()` executes `{"op": "stop"}` without setting `SafetyManager.estop_active`.

```python
class GameLoopTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_output_does_not_enter_estop(self):
        loop = make_game_loop_for_test()
        loop.execute_actions = AsyncMock(return_value=([], []))
        await loop.clear_output()
        loop.execute_actions.assert_awaited_once_with([{"op": "stop"}])
        self.assertFalse(loop.safety.estop_active)
```

- [ ] **Step 2: Run the adapter test and verify the missing method**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline -v`

Expected: FAIL because `GameLoop.clear_output` and the timeline hook do not exist.

- [ ] **Step 3: Add the GameLoop hook without bypassing safety**

- Add `self.timeline_session = None` in `GameLoop.__init__`.
- In `_autopilot_turn`, route generated actions through `timeline_session.process_live_turn()` when a live session is active; preserve the legacy path when no session is active.
- In `_autopilot_loop`, ask the active controller for `next_interval_s`; otherwise retain `autopilot_interval`.
- Add `async def clear_output(self)` that calls `execute_actions([{"op": "stop"}])` and does not call `estop()`.
- Extend every `executed` item with a frame-free `effective` mapping derived from the validated command: `op`, `channel`, `value`, `delta`, `pattern`, and `duration_s` as applicable. Never serialize waveform frames.

- [ ] **Step 4: Construct services and expose API routes**

In `AppState.__init__`, create the replay store from `cfg["timeline"]["replay_dir"]`, create `SessionController`, attach it to `GameLoop`, and include `session` in `build_state()`.

Implement exact request/response behavior:

```text
POST /api/session/start   {"mode":"autopilot"} -> {"ok":true,"session":SessionState}
POST /api/session/pause   {} -> {"ok":true,"session":SessionState}
POST /api/session/resume  {"cursor":null|int} -> {"ok":true,"session":SessionState}
POST /api/session/finish  {} -> {"ok":true,"replay":ReplaySummary}
GET  /api/replays         -> {"items":[ReplaySummary]}
GET  /api/replays/{id}/download -> FileResponse
POST /api/replays/{id}/play {"cursor":0} -> {"ok":true,"session":SessionState}
POST /api/replays/playback/pause  {}
POST /api/replays/playback/resume {"cursor":null|int}
POST /api/replays/playback/stop   {}
```

Starting autopilot starts/resumes the live session. Turning autopilot off pauses and clears; it does not finish. Switching role/profile and clearing history call `finish()` before applying the change.

Use FastAPI dependency seams or an `AppState` test factory so endpoint tests use temporary replay directories and fake relay/LLM objects; they must not open sockets or call OpenRouter.

- [ ] **Step 5: Run backend integration tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints -v`

Expected: all adapter and endpoint tests PASS.

- [ ] **Step 6: Commit backend integration**

```bash
git add backend/game_loop.py backend/main.py tests/test_game_loop_timeline.py tests/test_session_endpoints.py
git commit -m "feat: expose timeline session and replay APIs"
```

### Task 7: Add minimal session and replay UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/TopBar.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Create: `frontend/src/components/ReplayPanel.tsx`

**Interfaces:**
- Consumes: session state and replay API from Task 6.
- Produces: start/resume, pause, finish-and-save, replay list, replay start/pause/resume/stop, and download controls.

- [ ] **Step 1: Add exact TypeScript contracts**

```typescript
export interface TimelineSessionState {
  session_id: string | null;
  status: "idle" | "running" | "paused" | "finishing" | "completed" | "replaying";
  mode: "autopilot" | "replay" | null;
  cursor: number;
  event_count: number;
  current_event_id: string | null;
  adjusted: boolean;
  next_event_at_ms: number | null;
}

export interface ReplaySummary {
  replay_id: string;
  title: string;
  completed_at: string;
  role: string;
  profile: string;
  event_count: number;
  exact: boolean;
}
```

Add `session: TimelineSessionState` to `FullState` and add typed methods to `api.ts` for every Task 6 endpoint.

- [ ] **Step 2: Replace the binary autopilot toggle with session-aware controls**

Keep the control area compact:

- idle: `开始自动运行`;
- running: `暂停` and `结束并保存`;
- paused: `继续` and `结束并保存`;
- replaying: `暂停重放` and `停止重放`.

The control view continues to show current waveform and strength from existing state. Do not expose seed, random range, or decision reasoning in the normal view.

- [ ] **Step 3: Add a minimal replay view**

Extend `ViewName` with `replays`, add `历史` navigation, and render `ReplayPanel`. Each row shows title/date/DLC/event count plus `重放` and `下载`. Do not add search/rating/delete in MVP1.

- [ ] **Step 4: Run TypeScript and production build**

Run: `npm --prefix frontend run build`

Expected: TypeScript completes with zero errors and Vite emits `frontend/dist`.

- [ ] **Step 5: Commit the UI**

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/components/TopBar.tsx frontend/src/components/ChatPanel.tsx frontend/src/components/ReplayPanel.tsx
git commit -m "feat: add timeline session and replay controls"
```

### Task 8: Full verification, documentation, and MVP1 release gate

**Files:**
- Modify: `README.md`
- Create: `tests/test_mvp1_timeline_integration.py`

**Interfaces:**
- Consumes: all MVP1 services.
- Produces: one deterministic dry-run proof and operator documentation.

- [ ] **Step 1: Write an end-to-end dry-run test**

The test uses a fixed seed, fake LLM actions, fake relay, and temporary replay directory. It starts a live session, processes three turns, finishes, loads the archive, replays it, and asserts identical requested timelines plus zero real relay frames.

```python
class MVP1IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_live_session_round_trips_to_exact_replay(self):
        harness = await TimelineHarness.create(seed=20260831, dry_run=True)
        await harness.start()
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await harness.turn([{"op": "hold_strength", "channel": "B", "value": 12}])
        await harness.turn([{"op": "clear", "channel": "A"}])
        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)
        result = await harness.replay(replay)
        self.assertEqual(result.requested_timeline, replay.timeline)
        self.assertFalse(result.adjusted)
        self.assertEqual(harness.relay.frames, [])
```

- [ ] **Step 2: Document operation and storage**

Update `README.md` with session controls, `.coyote-replay` location, exact-versus-adjusted semantics, pause clearing, dry-run procedure, and the statement that replay never raises current safety caps.

- [ ] **Step 3: Run the complete backend verification**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`

Run: `.venv\Scripts\python.exe -m compileall -q backend tests`

Expected: all automated backend tests PASS and compileall exits 0.

- [ ] **Step 4: Run the clean frontend verification**

Run: `npm --prefix frontend ci`

Run: `npm --prefix frontend run build`

Expected: install audit reports no unresolved high/critical vulnerability and the production build exits 0.

- [ ] **Step 5: Perform manual dry-run acceptance**

With `dry_run: true` and runtime caps at or below 40:

1. start automatic play;
2. confirm A/B waveform and strength vary while remaining within resolved bounds;
3. pause and confirm both channels report zero;
4. resume and finish;
5. replay the saved archive and confirm the same requested event sequence;
6. lower one runtime cap and confirm replay is marked adjusted.

- [ ] **Step 6: Commit verified MVP1 documentation and integration test**

```bash
git add README.md tests/test_mvp1_timeline_integration.py
git commit -m "test: verify randomized timeline replay flow"
git push -u origin codex/mvp1-randomized-timeline-replay
```

- [ ] **Step 7: Run the real-device release gate**

After the user explicitly starts the real-device acceptance session, run one short completed session with cap 40 or lower, verify pause/estop clearing and exact replay, then create the annotated tag:

```bash
git tag -a mvp1-randomized-timeline-replay -m "MVP1 randomized timeline and exact replay accepted"
git push origin mvp1-randomized-timeline-replay
```

Do not create the tag before the real-device acceptance evidence exists.
