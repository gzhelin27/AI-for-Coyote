# Task 3 Report: Unified live, replay, clear, and manual arbitration

Date: 2026-09-01
Base HEAD: `4e00c7ac7c2f43f5fb28d5eacf892f717468a039`
Implementation commit: `b97bca615cbc50409c54f9e4ae0d4ad7469344a5` (`fix: arbitrate all timeline and manual device output`)

## Status

Complete. Live, replay, idle manual, helper waveform, delayed revert, clear, disconnect, disable, and estop device-producing paths now use the Task 1 coordinator and Task 2 reconciliation foundation. No parallel lock, generation, confirmed-output, or pending-safety implementation was introduced.

## Root causes

1. Ordinary high-level actions sent directly through the relay, while only safety reconciliation used the coordinator. Live/replay/manual output therefore had no shared ownership or stop-class serialization.
2. Default-wave helpers mutated local state and installed a resend worker before the primary strength send was known to have succeeded. A false return or exception could leave real helper output behind while reporting the parent action as successful or incompletely failed.
3. Live and replay cleanup treated any `clear_output()` return as success. Player/session phases could publish `paused` or `idle` after a transport false/exception.
4. Live runners and replay did not carry coordinator generations. Invalidated work could reach transport through the legacy executor boundary.
5. Cycle runner executor rejection/exception paths created failed `CycleRecord` entries even though no exact committed cycle existed.
6. Global stop results did not provide complete semantic A/B mappings, and stop transport short-circuited instead of conservatively accounting for partial physical effects.
7. Cancellation shielding completed coordinator callbacks, but helper-cleanup and delayed-revert pending-clear publication initially happened after the shielded await. Caller cancellation could therefore skip the safety block even though the physical cleanup failed.

## Implementation

- Added GameLoop timeline owner-generation APIs and routed normal actions through one per-action coordinator transaction.
- Kept transport callbacks lock-safe by separating raw frame helpers from high-level coordinated entry points; no callback recursively acquires the coordinator.
- Made default waveform + primary strength one logical action. Parent success is published only after both succeed. Primary failure clears the helper; failed helper cleanup commits the truthful finite physical effect and establishes retryable pending clear inside the cancellation-shielded callback.
- Routed pulse-loop resends and temporary-strength reverts through their owning generation/revision. Stale work fails closed; failed revert creates pending clear before cancellation can resume.
- Made global clear use stable A/B coordinator acquisition, attempt all required cleanup frames, commit atomically only on complete transport success, and return complete A/B effective mappings.
- Made manual arbitration own/shield live pause or replay stop, verify confirmed global clear, then claim a manual generation. Failed/unfinished prerequisite clear sends no manual action. Idle manual behavior remains unchanged.
- Made live/replay lifecycle publish `FINISHING` while clear is pending and publish `PAUSED`/`IDLE` only after validated exact clear results. False/exception remains retryable.
- Removed executor-rejected and executor-exception cycle archival; previously completed cycles remain intact.
- Preserved Task 2 cap/floor/disable reconciliation and did not change Task 4 outbox internals.
- `backend/main.py` required no edit: its existing `/api/manual` route already calls `execute_manual_action()` under the transition lock and maps `DeviceOutputError` safely.

## RED

Baseline before Task 3 tests:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -v
```

Result: 118 tests, OK.

After adding the required arbitration/delivery/lifecycle tests and before production changes:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -v
```

Result: 126 tests, `FAILED (failures=9, errors=1)`. Failures proved clear false was published as success, manual continued after failed prerequisite clear, transport failure created cycle history, and live/replay lacked generation arbitration.

Cancellation-boundary self-review REDs:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_cancelled_failed_helper_cleanup_still_blocks_later_output -v
```

Result: 1 test, failed at the expected `pending("A").clear_required` assertion.

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_cancelled_failed_temp_revert_still_requires_clear -v
```

Result: 1 test, failed at the expected `pending("A").clear_required` assertion.

## GREEN and verification

Focused Task 3 modules:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -q
```

Result: 133 tests in 8.311s, OK.

Helper rollback cancellation and adjacent cases:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_cancelled_failed_helper_cleanup_still_blocks_later_output tests.test_session_endpoints.SessionEndpointTests.test_failed_helper_cleanup_is_confirmed_and_blocks_later_output tests.test_session_endpoints.SessionEndpointTests.test_failed_manual_primary_cleans_up_default_wave_helper tests.test_session_endpoints.SessionEndpointTests.test_manual_primary_exception_cleans_up_default_wave_helper -v
```

