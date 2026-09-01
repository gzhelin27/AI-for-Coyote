# Output Report and Helper Lease Remediation Design

## Status

Approved design for a narrowly scoped MVP safety remediation. This spec fixes three findings from the final review of the device-output coordinator work. It does not introduce a new output subsystem, actor model, global command queue, UI, configuration surface, or device protocol.

## Problem

The current branch still has three coupled concurrency defects:

1. Helper rollback may wait for a helper worker while holding the same channel coordinator lock that the worker needs. A queued resend can therefore deadlock both the original request and a later estop.
2. A reported over-cap strength is committed before pending reduction and normal-output invalidation are established. Queued normal work can physically send in that gap.
3. An identical strength report advances helper ownership even though physical state did not change, terminating continuous manual helper output.

These defects block integration and real-device acceptance.

## Goals

- Make reported strength reconciliation atomic with safety ownership decisions.
- Make identical safe reports strict ownership no-ops.
- Prevent helper rollback from awaiting coordinator-dependent work while holding a channel lock.
- Ensure a retired helper cannot send another frame after rollback begins.
- Preserve current live, replay, manual, estop, outbox, provenance, API, and UI behavior except where required to fix these defects.

## Non-Goals

- Replacing the coordinator with actors or per-channel event queues.
- Adding a global application lock around all device operations.
- Changing waveform generation, randomization, archive formats, DLC behavior, or OpenRouter integration.
- Adding new UI controls, public state fields, configuration keys, dependencies, or protocol messages.
- Optimizing throughput beyond removing the identified deadlocks and race windows.

## Architecture

### 1. Atomic reported-strength reconciliation

`DeviceOutputCoordinator` will expose one async operation that reconciles a reported strength against the effective cap while holding the channel lock. The exact public name may follow existing naming, but its semantic inputs and result are fixed:

- Inputs: `channel`, normalized non-negative `reported_strength`, and normalized effective `cap`.
- Result: the resulting confirmed snapshot plus whether safety reduction is required.

Inside one lock acquisition it must:

1. Compare the report with confirmed strength.
2. For an identical report at or below cap, return without changing revision, main generation, normal policy epoch, helper generation, pending work, or priority.
3. For a changed report at or below cap, update confirmed strength and revision only. Existing live/replay and helper owners remain valid.
4. For a report above cap, update confirmed strength, merge `cap` into pending target strength using the strictest-target rule, invalidate queued normal work, and raise safety-reduction priority before releasing the lock.

The GameLoop will use this operation instead of separately confirming a report and later calling `mark_reduction()`. It may suspend the session and execute physical reduction after the atomic result returns, because pending safety priority already blocks normal output at that point.

Strength reports must not advance helper generation. A strength change alone does not prove that the reported waveform owner changed.

### 2. Helper lease retirement

Each continuous helper worker already captures coordinator ownership values. The minimal extension is an explicit helper lease/epoch check whose invalidation is synchronous and does not require awaiting the worker.

Rollback of a helper-assisted strength transaction must follow this order while the parent coordinator transaction owns the channel:

1. Invalidate the helper lease and set its stop event.
2. Do not await the helper worker.
3. Send the channel clear/reset frames.
4. On confirmed clear, commit strength zero and no waveform.
5. On failed or cancelled clear, establish `pending.clear_required` and publish the conservative finite helper effect before caller cancellation may propagate.
6. After the coordinator transaction releases the channel lock, reap the retired worker asynchronously and retrieve its result.

A worker resend must revalidate its captured helper lease inside the coordinator operation immediately before transport. If retired, it returns without sending. A resend that was queued before retirement may acquire the lock afterward, but it must observe the retired lease and remain transport-free.

The retired worker must not be able to delete or overwrite a replacement worker's task, event, or confirmed state; existing identity-checked cleanup remains required.

### 3. Cancellation and estop

Caller cancellation retains precedence, but only after helper ownership has been retired and either confirmed clear or durable pending clear has been established.

Rollback must never wait under the channel lock for a worker or child operation that may need that lock. Estop therefore remains able to acquire A then B and perform its physical clear even if a helper resend was queued at cancellation time.

## Data and State Invariants

- Confirmed output changes only after successful transport or accepted device report.
- An over-cap report and its pending reduction become visible atomically.
- No normal transport may begin after an over-cap report is accepted and before reduction ownership is established.
- An identical safe strength report changes no ownership counters.
- Retiring a helper lease prevents all later helper transports for that lease.
- A helper worker is never awaited while holding a lock it may acquire.
- Failed cleanup leaves truthful conservative confirmed state and durable pending clear work.
- Global operations continue to acquire A before B.

## Error Handling

- Invalid channel, strength, or cap inputs fail before state mutation using existing validation conventions.
- A physical helper clear returning false or raising a non-cancellation exception leaves pending clear and a conservative finite helper state.
- Cancellation during physical clear follows the same fail-closed state transition, then propagates cancellation.
- Worker-reaping errors are retrieved and logged but cannot reverse confirmed or pending coordinator state.

## Tests

All production changes use test-first development. Required deterministic regressions:

1. Queue helper resend behind a held channel lock, cancel the parent after helper delivery, and verify the parent settles without deadlock.
2. In the same scenario, start estop and verify it completes rather than waiting on a lock cycle.
3. Verify no helper frame is sent after lease retirement.
4. Verify successful rollback leaves strength zero, no waveform, no pending clear, and no live helper task.
5. Verify failed/cancelled rollback publishes conservative finite helper state and pending clear before cancellation propagates.
6. Queue normal pulse work, accept an over-cap report, and verify pending reduction/normal invalidation exists before the pulse can acquire the channel; no pulse frame is sent.
7. Verify an identical safe report leaves revision, main generation, normal epoch, helper generation, pending work, and continuous helper worker unchanged.
8. Verify a changed safe report updates confirmed strength/revision without orphaning live, replay, or helper ownership.
9. Preserve existing global A→B, estop release, outbox, provenance, exact replay, frontend, and full-suite tests.

## MVP Boundaries and Completion

Implementation is complete when the three review findings are closed, the required regressions pass in normal and asyncio debug modes, the full existing suite and frontend checks pass, and an independent whole-branch review finds no Critical or Important regression in this remediation.

Real-device behavior remains a separate manual acceptance gate. No additional feature work is authorized by this spec.
