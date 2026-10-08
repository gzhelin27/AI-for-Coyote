# Video ownership pause: feedback retry design

Date: 2026-10-08

## Scope and authorization

The user requested Astra diagnosis, a spec/plan, then a Sol task implementing the fix. This is a focused concurrency correction to the accepted local-video path. The authorized workflow supplies the design-to-implementation handoff; do not ask again for routine repair approval. No new product phase, media feature, playback policy, or frontend behavior is included.

Baseline: `e03f317`, branch `codex/video-local-playback`, with the existing integrated working-tree changes. The tree contains 29 inherited modified tracked files and unrelated untracked work. Never stage or discard them wholesale. `backend/game_loop.py` already contains the integration patch from `docs/superpowers/deliveries/2026-09-22-video-clear-ownership-integration.patch`; clean HEAD alone is not the runtime baseline.

## Evidence and confidence

Confirmed code defect, reproduced without a device or network:

1. A playing video transitions from A=20/B=20 to A=0/B=30.
2. `GameLoopVideoOutput.clear(('A',))` calls `preempt`, establishing pending A clear, generation A=4, B=3. Its OutputOwnership token is current.
3. Before the video clear gets the action lock, an ordinary `slots.patch` reports A=20/B=20 with unchanged policy.
4. `update_device_state` -> `_reconcile_device_report_channel` sees `pending.clear_required` and calls `_reconcile_channel_safety_locked` -> `_run_coordinated_safety` -> `DeviceOutputCoordinator.run(CLEAR_OR_DISABLE)`.
5. That path calls `_prepare_safety_intent` again, advancing A generation from 4 to 5 without the video's token. Policy epochs remain zero. This is servicing the existing clear, not a new external command.
6. The token retires; `VideoSession.tick()` emits `输出控制权已改变` and pauses. Acknowledged fake-phone clear cannot repair the retired token, as intended by its fail-closed contract.

Local, uncommitted reproductions and evidence:

- `work/video-owner-oct08-probe.py` / `.log`: direct report, deterministic barrier immediately before video clear execution. Expected playing, actual paused.
- `work/video-owner-oct08-relay-probe.py` / `.log`: actual RelayClient event decoding and event callback, TickPhone as its in-memory websocket, temporary busy action lock, real clear/reset RPC ACK handling. `dry_run=False` applies only to the temporary fake harness. No sockets are opened. Same failure, with fake commands clear A, delta -20, reset A.
- `work/video-owner-oct08-baseline.log`: 24 tests in `test_video_clear_ownership`, `test_output_ownership`, `test_video_direct_strength` pass before this repair. Those tests do not deliver ordinary feedback in the pending-clear window.

Production correlation, not proof of its exact triggering interleaving:

- Read-only `D:/AI-for-Coyote/logs/app.log` at 19:00:40 records ownership loss at epoch 2, sequence 318, position 186130 ms, observation age 63 ms, followed by `输出清零未确认`. Subsequent resume attempts fail ownership near the same position.
- 19:01:08 has a clear-confirmation failure in a new session; ownership errors follow on resume. This is a distinct first error, not evidence that every interruption starts with ownership loss.
- Current workspace and deployed normalized contents agree for coordinator, GameLoop, video output/session/API and video clock/player files (checked by the coordinating task). This excludes an obvious disk-copy omission, not all process/runtime differences.
- Existing production logs do not record the invalidation caller, report ordering or the failed transport receipt. Therefore the race is a proven defect matching the symptom, but the original production first cause remains unproven. The implementation must preserve useful first-failure diagnostics.

Repeated ownership errors after the first retirement are an expected consequence of permanent retirement. Do not fix them by reclaiming after a foreign change or automatically resuming the video.

## Options and decision

1. Recommended: conditional retry of an already established pending clear inside DeviceOutputCoordinator. Separate retry from creation of new safety intent. It needs no video token and corrects duplicate intent creation at its source.
2. Pass the video's token to report callbacks. Rejected: report tasks may contain real external policy changes; ambient/shared ownership could falsely adopt those changes and is unsafe across awaits.
3. Ignore ownership while clearing or suppress feedback reconciliation. Rejected: these hide real takeover, allow unsafe continuation, or strand failed clear work.

## Interfaces and behavior

Add `PendingClearRetryResult` and:

```python
async def retry_pending_clear(
    self, channel: str, operation: _ChannelOperation
) -> PendingClearRetryResult:
    ...
```

Result fields: `status` is `not_pending`, `superseded`, or `attempted`; `outcome` is `TransportOutcome | None`. Only `attempted` requires an outcome. `superseded` may carry the physical transport outcome if a new intent intervened during its await. This internal API does not grant ownership or initiate a new clear.

The method captures the existing generation and normal-policy epoch, then uses the channel lock and coordinator cancellation shielding. Under the lock:

