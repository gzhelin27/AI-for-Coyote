# Video CSV Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for implementation and superpowers:verification-before-completion before completion claims. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Play a local video with a validated per-video intensity CSV, deterministic random 30-second waveform blocks, and conditional strength ramps through the existing safety path.

**Architecture:** The browser owns media position; a backend video session owns output generations and a bounded playback lease. Pure CSV, waveform-plan, and ramp-policy modules are separated from a GameLoop adapter and the media-session state machine. CSV targets, capped targets, and confirmed strengths remain separate values.

**Tech Stack:** Python 3.12, FastAPI, existing GameLoop/SafetyManager/output coordinator, React 19, TypeScript, native HTML video, WebSocket, Python unittest and Node test runner. No model calls or video-analysis dependency.

**Spec:** `docs/superpowers/specs/2026-09-15-video-csv-playback-design.md`, confirmed in commits `40f2693` and `4c9d6ba`.

## Global Constraints

- CSV header is exactly `start_time,end_time,A_target,B_target`; time uses `H:MM:SS`, is relative to the start of the video, and is converted to integer milliseconds.
- Intervals are half-open `[start, end)`. Every uncovered time produces zero on both channels.
- A/B choose independently from allowed waveform presets. Resolve all choices before playback, preserve choices across seeks in that session, and exclude the immediately previous waveform when alternatives exist.
- Each CSV interval anchors its own 30-second blocks; truncate its final block. Replace queued frames at the boundary without resetting confirmed strength.
- Video mode loops frames continuously with no novel-mode random cycle gaps. Existing novel/autopilot policies remain unchanged.
- Clip CSV targets to current effective hard caps. Never increase local hard caps; current per-channel 40 is user-owned configuration, not a new code default.
- A new upward target within the single-step limit is immediate. A larger jump first uses the allowed step, then rises by one every two real seconds after confirmed increases. Equal targets do not restart that ramp; decreases are immediate.
- During an existing ramp, a different target within the permitted jump is immediate; a still-excessive target continues the existing +1 cadence without another initial jump.
- Ramp time is monotonic real time, not video time. Delayed ticks never catch up multiple increments.
- Pause, seek, waiting, error, end, source change, connection loss, and expired playback lease cancel increases and clear output. Old messages/ACKs cannot restore retired state.
- First release supports only 1x synchronized playback and browser-decodable local media. No model analysis, subtitle analysis, transcoding, URL capture, editor, or exact intra-waveform phase reconstruction.
- Never start or stop the mother checkout's service, connect a real relay, change its configuration, or use private imported content for tests.
- Validate output through the existing safety layer. Transport confirmation precedes publishing effective state.

## Entry gate and workspace evidence

Implementation is gated by the existing AGENTS.md release rule: the preceding phase must pass its release gate and be accepted by the user. The source conversation states that current novel-mode real-device testing will be completed later; no completed acceptance record is inherited. This plan and baseline inspection are preparation, not an assertion that the gate passed.

- Worktree: `C:/Users/gzhel/.codex/worktrees/c3b6/AI-for-Coyote`.
- Preparation branch: `codex/video-csv-development-plan`, created from inherited HEAD `4c9d6ba`.
- The fork inherited 29 modified tracked files plus untracked novel/preparation/PPTX files. Do not stage them as part of this plan or discard them.
- On 2026-09-15, the isolated baseline command `unittest tests.test_manual_next_output tests.test_relay_acknowledgement tests.test_story_adjustments tests.test_story_adjustment_endpoints -q` passed 198 tests in 70.318 seconds. This verifies selected inherited behavior, not new video functionality or the full release gate.
- On 2026-09-15, `tests.test_session_endpoints` ran 73 tests with 72 errors before video implementation. Setup calls `make_app()` with an AppState fixture missing `story_import_directory`. Record this baseline accurately; repair the fixture in a separate prerequisite change before expecting the full gate to pass.
- No accepted phase-specific tag was found in the inherited tag list. After user acceptance, establish the reviewed integration commit/tag containing required current fixes and create `codex/phase4-video-csv` from that base. Do not assume HEAD alone includes the inherited uncommitted fixes.

