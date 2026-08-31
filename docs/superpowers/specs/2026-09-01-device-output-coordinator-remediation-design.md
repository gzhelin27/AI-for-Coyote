# Device Output Coordinator Safety Remediation Design

**Date:** 2026-09-01  
**Status:** Proposed  
**Parent spec:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

## Goal

Eliminate the remaining device-safety split-brain conditions by giving each
channel one authoritative output coordinator. A software state transition is
committed only after the required device operation succeeds.

## Problem statement

The current implementation has several valid local abstractions whose
composition is unsafe:

1. Ordinary `hold_strength` may install a default continuous waveform before
   changing strength. Reusing it for cap or overheat reductions can therefore
   create output while trying to reduce risk.
2. Some cap, channel-enable, pause, and clear flows update tracked state before
   physical delivery is confirmed. A failed send can leave software and device
   state disagreeing.
3. Live runners, replay, manual controls, safety reductions, and stop-class
   operations can each initiate device work without one shared ownership and
   prerequisite protocol.
4. Callback retry has more than one delivery owner, so a stale retry snapshot can
   reinsert and redeliver a record.
5. Provenance hashes omit effective inline prompt/examples and broad application
   content, so replay identity can claim stronger provenance than it possesses.

## Safety invariants

These requirements are binding:

- A safety reduction never creates or starts a waveform.
- Disable, pause, disconnect, stop, and estop require a confirmed physical clear
  before the coordinator reports the channel clear.
- A failed prerequisite clear prevents the dependent manual or replay operation.
- Software current strength, waveform, phase, and enabled state reflect confirmed
  device results, not requested intent.
- Failed cap/overheat reductions remain pending and are retried on the next
  reconciliation trigger, including an unchanged repeated device-state report.
- A live runner cannot reassert a strength above the current effective cap after
  reconciliation.
- Stop-class operations preempt normal output. Estop remains latched and cannot be
  cleared implicitly.
- Dry-run results use the same transaction semantics but produce no relay frames.
- No source text, waveform frames, RNG state, secrets, or local paths enter public
  state.

## Architecture

### 1. Per-channel output coordinator

Introduce one coordinator for channels A and B. All device-producing paths use
it:

- live timeline cycles;
- recorded replay;
- idle manual commands;
- cap and overheat reconciliation;
- channel enable/disable;
- pause, disconnect, finish, stop, and estop.

The coordinator serializes operations per channel and maintains:

- confirmed strength;
- confirmed waveform identity and mode;
- enabled/disabled status;
- pending safety reduction;
- generation/owner token;
- whether a confirmed clear is required.

Global stop-class operations acquire both channels in a stable A-then-B order.

### 2. Intent classes and priority

Operations are classified, highest priority first:

1. `ESTOP`
2. `CLEAR_OR_DISABLE`
3. `SAFETY_REDUCE`
4. `TIMELINE_OR_REPLAY`
5. `MANUAL`

A higher-priority intent invalidates older normal-output generations. A normal
operation must revalidate its generation immediately before sending.

`SAFETY_REDUCE` is a dedicated delta-strength operation. It never calls
`_ensure_default_wave`, never installs a loop, and never sends waveform frames.

### 3. Prepare, execute, commit

Every operation follows a transaction-like protocol:

1. **Prepare:** validate enabled state, estop, current caps, owner generation, and
   prerequisites without mutating confirmed state.
2. **Execute:** send the minimal device command.
3. **Commit:** only a confirmed send updates SafetyManager/coordinator state and
   produces an executed result.
4. **Fail:** record a dropped/failure result, retain or create pending safety work,
   and keep dependent operations blocked.

Temporary safety changes are not rolled back merely because delivery failed.
Instead, the lower limit remains authoritative while physical reconciliation is
pending.

### 4. Reconciliation

Cap, overheat, and channel configuration changes create desired safety state.
The coordinator compares it with confirmed device state.

- Lower desired strength: send a dedicated reduction delta.
- Disabled channel: invalidate its runner generation, terminalize output work,
  then require a confirmed channel clear.
- Failed reconciliation: retain the pending item and retry on the next state
  report, explicit retry, session transition, or before any lower-priority output.
- Successful reconciliation: commit confirmed state and notify the live session
  so replacement runners use the reconciled strength/cap.

No unchanged-state optimization may suppress a pending retry.

### 5. Manual arbitration

Idle manual behavior remains unchanged.

When a live session exists, manual output first performs a confirmed session
pause and global clear. When replay exists, it first performs a confirmed replay
stop and clear. If that prerequisite fails, the manual command is rejected and
no new device command is sent.

### 6. Truthful execution results

The existing `(executed, dropped)` boundary remains authoritative, but an action
enters `executed` only after every helper and primary transport operation it
depends on succeeds. Helper operations are either part of the same transaction
or absent.

Cycle recording and exact replay consume only committed effective results.
Transport false, exception, stale generation, or failed prerequisite cannot
produce a successful CycleRecord.

### 7. Single-owner callback outbox

Cycle records enter an ordered keyed outbox by `(channel, cycle_index)`. Exactly
one drain owner removes and delivers entries.

A retry only signals the owner or becomes the owner when none exists; it never
takes and later reinserts a stale snapshot. The delivery lock is not held while
awaiting arbitrary callbacks. Failed entries remain keyed in place for ordered
retry.

### 8. Provenance

Replay provenance includes:

- effective inline character prompt;
- effective examples;
- referenced prompt file bytes when present;
- role/profile/DLC identity and waveform policy;
- waveform hashes;
- an application fingerprint covering tracked runtime source/config schema
  content, with a stable release version when available.

Legacy archives remain loadable and are marked adjusted when required identity
fields are absent or differ.

## State and API behavior

Public session state continues to expose only phase, pattern, effective strength,
cycle index, and next start. Coordinator generations, pending safety details,
hashes, and provenance internals remain private.

Safety/config endpoints return success only after required physical
reconciliation succeeds. Delivery failures return a conflict/service error with
safe text and leave retryable internal state.

## Error and cancellation handling

Coordinator cleanup is owned by an internal task and shielded from caller
cancellation. Caller cancellation is re-raised only after the safety operation
has reached one of these states:

- confirmed clear/reduction;
- retryable pending safety state with all lower-priority output blocked.

No cancellation path may publish idle/paused/disabled while an unblocked older
owner can still send.

## Testing strategy

Strict RED-GREEN tests must cover:

- cap reduction during cycle and during generated gap without any waveform helper;
- failed reduction followed by identical overheat report retry;
- channel disable clear failure and retry with no later runner output;
- transport false/exception without confirmed-state mutation;
- manual command blocked after failed live/replay clear;
- concurrent safety and runner generation ordering;
- callback retry concurrency without duplicate delivery;
- provenance changes for inline prompt, examples, waveform policy, and unrelated
  runtime source changes;
- dry-run parity;
- full resolver → runner → store → no-RNG replay integration.

The final gate includes the complete Python suite, asyncio-debug concurrency
suite, compileall, persistent frontend tests, npm audit, and production build.
Real-device acceptance remains manual and occurs only after automated and review
gates pass.

## Scope

This remediation may modify SafetyManager, GameLoop, timeline runner/session/
player integration, API safety/manual endpoints, replay provenance, focused
frontend state types, and tests.

It does not add new gameplay features, new randomization policy, new manual
controls, video/novel automation, search/rating/delete, or a new archive format
unless compatibility tests prove a schema addition is necessary.

