# Task 7 Report: FastAPI integration and automatic-mode wiring

## Status

Complete. The implementation is committed as
`a6c1934f41a7cb628071ecfff2c83af29820937d`
(`feat: expose cycle-aware sessions and replays`).

## Files changed

- `backend/game_loop.py`
- `backend/main.py`
- `backend/timeline/cycle_runner.py`
- `backend/timeline/session.py`
- `tests/test_game_loop_timeline.py`
- `tests/test_session_endpoints.py`

The two timeline-module edits are focused integration extensions. The runner now
reports the next start derived from its already-sampled gap, avoiding duplicate
scheduling or RNG work in the web layer. The session edit fixes the explicitly
authorized finish-cancellation safety race and prevents an already-active estop
from being archived as a normal completion.

## RED evidence

All production changes followed failing behavior tests.

1. Initial GameLoop integration RED:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline -v
   ```

   The first corrected fixture run produced three failures: the live cursor
   remained `0` instead of `1`, the runner never reached its generated gap, and
   the required session/runner state was absent. Separate `auto_open`, observation,
   and user-turn tests also each observed cursor `0` instead of `1`.

2. Initial endpoint and cancellation RED:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints -v
   ```

   Ten route tests received `404` because the session/replay routes did not yet
   exist. The deferred Task 6 regression also failed because cancellation left
   the controller in `FINISHING` instead of retryable `PAUSED` state.

3. Safety and transition REDs added during integration review:

   - paused-session AI fallback: `AssertionError: False is not true` because AI
     actions fell through to direct execution;
   - disconnect with the legacy toggle disabled: `RUNNING != PAUSED`;
   - atomic resume/finish transition: `AttributeError` for the missing
     `resume_timeline_session` operation;
   - estop racing finish: finish returned `200` instead of `409` and would have
     saved a completed archive;
   - automatic sensor lifecycle: observed `[]` instead of
     `[call(True), call(False), call(True), call(False)]`, with history/profile,
     estop, and disconnect paths likewise reporting the expected sensor-stop call
     was never awaited.

Each RED was run before its corresponding production change and then rerun GREEN.

## Implementation

- Constructed and attached the existing `ReplayStore` and `SessionController` in
  `AppState`, using the validated project timeline policy and a cryptographic
  process seed. No scheduling, randomization, replay, or persistence logic was
  reimplemented in FastAPI or GameLoop.
- Routed every live AI action source (automatic turn, automatic opening,
  observation, and user chat while automatic mode is active) through
  `SessionController.process_live_turn()`. Paused live sessions and replay mode
  consume/block AI device actions instead of falling back to direct execution.
- Kept `_autopilot_loop`'s only wait at exactly `self.autopilot_interval`.
  Cycle/gap completion remains internal to the runner and never calls the LLM.
  Manual `pulse` and `pulse_hold` still call the direct GameLoop executor.
- Serialized start/resume/pause/finish/estop transitions around the automatic task
  so conflicting requests cannot restart a session after it was finished.
- Implemented all ten session/replay routes, domain error mapping to 400/404/409,
  replay list/playback controls, and validated archive download with a contained
  resolved path and replay-ID-derived safe filename.
- Preserved lifecycle semantics: automatic off pauses and clears without saving;
  history clear and role/profile change finish first; disconnect always pauses and
  clears without saving; estop aborts without resetting the estop latch; automatic
  sensor start/stop follows every new session lifecycle path.
- Added safe HTTP/WebSocket state with allowlisted session fields and per-channel
  phase, pattern, strength, cycle index, and already-scheduled next-cycle start.
  Removed raw waveform frames and filesystem configuration paths from public
  state. Tests also reject source/RNG/API-key fields and secret fixture values.
- Fixed `SessionController.finish()` so pending clear is marked before its first
  cancellation point, cancellation rolls back to retryable `PAUSED`, and estop is
  rechecked before persistence.
- Added isolated ASGI and GameLoop integration tests backed only by temporary
  replay directories, controlled clocks/RNGs, fake relay/sensors, and fake LLMs.
  No test uses a real network, device, or paid API.

## Exact verification

Baseline before implementation:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

```text
Ran 131 tests in 4.235s
OK
```

Final focused Task 7 suites:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints -v
```

```text
Ran 23 tests in 1.524s
OK
```

Final directly impacted suites with asyncio debug enabled:

```powershell
$env:PYTHONASYNCIODEBUG = '1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_timeline_session tests.test_timeline_player tests.test_cycle_runner tests.test_game_loop_cycle tests.test_replay_store tests.test_timeline_randomizer tests.test_timeline_models -v
Remove-Item Env:PYTHONASYNCIODEBUG
```

```text
Ran 149 tests in 10.067s
OK
```

Final full non-probe suite:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

```text
Ran 154 tests in 9.956s
OK
```

Compile and diff validation:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests
git diff --check
```