Commands below run from the isolated worktree. The mother virtual environment may supply dependencies without changing its working directory or configuration:

```powershell
$videoPython = 'D:/AI-for-Coyote/.venv/Scripts/python.exe'
$env:PATH = 'D:/AI-for-Coyote/.runtime/node;' + $env:PATH
```

## File map and contracts

New backend modules:

- `backend/video/models.py`: immutable `VideoInterval`, `VideoCsv`, `VideoBlock`, `VideoPlan`, `VideoObservation`, `VideoState`.
- `backend/video/csv_timeline.py`: parsing and interval lookup; no I/O or device access.
- `backend/video/waveforms.py`: deterministic block resolution; no device access.
- `backend/video/ramp.py`: pure conditional-ramp policy and confirmation bookkeeping.
- `backend/video/output.py`: video output adapter using GameLoop, confirmed state, and owner generations.
- `backend/video/session.py`: media-clock/lease state machine and asynchronous output ownership.
- `backend/video/source.py`: bounded local video/CSV persistence, SHA-256 identity, and opaque storage IDs.
- `backend/video/api.py`: router and WebSocket message validation; dependency-injected state, no second global AppState.

New frontend modules: `videoTypes.ts`, `videoApi.ts`, `videoClock.ts`, `components/VideoPlayer.tsx`. Existing `App.tsx`, `api.ts`, `types.ts`, `store.ts`, and `backend/main.py` receive only integration changes. Add video-specific tests rather than expanding large novel fixtures with unrelated details.

Core public signatures:

```python
parse_video_csv(payload: bytes, *, duration_ms: int) -> VideoCsv
find_interval(timeline: VideoCsv, position_ms: int) -> VideoInterval | None
resolve_video_plan(timeline: VideoCsv, *, allowed: tuple[str, ...], library_sha256: str, seed: int) -> VideoPlan
find_block(plan: VideoPlan, position_ms: int) -> VideoBlock | None
RampPolicy.propose(*, requested: int, confirmed: int, cap: int, max_step: int, now_s: float) -> int | None
RampPolicy.confirm(*, requested: int, actual: int, now_s: float) -> None
RampPolicy.cancel() -> None
VideoOutputPort.snapshot(channel: str) -> OutputSnapshot
VideoOutputPort.set_strength(channel: str, target: int, generation: int) -> OutputReceipt
VideoOutputPort.replace_block(channel: str, pattern: str, remaining_ms: int, generation: int) -> OutputReceipt
VideoOutputPort.clear(channels: tuple[str, ...]) -> None
VideoSession.observe(observation: VideoObservation) -> VideoState
VideoSession.tick() -> VideoState
VideoSession.close(reason: str) -> VideoState
```

The three output operations and session operations are async. `OutputSnapshot` exposes confirmed strength, effective cap, max step, enabled and pending-safety flags. `OutputReceipt` exposes success, confirmed strength, simulated flag, and error. Failed receipts never count as confirmation. A `VideoObservation` contains session ID, epoch, strictly increasing sequence, position_ms, and state (`playing`, `paused`, `seeking`, `waiting`, `ended`, `error`); only 1x rate is accepted.

## Task 1: Parse and query CSV intervals

**Files:** Create `backend/video/__init__.py`, `models.py`, `csv_timeline.py`, `tests/test_video_csv.py`.

**Interfaces:** `VideoInterval(row_id: str, start_ms: int, end_ms: int, a_target: int, b_target: int)`; `VideoCsv(intervals: tuple[VideoInterval, ...], sha256: str, duration_ms: int)`. Parse at most 16 MiB and 100,000 rows; reject excessive input before constructing the index. Canonical SHA-256 covers sorted normalized rows, not incidental CSV whitespace.

- [ ] Write the failing interval test and malformed-input table.