Result: 4 tests in 0.134s, OK.

Temporary-revert cancellation and failed-primary regression:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_session_endpoints.SessionEndpointTests.test_cancelled_failed_temp_revert_still_requires_clear tests.test_game_loop_cycle.GameLoopCycleTests.test_failed_temp_strength_does_not_schedule_a_later_revert -v
```

Result: 2 tests in 0.064s, OK.

Impacted coordinator/timeline/API suite:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_output_coordinator tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_game_loop_timeline tests.test_session_endpoints -q
```

Result: 188 tests in 10.213s, OK.

Cycle and GameLoop timeline suites:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_cycle tests.test_game_loop_timeline -q
```

Result: 76 tests in 4.712s, OK.

Asyncio debug gate:

```powershell
$env:PYTHONASYNCIODEBUG='1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -W error::RuntimeWarning -W error::ResourceWarning -W default::DeprecationWarning -m unittest tests.test_output_coordinator tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -q
Remove-Item Env:PYTHONASYNCIODEBUG
```

Result: 166 tests in 8.620s, OK; no runtime/resource/pending-task warnings.

The brief's literal `-W error` form was also attempted. It stopped during `backend.main` import on the repository's existing FastAPI `on_event` deprecation before tests loaded. Deprecation warnings were therefore left visible but not promoted, while coroutine and resource warnings remained errors in the successful command above.

Compile gate:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests
```

Result: exit 0, no output.

Full Python suite:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Result: 302 tests in 13.320s, OK.

One earlier full-suite run crossed the existing 100ms `pulse_active` expiry between the test's separate HTTP and websocket snapshots and failed only that volatile equality. Root-cause inspection confirmed no coordinator/connectivity mismatch. The exact test then passed 10/10 in 1.796s, a full rerun passed 300/300 before the two cancellation tests were added, and the final tree passed 302/302 as recorded above.

Diff gate:

```powershell
git diff --check
```

Result: exit 0; only Git's existing LF-to-CRLF checkout notices were emitted.

## Test coverage added

- real relay false and exception for live/replay primary actions, with no `CycleRecord` or archive mutation;
- helper success followed by primary false/exception, successful rollback, failed rollback, and caller cancellation during failed rollback;
- cancellation during failed delayed temporary-strength revert;
- false replay/live clear, retry, cancellation, and `FINISHING` lifecycle behavior;
- manual live/replay prerequisite failure, cancellation shielding, and idle parity;
- stale timeline generation before transport;
- estop/disable ordering and global A/B atomic clear semantics;
- complete A/B public effective stop mappings;
- executor rejection/exception never becoming cycle history.

## Self-review

- Scoped diff contains only four backend integration files and four focused test files; no Task 1 coordinator internals, Task 2 safety manager, Task 4 outbox, frontend, config, or archive schema changes.
- Remaining direct relay sends are raw transport primitives invoked only from coordinator callbacks (including floor/safety callbacks); high-level live/replay/manual/clear paths do not bypass arbitration.
- Every normal send checks generation/pending/enabled/estop immediately before transport. Delayed workers also check generation and, for temp revert, confirmed revision.
- Partial global cleanup never commits a false clear. Partial helper cleanup commits the conservative finite waveform effect and blocks lower-priority output.
- Player/session status cannot become paused/idle until exact `(executed, dropped)` clear validation succeeds.
- Public state does not expose generations, pending coordinator internals, frames, hashes, RNG state, secrets, or paths.

## Concerns / follow-up

- No implementation blocker remains.
- Real-device acceptance was not performed and remains the manual gate required by the binding design.
- The pre-existing FastAPI `on_event` deprecation prevents a repository-wide literal `-W error` import; migrating startup/shutdown to lifespan handlers is separate from Task 3.
- The existing HTTP/websocket snapshot equality test contains a real-time 100ms `pulse_active` boundary and can be timing-sensitive under a slow host; it was not weakened or changed in this task.
- The Task 1 estop coordinator latch remains a hard latch by design; Task 3 does not add an implicit or explicit coordinator-unlatch API.
