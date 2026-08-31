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