```python
def test_absolute_time_and_uncovered_time(self):
    data = b'start_time,end_time,A_target,B_target\n0:10:20,0:11:30,20,15\n'
    timeline = parse_video_csv(data, duration_ms=720000)
    row = timeline.intervals[0]
    self.assertEqual((row.start_ms, row.end_ms), (620000, 690000))
    self.assertIsNone(find_interval(timeline, 619999))
    self.assertEqual(find_interval(timeline, 620000), row)
    self.assertIsNone(find_interval(timeline, 690000))
```

- [ ] Run `& $videoPython -m unittest tests.test_video_csv -v`; verify failure from missing implementation.
- [ ] Implement strict time parsing with `re.fullmatch(r'[0-9]+:[0-5][0-9]:[0-5][0-9]', text)`, strict four-column CSV decoding with UTF-8 BOM support, integer targets 0–200, and sorted non-overlapping bounds. Check video duration is finite, positive integer milliseconds before parsing. Reject rows beyond it. Use binary search and check `start <= position < end`; negative/out-of-video positions return no interval.
- [ ] Test BOM, unsorted rows, adjacent boundaries, duplicate/overlapping ranges, malformed time/targets, too many rows, oversized bytes, missing columns, zero/negative duration, and output-zero gaps.
- [ ] Run focused tests, then commit only these files as `feat: parse video intensity timelines`.

## Task 2: Resolve stable random 30-second blocks

**Files:** Create `backend/video/waveforms.py`, `tests/test_video_waveforms.py`; extend `models.py`.

**Interfaces:** `VideoBlock(row_id, index, start_ms, end_ms, a_pattern: str | None, b_pattern: str | None, a_target, b_target)`. `VideoPlan(timeline_sha256, seed, library_sha256, blocks: tuple[VideoBlock, ...])`. The caller supplies library identity calculated from sorted preset identities and raw-frame hashes, not names alone. Validate its SHA-256 format before resolution. Do not emit blocks for uncovered time; zero channels have `None` pattern.

- [ ] Write tests for 70-second partitioning and seed stability.

```python
def test_interval_anchor_and_truncated_last_block(self):
    timeline = parse_video_csv(
        b'start_time,end_time,A_target,B_target\n0:10:20,0:11:30,20,15\n',
        duration_ms=720000)
    plan = resolve_video_plan(timeline, allowed=('wave-a', 'wave-b'), library_sha256='a' * 64, seed=42)
    self.assertEqual([(b.start_ms, b.end_ms) for b in plan.blocks],
                     [(620000, 650000), (650000, 680000), (680000, 690000)])
    self.assertEqual(find_block(plan, 685000), plan.blocks[2])
    self.assertNotEqual(plan.blocks[0].a_pattern, plan.blocks[1].a_pattern)
```

- [ ] Run `& $videoPython -m unittest tests.test_video_waveforms -v` and verify red.
- [ ] Partition with `range(row.start_ms, row.end_ms, 30000)` and end `min(start + 30000, row.end_ms)`. Use separately derived A/B RNG seeds from canonical `(seed, channel)` hashing; choose from sorted allowed names excluding previous selected name when possible. Cap total blocks at 100,000 and fail atomically if exceeded. Preserve the resolved plan across seeks; do not re-sample in `find_block`.
- [ ] Verify one preset, empty library with positive output, zero channels, independent channel streams, same seed/library stability, changed library identity, row boundaries and no gap blocks. Commit `feat: resolve deterministic video waveform blocks`.

## Task 3: Conditional ramp policy without transport

**Files:** Create `backend/video/ramp.py`, `tests/test_video_ramp.py`.

**Interfaces:** `RampPolicy` stores requested target, whether that target is already ramping, and last confirmed upward time. `propose` is side-effect free; only `confirm` commits successful output. Caller serializes one pending proposal. `cancel` removes target/cadence state after clear.

- [ ] Write the user-confirmed cases before implementation.