Both exited `0`; compileall was silent. `git diff --check` reported only the
repository's existing Windows LF-to-CRLF checkout warnings, with no whitespace
errors. Per task scope, no frontend Node command was run.

## Commit

- `a6c1934f41a7cb628071ecfff2c83af29820937d` —
  `feat: expose cycle-aware sessions and replays`

## Self-review

- Verified `autopilot_interval` is asserted behaviorally as the exact wait timeout
  and that no timeline interval helper was introduced.
- Verified plot resolution is called once while cycle-gap RNG advances separately;
  completing a cycle does not call the fake LLM.
- Verified public session and runner dictionaries use exact allowlists and public
  state contains no raw frames, source text, RNG metadata/seed, API key, or local
  filesystem path. Replay summaries retain their domain-defined seed field, but it
  is not exposed in `/api/state` or WebSocket state.
- Verified manual controls bypass the session cursor/runners, while every AI source
  uses the live controller and paused/replay modes cannot leak direct actions.
- Verified automatic off, disconnect, estop, normal finish, role/profile switch,
  and history clear each have archive/output assertions, including cancellation
  and estop/finish concurrency.
- Verified download validation delegates ID/archive validation to the existing
  store and separately enforces final-path containment and a safe response name.
- Reviewed the committed diff against base `702d4cdb45c8909cce64f2599c228197c3b6a7fe`;
  no configuration, generated replay, frontend, or unrelated user file changed.

## Concerns

- No unresolved backend code concern. Task 8's proposed TypeScript model uses
  camelCase/nested channel fields while the backend intentionally preserves the
  existing domain/API snake_case serialization; the frontend task should make that
  mapping/type decision explicitly.
- Real-relay/device acceptance remains manual by project policy. This task was
  deliberately verified only with fake relay/device/LLM components and dry-run
  execution, as required.

---

## Fix round 1 (2026-08-31)

### Status

Complete. All Critical and Important findings from Task 7 review are fixed in
`716afad50c279d113601a92c73352bb1b415a959`
(`fix: harden timeline session integration`). The original Task 7 commits were
preserved unchanged.

### Files changed

- `backend/game_loop.py`
- `backend/main.py`
- `backend/timeline/replay_store.py`
- `backend/timeline/session.py`
- `tests/test_app_state_timeline.py` (new)
- `tests/test_game_loop_timeline.py`
- `tests/test_replay_store.py`
- `tests/test_session_endpoints.py`

This report was appended after the implementation commit; no prior report text
or commit history was rewritten.

### RED evidence

Each reviewed defect was first reproduced at its public behavior boundary.

1. Late AI results and wrapper cancellation:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline.GameLoopTimelineTests.test_user_turn_started_live_is_discarded_after_finish tests.test_game_loop_timeline.GameLoopTimelineTests.test_auto_open_started_live_is_discarded_after_finish tests.test_game_loop_timeline.GameLoopTimelineTests.test_observation_started_live_is_discarded_after_finish tests.test_game_loop_timeline.GameLoopTimelineTests.test_autopilot_turn_started_live_is_discarded_after_finish tests.test_game_loop_timeline.GameLoopTimelineTests.test_user_turn_started_during_replay_is_discarded_after_stop tests.test_game_loop_timeline.GameLoopTimelineTests.test_cancelled_automatic_off_has_already_paused_and_cleared tests.test_game_loop_timeline.GameLoopTimelineTests.test_cancelled_finish_wrapper_has_already_finished_and_cleared -v
   ```

   ```text
   Ran 7 tests in 0.415s
   FAILED (failures=7)
   ```

   All five delayed model results directly executed the stale strength action.
   The two cancelled wrappers left the controller `RUNNING` instead of already
   `PAUSED`/`IDLE` with output cleared.

2. Transition serialization through sensor work and mutation:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_start_response_and_sensors_are_atomic_against_finish tests.test_session_endpoints.SessionEndpointTests.test_history_finish_sensor_and_mutation_block_a_new_start tests.test_session_endpoints.SessionEndpointTests.test_profile_reload_finish_sensor_and_save_block_a_new_start -v
   ```

   ```text
   Ran 3 tests in 0.298s
   FAILED (failures=3)
   ```

   The start response observed the later `finishing` state, and concurrent start
   requests completed inside both finish-to-history and finish-to-profile gaps.

