# Output Report and Helper Lease Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final report/reduction race and helper rollback lock cycle without expanding the MVP feature surface.

**Architecture:** Add one coordinator-owned reported-strength transaction that atomically commits report truth and any required safety ownership. Retire helper resend ownership synchronously inside the parent transaction, perform clear without waiting for the helper worker, and reap the retired worker only after the coordinator lock is released.

**Tech Stack:** Python 3.11+, asyncio, FastAPI backend, unittest/pytest-compatible test suite, TypeScript/Vitest frontend verification.

**Spec:** `docs/superpowers/specs/2026-09-01-output-report-helper-lease-remediation-design.md`

## Global Constraints

- Keep the change limited to the three final-review findings; add no UI, configuration keys, protocol messages, dependencies, actor model, or global command queue.
- Confirmed state changes only after successful transport or an accepted device report.
- Global device operations continue to acquire channel A before channel B.
- Caller cancellation propagates only after helper retirement and confirmed clear or durable pending clear.
- An identical safe strength report changes no revision or ownership counter.
- Real-device behavior remains a separate manual acceptance gate.
- Every production behavior is implemented with an observed failing regression first.

---

### Task 1: Atomic Report and Reduction Ownership

**Files:**
- Modify: `backend/output_coordinator.py`
- Modify: `backend/game_loop.py`
- Test: `tests/test_output_coordinator.py`
- Test: `tests/test_game_loop_cycle.py`
- Test: `tests/test_session_endpoints.py`

**Interfaces:**
- Consumes: `DeviceOutputCoordinator.confirmed(channel)`, `pending(channel)`, `normal_policy_epoch(channel)`, `_ChannelSlot.lock`, `SafetyManager.cap_for(channel)`.
- Produces: `async reconcile_reported_strength(channel: str, reported_strength: int, cap: int) -> ReportReconciliation`, where the immutable result exposes `confirmed: ConfirmedChannelOutput` and `reduction_required: bool`.
- Preserves: existing `confirm_reported_strength()` compatibility for tests/internal callers that do not make a safety decision, but GameLoop report handling must use the new atomic interface.

- [ ] **Step 1: Write coordinator RED tests for identical, changed-safe, and over-cap reports**

Add focused tests that capture all counters before the call and assert:

```python
before = coordinator.snapshot("A")
result = await coordinator.reconcile_reported_strength("A", 10, 20)
after = coordinator.snapshot("A")
assert result.reduction_required is False
assert after == before
assert coordinator.normal_policy_epoch("A") == before_normal_epoch
assert coordinator.helper_generation("A") == before_helper_generation
```

For a changed safe report, assert confirmed strength and revision change but main generation, normal epoch, helper generation, and pending work do not. For an over-cap report, assert confirmed strength, strictest pending target, safety priority, main generation, and normal epoch are all established before the call returns; helper generation remains unchanged.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest tests/test_output_coordinator.py -q
```

Expected: FAIL because `reconcile_reported_strength` and `ReportReconciliation` do not exist.

- [ ] **Step 3: Implement the immutable result and one-lock coordinator transaction**

Add a frozen result type near the existing coordinator result types:

```python
@dataclass(frozen=True)
class ReportReconciliation:
    confirmed: ConfirmedChannelOutput
    reduction_required: bool
```

Implement the public async wrapper using the existing cancellation-owned task pattern. Under `_ChannelSlot.lock`:

```python
if slot.confirmed.strength == strength and strength <= effective_cap:
    return ReportReconciliation(slot.confirmed, False)

if slot.confirmed.strength != strength:
    slot.confirmed = replace(slot.confirmed, strength=strength)
    slot.revision += 1

if strength > effective_cap:
    strictest = (
        effective_cap
        if slot.pending.target_strength is None
        else min(slot.pending.target_strength, effective_cap)
    )
    slot.pending = replace(slot.pending, target_strength=strictest)
    slot.normal_epoch += 1
    slot.generation += 1
    slot.minimum_priority = max(
        slot.minimum_priority, OutputIntentKind.SAFETY_REDUCE
    )
    return ReportReconciliation(slot.confirmed, True)