```python
def test_initial_step_then_no_catch_up(self):
    ramp = RampPolicy()
    self.assertEqual(ramp.propose(requested=30, confirmed=0, cap=40, max_step=10, now_s=0), 10)
    ramp.confirm(requested=30, actual=10, now_s=0)
    self.assertIsNone(ramp.propose(requested=30, confirmed=10, cap=40, max_step=10, now_s=1.99))
    self.assertEqual(ramp.propose(requested=30, confirmed=10, cap=40, max_step=10, now_s=10), 11)

def test_permitted_jump_is_immediate(self):
    ramp = RampPolicy()
    self.assertEqual(ramp.propose(requested=30, confirmed=20, cap=40, max_step=10, now_s=0), 30)
```

- [ ] Run `& $videoPython -m unittest tests.test_video_ramp -v`; verify red.
- [ ] Implement target clipping, immediate decreases, no-op equal strength, fresh target allowed-step proposal, and ongoing ramp `confirmed + 1` only when `now_s >= last_increase_s + 2`. Store the original request so unchanged target messages cannot restart an initial jump. For changed target during an active ramp: immediate when the new difference fits max_step; otherwise retain ramp cadence. If the cap decreases below confirmed strength propose the lower cap immediately. Never infer confirmation from proposed values.
- [ ] Test 20→31, 0→30 through 40 real seconds, 30→20, target 60 capped at 40, cap reduction, same target with remaining difference 10, changed target during ramp, failed proposal without confirmation, repeated timestamps, long scheduler delay and cancel/resume. Commit `feat: track conditional video strength ramps`.

## Task 4: Safe output adapter and bounded waveform execution

**Files:** Create `backend/video/output.py`, `tests/test_video_output.py`; modify `backend/game_loop.py` and `backend/safety.py` only for a scoped video waveform command if needed.

**Interfaces:** Implement `VideoOutputPort`/`OutputSnapshot`/`OutputReceipt`. Obtain generations with `GameLoop.begin_timeline_output`; strengths go through `execute_timeline_actions`, never relay calls. Use `replace_timeline_waveform` for queue replacement and `clear_output` for physical stop. Store the owner generation on all worker callbacks.

- [ ] Write fake-relay tests using the existing real GameLoop test harness.

```python
async def test_queue_replacement_preserves_strength(self):
    self.h.safety.max_step = 10
    generation = self.h.loop.begin_timeline_output(('A',))['A']
    receipt = await self.output.set_strength('A', 10, generation)
    self.assertTrue(receipt.success)
    await self.output.replace_block('A', 'wave-a', 5000, generation)
    self.assertEqual(self.output.snapshot('A').confirmed_strength, 10)
    await self.output.clear(('A',))
    self.assertEqual(self.output.snapshot('A').confirmed_strength, 0)
```

Test setup creates a TemporaryDirectory, calls existing `make_game_loop_for_test`, registers synthetic preset `wave-a`, constructs the adapter, and clears/closes all tasks in async cleanup. Do not point the harness at a real relay.

- [ ] Run `& $videoPython -m unittest tests.test_video_output -v`; verify red.
- [ ] Add a video-scoped bounded waveform execution path. If existing `pulse_cycle` cannot bound the remaining block time, introduce an internal `pulse_video` action carrying only preset name, positive `duration_ms`, and channel. SafetyManager selects frames from its own validated preset and caps duration to at most 30,000 ms and the current block's remaining duration. The client never supplies raw frames. Repeat/truncate the known raw sequence in 100 ms frame units; do not send a frame whose full duration extends beyond the authorized remaining interval. Bound chunks so the existing global pulse limit is not bypassed. If less than one frame remains, send none and wait for the boundary/clear.
- [ ] Queue clear must be ACK-confirmed before replacement frames; each strength must be ACK-confirmed before dependent waveforms. A block timer may stop/clear but may not independently advance media time or activate a new block without a fresh playback lease. Re-check generation and safety before every worker submission. A safety stop interrupts pending ACK waits using the existing priority path.
- [ ] Prevent the legacy automatic default-wave helper from being created by a video ramp's standalone strength command. Add an internal `waveform_managed_channels: tuple[str, ...] = ()` parameter to the timeline action path and propagate it to action execution. Only the adapter supplies its currently owned video channels; it suppresses helper creation for those strength commands without suppressing safety validation, confirmation, caps or clear. Do not accept this parameter from external action JSON. Test that increasing a video's strength never substitutes the UI default waveform.
- [ ] Test no positive frames after stale generation, caps/max step, partial raw cycle at boundary, remaining 5 seconds after a seek, late ACK, helper cleanup, failed clear, estop while waiting and zero channel. Run existing manual-next and relay-ACK tests. Commit `feat: execute bounded video output through safety`.