3. Real production object construction, provenance, and shutdown:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_app_state_timeline -v
   ```

   ```text
   Ran 2 tests in 0.129s
   FAILED (failures=2)
   ```

   Two live sessions stored the same process-start seed (`101 == 101`), and the
   real lifespan left replay status `replaying` after shutdown when legacy
   `auto_clear_on_disconnect` was false.

4. Download validation/open race:

   ```powershell
   D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_download_streams_the_validated_open_archive_not_swapped_path -v
   ```

   ```text
   Ran 1 test in 0.089s
   FAILED (failures=1)
   ```

   The response contained `SWAPPED_AFTER_VALIDATION` rather than the bytes that
   had passed replay validation.

All commands above were rerun GREEN after their corresponding minimal production
change. Fixtures use temporary replay roots, controlled clocks, fake sensors,
relay, and LLM only.

### Implementation

- Added an internal routing generation to `SessionController`. Every eligibility
  transition increments it; all four AI entry points capture controller, mode,
  session ID, and generation before awaiting the LLM. Live submission rechecks
  the expected generation under the controller lock, while stale live, replay,
  or pre-session results are consumed with no direct actuation and no channel
  floor.
- Reordered GameLoop pause, finish, abnormal stop, disconnect, and estop wrappers
  so controller cleanup/output blocking is established before cancellation-prone
  automatic-task gathering. Production wrapper cancellation regressions assert
  cleared output and correct archive semantics.
- Added one AppState transition lock around lifecycle work, required sensor side
  effects, response snapshots, history mutation, and role/profile reload/save.
  E-stop still activates physically before waiting on bookkeeping locks, so it
  preempts an in-flight normal finish and prevents an archive.
- Added `GameLoop.stop_timeline_session()` and made real application shutdown stop
  both live and replay work unconditionally, clear output, and never save an
  incomplete archive. Shutdown also awaits cancelled background tasks.
- Added per-new-live-session seed and metadata factories. Production seeds come
  from `secrets.randbits`; model, role, profile, DLC version, and app version are
  snapshotted when each session begins. Pause/resume does not reseed, and replay
  continues to use the archive's recorded seed/schedule.
- Refactored replay loading so download validation reads the same opened handle
  that is streamed. Containment, regular-file type, pre-open versus opened file
  identity, archive schema, checksums, and replay ID are validated before the
  handle is rewound. `StreamingResponse` owns that handle through completion and
  closes it via both iterator finalization and a background cleanup. A safe
  replay-ID-derived filename and `application/zip` are retained.
- Added actual `AppState` constructor and FastAPI lifespan tests with a temporary
  replay root and fake external dependencies. No real device, network, or paid
  model call is made.

### Exact verification

Focused GameLoop suite:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline -v
```

```text
Ran 17 tests in 0.959s
OK
```

Directly impacted timeline/API suites:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_timeline_models tests.test_timeline_randomizer tests.test_cycle_runner tests.test_timeline_player tests.test_replay_store tests.test_timeline_session tests.test_game_loop_timeline tests.test_session_endpoints tests.test_app_state_timeline -v
```

```text
Ran 147 tests in 11.665s
OK
```

Final full non-probe suite:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -p test_*.py -v
```

```text
Ran 168 tests in 12.173s
OK
```

Final safety/concurrency suites with asyncio debug enabled:

```powershell
$env:PYTHONASYNCIODEBUG = '1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_app_state_timeline tests.test_timeline_session tests.test_timeline_player tests.test_cycle_runner tests.test_game_loop_cycle tests.test_replay_store -v
```

```text
Ran 148 tests in 12.280s
OK
```

Compile and diff checks:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests
git diff --check
```

Both exited `0`. Compileall was silent. `git diff --check` reported only the
repository's Windows LF-to-CRLF checkout warnings and no whitespace errors.

### Commit

- `716afad50c279d113601a92c73352bb1b415a959` —
  `fix: harden timeline session integration`

### Self-review

- Confirmed the routing generation is internal only and adds nothing to HTTP or
  WebSocket state. Existing exact redaction allowlists still pass.
- Confirmed every model-producing path captures origin before its LLM await, and
  controller-lock revalidation closes the check-to-submit race.
- Confirmed automatic off remains pause-and-clear without persistence; normal
  finish still archives; abnormal stop, disconnect, estop, and shutdown do not.
- Confirmed route locking spans sensor awaits and response/mutation reads. The
  concurrent estop regression still proves physical estop occurs while normal
  finish is quiescing, and finish returns `409` without saving.
- Confirmed replay download streams the already-opened validated object. In
  addition to the route fake swap, a Windows-safe deterministic store test
  replaces the path between `stat` and `open` and verifies identity rejection.
- Confirmed each production live start receives a different seed without tests
  asserting real nondeterministic values; metadata changes appear only in the
  next session manifest. Exact replay remains RNG-free by the existing player
  test.
- Rechecked cadence invariants: `autopilot_interval` remains the sole automatic
  turn wait; cycle/gap progression never calls the LLM; resolver and per-channel
  cycle RNG calls remain separate; manual pulse controls bypass session timing.

### Concerns

- No unresolved backend code concern. Holding the transition lock across sensor
  start/stop is intentional so API responses cannot observe a later transition;
  a slow sensor driver can therefore delay another normal lifecycle request.
- Real relay/device acceptance remains manual by project policy. All automated
  verification in this round used fakes and dry-run execution as required.