- A changed generation or policy means `superseded`; do not invoke the operation, interrupt ACK waiters, or change priority/pending/confirmed state.
- With unchanged identity but no pending clear, return `not_pending`; do not invoke transport or report a new clear success.
- For the same pending clear, preserve its generation/epoch and priority, apply the existing rejection checks, and execute the supplied operation. Never call `_prepare_safety_intent` or `invalidate` for this retry.
- Successful transport uses normal complete-clear validation and publication. Failed or cancelled transport leaves pending work retryable; shielding retains the established coordinator cleanup semantics.
- If a different generation/policy appears while transport awaits, the physical result may update confirmed output, but must not consume the newer pending safety work, lower its priority, or adopt its identity. Return `superseded`. Save and restore the newer pending state around any existing commit helper if needed. Old video ownership stays retired.
- Concurrent retries of the same pending clear serialize: the first successful retry consumes that pending request, later waiters return `not_pending` without sending commands or interrupting newer work.

Generation plus policy epoch identify the pending request: `require_clear`, safety preparation and genuine new policy transitions already advance these values. Do not rely on object identity of an immutable PendingSafetyWork snapshot, which also changes on unrelated reduction bookkeeping.

In GameLoop, only device-feedback reconciliation opts into this conditional retry. Extend `_reconcile_channel_safety_locked(channel, *, retry_existing_clear=False)` and call it with true from `_reconcile_device_report_channel`. The regular runtime-safety, operator clear/disable, estop and cap-change paths retain their existing explicit intent creation.

Provide a small `_retry_coordinated_clear(channel, operation)` wrapper that publishes coordinator state in `finally`. Its ACK interruption and loop cancellation occur inside the guarded operation, after the lock/identity/pending checks. Do not call `_run_coordinated_safety` first: its eager interruption and new preparation are the defect. Do not pass `_clear_ownership_context` or a global video token to report work.

For `not_pending`, re-read coordinator state without recording a fabricated successful clear. For `superseded`, stop this stale reconciliation attempt and leave new pending work to its owner or the next reconciliation; never turn it into a claim. For `attempted`, use existing outcome/error handling, and publish safety mirrors only from confirmed coordinator state. A failed retry must remain available to subsequent feedback or the existing safety retry entry point.

A true over-cap report and a lower effective policy continue to advance generation/policy before clear retry. Explicit external actions continue to retire the video's token even if they occur inside the same pending window.

## Diagnostics

Keep warning logs event-based, not per watchdog tick. On the first ownership interruption, log a bounded channel snapshot: current generation/policy, expected video generation or clear-token expectation, whether that token is retired, and pending clear/reduction. Expose an observation-only `OutputOwnership.diagnostics()` helper rather than using a mutable token in callbacks. Log the original bounded receipt error when VideoSession catches output/clear failure, with the already present epoch/sequence/position/observation age. Do not include media paths, CSV rows, novel content, client identifiers, request IDs, or configuration/secrets. Do not replace the existing user-facing Chinese error messages with implementation detail.

## Global constraints

- Runtime output ceiling remains 40 per channel in ignored local configuration; committed code must not raise it automatically.
- Video lease remains 1.0 seconds; lease loss stops output and does not auto-resume.
- Emergency stop, disconnect, overheat, reduced caps, channel disable and external output ownership always take priority.
- No ambient ownership token, automatic reclaim after retirement, blanket ownership bypass or permanent suppression of safety retries.
- Diagnosis and automated tests use temporary configuration and FakeRelay/TickPhone only; never target production ports 8000/9998 or switch production dry_run.
- Do not run tests/probe_llm.py; it performs paid external requests.
- Do not commit logs, private media/content, runtime configuration or inherited unrelated edits.

## Acceptance

1. Both existing reproduction mechanisms remain playing with one epoch, no ownership warning, correct A/B output and no pending clear after completion, in mirror A/B variants and full-gap clearing.
2. Real RelayClient slots.patch with delayed/mixed ACK timing exercises the report retry. Repeated safe reports do not change generation/policy. A queued retry that becomes unnecessary emits no transport, ACK interruption or fake success action.
3. Two concurrent reports coalesce; failed transport retains pending; a later successful retry clears it without pretending another owner took over.
4. External clear, takeover, estop, disable, cap reduction and genuine over-cap/overheat reports before locking and during ACK wait retire the old video and block its later normal commands. New pending work survives an older retry's late completion.
5. Existing pause, seek, disconnect, 1-second lease and clear-confirmation behavior pass. Retired sessions do not auto-recover.
6. Full Python suite, syntax check, frontend existing tests/build, and an offline synthetic multi-minute browser playback with report feedback pass before any manual real-device acceptance.
7. Deployment is a separately coordinated final action after dry-run verification. Compare source hashes with the tested integrated baseline, preserve ignored settings and caps, confirm idle A/B zero and no active session, then perform user-authorized deployment and manual real-device acceptance. Never claim a real-device fix from fake-device evidence alone.