## Task 5: Video session and playback lease

**Files:** Create `backend/video/session.py`, `tests/test_video_session.py`, `tests/video_fakes.py`.

**Interfaces:** `VideoSession(plan, output: VideoOutputPort, clock: Callable[[], float])`. `observe`, `tick`, and `close` are serialized by a session transition lock, but safety invalidation happens synchronously before awaiting output. `VideoState` includes session_id, epoch, last_sequence, status, media_position_ms, row_id/block_index and both channel target/cap/confirmed/ramp/reason fields.

- [ ] Write synthetic clock and output-port fixtures. Fake output keeps a receipt queue, records calls, blocks individual ACKs on asyncio.Event and exposes a manual real-time clock. Test the state machine without network or media decoding.

```python
async def test_seek_does_not_replay_skipped_blocks(self):
    await self.session.observe(VideoObservation(self.session_id, 1, 1, 620000, 'playing'))
    self.output.calls.clear()
    await self.session.observe(VideoObservation(self.session_id, 2, 2, 685000, 'seeking'))
    await self.session.observe(VideoObservation(self.session_id, 2, 3, 685000, 'playing'))
    self.assertEqual(self.session.state.block_index, 2)
    self.assertFalse(any(call.position_ms == 650000 for call in self.output.activations))
```

- [ ] Run `& $videoPython -m unittest tests.test_video_session -v`; verify red.
- [ ] Validate strictly increasing message sequence and matching session/epoch. Playing observation grants a 1-second lease; browser sends at most 10 position samples/second while playing. State events send immediately. Stale/invalid observations do not refresh the lease. The exact initial lease value is a bounded implementation parameter, verified under local browser integration, never an unlimited timer.
- [ ] Map observed video position directly to one interval/block. Process only actual state/block/target changes. While the lease remains valid, `tick` may attempt due +1 steps using confirmed output, but may not activate a future block based only on wall time. At the current block's predicted end, stop its worker if a fresh observation has not authorized a replacement. Thus delayed browser messages can create a short stop, not output from a guessed future segment.
- [ ] Seek/pause/wait/end/error/close cancel ramp and workers and invalidate output generations before awaiting clear. Seek completion retains epoch and checks playing state; old observations and old ACKs cannot publish state. Failures set paused/error and leave pending clear auditable. Clear both channels when leaving covered time. Source switches clear before replacing source/plan references.
- [ ] Test same-block deduplication, all 13 spec acceptance cases, lease expiry/renewal, 10-second scheduling delay, background heartbeat loss, duplicate sequence, wrong session, forward/backward seek, ACK failure, changed target while ramping and both channels independently. Commit `feat: synchronize video sessions with media observations`.

## Task 6: Local source binding and API ownership

**Files:** Create `backend/video/source.py`, `backend/video/api.py`, `tests/test_video_source.py`, `tests/test_video_endpoints.py`; modify `backend/main.py`, `backend/config.py`, `config/config.example.yaml`, `.gitignore`.

**Interfaces:** `VideoSourceStore.import_stream(original_name, chunks, duration_ms) -> VideoSource` asynchronously streams local upload chunks into an opaque store ID with SHA-256; `bind_csv(source_id, payload) -> VideoCsv` atomically saves validated association. `VideoSource` has opaque ID, original display name, content hash, bytes, duration_ms. Default source upload cap 4 GiB, configurable locally; stream in at most 1 MiB chunks, never read the full movie into memory. Browser supplies decoded duration; backend checks its bounds and treats it as client metadata, not an independently probed codec fact.