return ReportReconciliation(slot.confirmed, False)
```

Use existing validators for channel, strength, and cap. Do not modify helper generation for any strength report.

- [ ] **Step 4: Run coordinator tests and verify GREEN**

Run the Task 1 coordinator test file. Expected: PASS with identical reports producing byte-for-byte unchanged snapshots and counters.

- [ ] **Step 5: Write GameLoop RED tests for the over-cap interleaving and owner preservation**

Create a deterministic barrier around the channel lock:

- queue normal `pulse_cycle` work;
- accept reported strength 30 with effective cap 20;
- release the queued normal task only after report reconciliation returns;
- assert pending target 20 and invalidated normal epoch already exist;
- assert no pulse frame is sent before or during reduction.

Also assert identical and changed-safe reports preserve active live, replay, and continuous helper owners, and that below/equal-cap retained work may resume while above-cap work may not.

- [ ] **Step 6: Run the new GameLoop tests and verify RED**

Run the named tests in `test_game_loop_cycle.py` and `test_session_endpoints.py`. Expected: FAIL because GameLoop still confirms and marks reduction in separate operations or still advances helper ownership.

- [ ] **Step 7: Route GameLoop reports through the atomic result**

Replace the split confirmation/`mark_reduction()` sequence in `_reconcile_device_report_channel()` with the new transaction. Publish the returned confirmed snapshot, then suspend/reconcile only when `reduction_required` or pre-existing pending work requires it. Keep the safe resume predicate `confirmed_strength <= cap`.

Do not add a broad `_action_lock` around report processing.

- [ ] **Step 8: Run Task 1 focused and impacted tests**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest tests/test_output_coordinator.py tests/test_game_loop_cycle.py tests/test_session_endpoints.py -q
```

Expected: PASS with no transport in the report-to-reduction gap.

- [ ] **Step 9: Commit Task 1**

```powershell
git add backend/output_coordinator.py backend/game_loop.py tests/test_output_coordinator.py tests/test_game_loop_cycle.py tests/test_session_endpoints.py
git commit -m "fix: reconcile device reports atomically"
```

---

### Task 2: Non-Blocking Helper Lease Retirement

**Files:**
- Modify: `backend/output_coordinator.py`
- Modify: `backend/game_loop.py`
- Test: `tests/test_game_loop_cycle.py`
- Test: `tests/test_session_endpoints.py`

**Interfaces:**
- Consumes: coordinator `helper_generation(channel)`, GameLoop `loop_tasks`, `loop_events`, `_cancel_loops()`, `_rollback_helper_transport()`.
- Produces: `retire_helper(channel: str) -> int`, a synchronous coordinator operation that advances only helper ownership and returns the retired generation; a post-transaction GameLoop reaper for identity-matching retired tasks.
- Preserves: helper resend operations continue through `DeviceOutputCoordinator.run()` and revalidate their captured helper generation immediately before transport.

- [ ] **Step 1: Write RED tests for the lock cycle and estop progress**

Use valid playback timing (`loop_batch_s=2.0`, `loop_overlap_s=0.3`). Arrange:

1. initial helper frame succeeds;
2. helper worker times out and queues its resend on the channel coordinator;
3. primary strength transport raises `CancelledError` while the parent owns the channel;
4. assert the parent settles within a bounded event-based timeout;
5. start estop and assert it also settles;
6. assert no resend transport occurs after retirement.

Use events/barriers, not sleeps, to establish ordering. A short timeout may only guard against deadlock after all ordering events are observed.

- [ ] **Step 2: Run the deadlock tests and verify RED**

Run the named tests with `PYTHONASYNCIODEBUG=1`. Expected: timeout with parent/helper waiting on the same channel lock.

- [ ] **Step 3: Add synchronous helper lease retirement**

Add the coordinator operation:

```python
def retire_helper(self, channel: str) -> int:
    slot = self._slot(channel)
    retired = slot.helper_generation
    slot.helper_generation += 1
    return retired
```

The method must not change confirmed state, main generation, normal epoch, pending work, or priority. It is called only by code that already owns the parent output transaction or by explicit loop replacement paths.

- [ ] **Step 4: Refactor rollback to invalidate, clear, then reap**

Inside `_rollback_helper_transport()`:

- retire the helper lease;
- set the identity-matching stop event and cancel the identity-matching task;
- do not `await` or `gather` that task while inside the coordinator callback;
- perform physical clear and return the truthful `TransportOutcome`;
- on clear failure/cancellation, call `require_clear()` and publish the finite helper effect before propagating caller cancellation.

After `coordinator.run()` returns and its channel lock is released, schedule or await a small reaper that retrieves the retired task result only if it is still the same task. The reaper must never delete replacement task/event ownership.

- [ ] **Step 5: Revalidate the helper lease inside resend transport**

Retain the pre-run fast check, and keep the decisive check inside the coordinator callback immediately before `_send_frames_complete()`:

```python
if coordinator.helper_generation(channel) != captured_helper_generation:
    return TransportOutcome(sent=False, error="stale helper lease")
```

This guarantees a resend queued before retirement becomes transport-free after it acquires the released lock.

- [ ] **Step 6: Run deadlock and no-post-retirement-send tests and verify GREEN**

Run the new focused tests normally and with asyncio debug. Expected: parent and estop complete, worker becomes terminal, and no helper frames are sent after retirement.

- [ ] **Step 7: Add cleanup success/failure/cancellation state tests**

Assert:

- successful rollback: confirmed strength 0, waveform `None`, no pending clear, retired worker terminal;
- false/exceptional clear: conservative finite helper waveform plus pending clear;
- cancelled clear: the same durable fail-closed state exists before `CancelledError` reaches the caller;
- a replacement helper survives stale worker finalization.

- [ ] **Step 8: Run Task 2 focused and impacted tests**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest tests/test_game_loop_cycle.py tests/test_session_endpoints.py -q
$env:PYTHONASYNCIODEBUG='1'; D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest tests/test_game_loop_cycle.py tests/test_session_endpoints.py -q
```

Expected: PASS with no pending asyncio tasks or un-retrieved exceptions.

- [ ] **Step 9: Commit Task 2**

```powershell
git add backend/output_coordinator.py backend/game_loop.py tests/test_game_loop_cycle.py tests/test_session_endpoints.py
git commit -m "fix: retire helper leases without lock cycles"
```

---

### Task 3: Integrated Safety Verification

**Files:**
- Modify: `tests/test_mvp1_timeline_integration.py`
- Modify: `README.md` only if verification wording must be corrected

**Interfaces:**
- Consumes: Task 1 atomic report transaction and Task 2 helper retirement behavior.
- Produces: one composed regression matrix proving report, helper rollback, and estop progress together without new product behavior.

- [ ] **Step 1: Add a composed concurrency regression**

Create an integration test that runs A/B normal output, accepts an identical safe report on one channel, accepts an over-cap report on the other, and cancels a helper-assisted action with a resend queued. Assert:

- identical report preserves continuous helper output;
- over-cap channel blocks normal transport before reduction;
- cancelled helper owner settles with confirmed clear or durable pending clear;
- global estop completes A then B;
- no task remains live after cleanup.

- [ ] **Step 2: Prove the integration test detects old behavior**

Run the test against a controlled fault seam or temporarily invert one assertion locally; observe the expected failure, then restore the correct test. Do not add a production test hook.

- [ ] **Step 3: Run the complete backend suite normally and with asyncio debug**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest -q
$env:PYTHONASYNCIODEBUG='1'; D:\AI-for-Coyote\.venv\Scripts\python.exe -m pytest -q
D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests
```

Expected: all tests pass with no pending-task or un-retrieved-exception diagnostics.

- [ ] **Step 4: Run frontend verification without changing product UI**

Run from `frontend` using the bundled Node runtime:

```powershell
npm audit --audit-level=high
npm test -- --run
npm run build
```

Expected: zero high-severity audit findings, tests pass, build succeeds.

- [ ] **Step 5: Verify repository scope and documentation truthfulness**

Run `git diff --check` and inspect the branch diff. Confirm no new UI/config/protocol/dependency feature and that README still states real-device acceptance is manual.

- [ ] **Step 6: Commit Task 3**

```powershell
git add tests/test_mvp1_timeline_integration.py README.md
git commit -m "test: verify atomic report and helper retirement"
```