- [ ] Write endpoint tests using in-process HTTP and temporary stores, with synthetic bytes and fake decoded duration. AppState test fixtures must supply every new dependency explicitly, including existing `story_import_directory`.

```python
async def test_invalid_csv_does_not_replace_bound_plan(self):
    source = await self.client.post('/api/video/sources', files={'file': ('sample.mp4', b'fixture')},
                                    data={'duration_ms': '720000'})
    source_id = source.json()['source_id']
    valid = b'start_time,end_time,A_target,B_target\n0:10:20,0:11:30,20,15\n'
    good = await self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('plan.csv', valid)})
    bad = await self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('plan.csv', b'bad')})
    self.assertEqual(good.status_code, 200)
    self.assertEqual(bad.status_code, 422)
    self.assertEqual(self.store.bound_csv(source_id).sha256, good.json()['csv_sha256'])
```

- [ ] Run `& $videoPython -m unittest tests.test_video_source tests.test_video_endpoints -v`; verify red.
- [ ] Register a router with dependency-injected AppState. Endpoints: POST `/api/video/sources`, POST `/api/video/sources/{source_id}/csv`, POST `/api/video/sessions`, GET `/api/video/state`, POST `/api/video/sessions/{session_id}/stop`; observation messages use `type=video_clock` on a dedicated session-owned WebSocket `/api/video/sessions/{session_id}/clock`. Opening a socket alone never activates output.
- [ ] Streaming import uses sanitized display names and opaque server-owned paths; enforce limits during streaming, hash actual bytes, clean partial temporary files on cancellation, and atomically publish only completed sources. Save bound source hash/CSV hash/schema/library identity/seed locally. Never trust a client-supplied filesystem path. CSV reload pauses/clears before a validated candidate replaces live ownership; reject a saved binding whose source hash differs. Ignore `data/video/` stores in Git.
- [ ] Starting a video session must use the shared transition lock to clear/finish prior mode before claiming output; likewise novel/replay/manual takeover stops video. Stop on shutdown, WebSocket close and explicit video errors. Multiple browser tabs cannot own simultaneous sessions. Extend public state with optional video state without breaking old clients. Do not duplicate relay handling.
- [ ] Test size/cancellation/path rejection, source-hash binding, stale source ID, CSV atomic replacement, concurrent tabs, all mode takeovers, disconnect, shutdown clear failure and optional-state normalization. Commit `feat: expose local video sessions and CSV binding`.

## Task 7: Embedded player and observable output state

**Files:** Create `frontend/src/videoTypes.ts`, `videoApi.ts`, `videoClock.ts`, `components/VideoPlayer.tsx`, `frontend/tests/video-clock.test.mjs`, `frontend/tests/video-state.test.mjs`; modify `App.tsx`, `types.ts`, `api.ts`, `store.ts` and add video UI tests following existing browser-harness conventions.

**Interfaces:** Export `VideoClockBridge({send, now})` with `onMediaState(state, positionMs)`, `onVideoFrame(positionMs)`, `close()`. It tracks epoch/sequence and emits the Task 5 observation shape with session ID. `VideoPlayer` selects a File, creates/revokes its object URL for local playback, obtains loadedmetadata, uploads it through Task 6 with progress, imports CSV, and starts only on user action after both operations validate.

- [ ] Write clock bridge tests before UI integration.

```javascript
test('seeking advances epoch and a paused seek never emits playing', () => {
  const sent = [];
  const bridge = new VideoClockBridge({sessionId: 'test', send: m => sent.push(m), now: () => 0});
  bridge.onMediaState('seeking', 685000);
  bridge.onMediaState('paused', 685000);
  assert.equal(sent[0].epoch, sent[1].epoch);
  assert.equal(sent.at(-1).state, 'paused');
  assert.ok(sent[1].sequence > sent[0].sequence);
});
```

- [ ] Run `npm --prefix frontend test`; verify the new test fails for the missing module.
- [ ] Attach native `playing`, `pause`, `seeking`, `seeked`, `waiting`, `ended`, `error` listeners and `requestVideoFrameCallback` when available, with requestAnimationFrame/currentTime fallback. Playing samples are throttled to 10 Hz; transitions are immediate. On seeked, inspect paused/seeking/readyState instead of unconditionally sending playing. Enforce rate=1 for this release. Fullscreen changes do not restart output.
- [ ] Render local file/CSV selectors, video controls, timecode and active interval, and independent requested/capped/confirmed strengths and waveforms. Display ramping, cap clipping, zero gap, waiting, failed confirmation and stopped states distinctly. If backend stops or loses ownership, pause the video; do not auto-resume after reconnect. A changed file/CSV or unmount stops the prior session and revokes its object URL only after stopping its media.
- [ ] Add browser tests with synthetic short media and stubbed backend: upload failure, invalid CSV, pause/seek/wait/end, continuous scrubbing, stale HTTP result, lost socket, fullscreen, tab competition, cap explanation and 1x enforcement. Test with no device endpoint and no external model traffic. Run frontend tests and `npm --prefix frontend run build`; commit `feat: add local video playback controls`.

## Task 8: Integrated dry-run, documentation and release evidence

**Files:** Create `tests/test_video_integration.py`, `tests/video_browser_harness.py`, `docs/video-csv-playback.md`; modify test fixture setup only where the required dependency contract changed.

- [ ] Build an in-process synthetic integration fixture with real SafetyManager/GameLoop, fake relay, video session, temporary source store, fake monotonic time and deterministic seed. Assert that dry-run submits zero real relay frames.

```python
async def test_dry_run_conditional_ramp_and_gap(self):
    await self.h.play(position_ms=620000, target_a=30, target_b=0)
    self.assertEqual(self.h.confirmed(), {'A': 10, 'B': 0})
    await self.h.advance_real_and_media(seconds=2)
    self.assertEqual(self.h.confirmed(), {'A': 11, 'B': 0})
    await self.h.seek(position_ms=700000, playing=True)
    self.assertEqual(self.h.confirmed(), {'A': 0, 'B': 0})
    self.assertEqual(self.h.relay.sent_frames, [])
```

The harness methods call the real session observation/tick methods; `advance_real_and_media` sends fresh playing observations while advancing injected clocks. `seek` sends seeking then the requested final state. It does not set confirmed strength directly or skip safety execution.

- [ ] Run all video unit/integration tests, existing manual-next/relay-ACK/coordinator/novel tests, and frontend browser tests. Use 30/30/10 blocks, zero gaps, all ramp examples, slow/failed ACKs, safety cap changes, expired leases and repeated seeks.
- [ ] Run full Python unittest discovery, compileall, frontend clean install and production build. Do not invoke `tests/probe_llm.py`. Repair baseline fixture errors in their own reviewed commit before declaring the suite green; compare failures to the documented baseline rather than hiding them.
- [ ] Write user documentation containing the exact four-column CSV example, text-time formatting advice, zero gaps, per-video association, random block rules, conditional ramp examples and instructions for dry-run/manual acceptance. Record actual measured timing errors and test results; do not claim real-device success from simulation.
- [ ] Perform code review focused on safety preemption, stale ownership, partial ACK and row-boundary behavior. Fix important findings and repeat only affected checks plus required gates.
- [ ] Deliver the isolated branch and verification evidence to the user. Push only the user's origin fork if delivery requires it. Do not deploy, start output, create a release tag or mark the video phase accepted until the user completes its real-device gate.

## Plan self-review

Spec coverage: time contract and storage binding → Tasks 1/6; random 30-second resolution → Task 2; conditional ramp → Task 3; frame boundaries and safety transport → Task 4; media time/seek/lease/zero gaps → Task 5; API/mode ownership → Task 6; native player/state display → Task 7; all acceptance cases and release evidence → Task 8.

The plan intentionally leaves the running mother checkout untouched. Work possible before the entry gate is limited to this plan, source/interface inspection and baseline test evidence. It does not authorize silently waiving the preceding real-device acceptance requirement.
