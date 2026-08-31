# Device Output Coordinator Safety Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace split device/software state transitions with one confirmed, priority-aware per-channel output coordinator and close the five residual safety/reliability findings.

**Architecture:** A new coordinator serializes channel intents, owns confirmed output state, invalidates stale normal-output generations, and commits state only after transport confirmation. GameLoop remains the device-command adapter, but safety reduction, clear, live/replay, and manual paths must enter through the coordinator. Callback delivery and replay provenance are corrected independently after the physical-output boundary is reliable.

**Tech Stack:** Python 3.12, asyncio, FastAPI, unittest, React 18, TypeScript, Node 24.

**Spec:** `docs/superpowers/specs/2026-09-01-device-output-coordinator-remediation-design.md`

## Global Constraints

- A safety reduction never creates or starts a waveform.
- Disable, pause, disconnect, stop, and estop require confirmed physical clear before reporting the channel clear.
- Failed prerequisite clear prevents dependent manual or replay output.
- Confirmed software state changes only after confirmed device delivery.
- Failed cap/overheat reconciliation remains pending and retries even when the next reported safety state is unchanged.
- A live runner cannot reassert strength above the current effective cap.
- Stop-class operations preempt normal output; estop remains latched.
- Dry-run uses identical transaction semantics but sends no relay frames.
- No source text, waveform frames, RNG state, secrets, hashes, or local paths enter public state.
- Existing raw-cycle, random-gap, exact-replay, manual-idle, archive, and autopilot-cadence behavior remains unchanged unless this plan explicitly narrows it for safety.

---

### Task 1: Per-channel confirmed-output coordinator

**Files:**
- Create: `backend/output_coordinator.py`
- Test: `tests/test_output_coordinator.py`

**Interfaces:**
- Produces: `OutputIntentKind`, `TransportOutcome`, `ConfirmedChannelOutput`, `PendingSafetyWork`, and `DeviceOutputCoordinator`.
- Produces: `run(channel, kind, operation) -> TransportOutcome`, `invalidate(channel, minimum_priority) -> int`, `require_clear(channel)`, `mark_reduction(channel, target)`, `confirmed(channel)`, and `pending(channel)`.
- Consumes: async operation callbacks that perform one minimal transport operation and return `TransportOutcome(sent, effective)`.

- [ ] **Step 1: Write failing transaction tests**

Create tests proving:

```python
class OutputCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_transport_does_not_commit_confirmed_state(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        result = await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(sent=False, effective={"strength": 10}),
        )
        self.assertFalse(result.sent)
        self.assertEqual(coordinator.confirmed("A").strength, 30)

    async def test_higher_priority_invalidation_makes_normal_generation_stale(self):
        coordinator = DeviceOutputCoordinator()
        started = coordinator.generation("A")
        coordinator.invalidate("A", OutputIntentKind.CLEAR_OR_DISABLE)
        self.assertFalse(coordinator.is_current("A", started))

    async def test_failed_pending_reduction_survives_identical_retry_trigger(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.mark_reduction("A", 20)
        await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(sent=False),
        )
        self.assertEqual(coordinator.pending("A").target_strength, 20)
```

Also cover stable A-then-B acquisition for global clear, estop priority, callback cancellation, and dry-run `sent=True, simulated=True` commit parity.

- [ ] **Step 2: Verify RED**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_output_coordinator -v
```

Expected: import failure because `backend.output_coordinator` does not exist.

- [ ] **Step 3: Implement the coordinator**

Implement immutable transport outcomes and mutable private per-channel slots guarded by one `asyncio.Lock` per channel. Use priority order:

```python
class OutputIntentKind(IntEnum):
    MANUAL = 10
    TIMELINE_OR_REPLAY = 20
    SAFETY_REDUCE = 30
    CLEAR_OR_DISABLE = 40
    ESTOP = 50
```

`run()` must:

1. acquire the channel lock;
2. reject stale generations and disabled/clear-required lower-priority work;
3. invoke the supplied transport callback;
4. commit effective state only when `sent is True`;
5. retain pending reduction/clear work after false, exception, or cancellation;
6. return a failure outcome without fabricating effective state.

No coordinator lock may be held while awaiting an arbitrary record callback; this module owns only transport serialization.

- [ ] **Step 4: Verify GREEN and concurrency**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_output_coordinator -v
$env:PYTHONASYNCIODEBUG='1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -W error -m unittest tests.test_output_coordinator -v
Remove-Item Env:PYTHONASYNCIODEBUG
```

Expected: all tests pass with no pending-task warnings.

- [ ] **Step 5: Commit**

```powershell
git add backend/output_coordinator.py tests/test_output_coordinator.py
git commit -m "feat: add confirmed device output coordinator"
```

---

### Task 2: Safety reconciliation without waveform side effects

**Files:**
- Modify: `backend/safety.py`
- Modify: `backend/game_loop.py`
- Modify: `backend/main.py`
- Test: `tests/test_game_loop_timeline.py`
- Test: `tests/test_session_endpoints.py`
- Test: `tests/test_game_loop_cycle.py`

**Interfaces:**
- Consumes: `DeviceOutputCoordinator` from Task 1.
- Produces: `GameLoop.reconcile_runtime_safety(channel=None)`, `GameLoop.set_runtime_cap(channel, cap)`, and confirmed channel-disable behavior.
- Produces: dedicated strength-delta transport that never calls `_ensure_default_wave()`.

- [ ] **Step 1: Write failing safety regressions**

Add focused real-adapter tests:

```python
async def test_cap_reduction_during_gap_sends_no_waveform_helper(self):
    loop = make_active_timeline_loop(strength=30, phase="gap")
    await loop.set_runtime_cap("A", 10)
    self.assertEqual(loop.device_ops, [("strength_delta", "A", -20)])
    self.assertNotIn("waveform", [op[0] for op in loop.device_ops])
    self.assertEqual(loop.output_coordinator.confirmed("A").strength, 10)

async def test_failed_overheat_reduction_retries_same_report(self):
    loop = make_active_timeline_loop(strength=30)
    loop.device.fail_next_strength_delta()
    await loop.update_device_state(overheat_cap=20)
    await loop.update_device_state(overheat_cap=20)
    self.assertEqual(loop.device.strength_attempts, [-10, -10])
    self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)

async def test_disable_requires_confirmed_clear_and_blocks_runner_restart(self):
    loop = make_active_timeline_loop(strength=25)
    loop.device.fail_next_clear("A")
    with self.assertRaises(DeviceOutputError):
        await loop.set_channel_enabled("A", False)
    self.assertTrue(loop.output_coordinator.pending("A").clear_required)
    self.assertEqual(loop.runner_cycle_sends_after_failure, 0)
```

Cover cap reduction during raw cycle, during gap, failed delta, transport exception, overheat cap recovery without unsafe re-escalation, disable retry, and A/B independence.

- [ ] **Step 2: Verify RED**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_game_loop_cycle -v
```

Expected: failures showing default-wave helper creation, missing identical-state retry, or premature state commit.

- [ ] **Step 3: Integrate coordinator and dedicated safety commands**

Construct one coordinator in `GameLoop`. Seed it from current confirmed SafetyManager state.

Refactor cap/overheat/channel endpoints so they:

1. set desired safety policy without lowering confirmed physical state;
2. invalidate affected timeline generations;
3. call `reconcile_runtime_safety()`;
4. send only the minimal strength delta or channel clear;
5. commit SafetyManager/current state after coordinator success;
6. preserve pending work and return safe 409/503 on failure;
7. notify the session/runner of the reconciled cap before output restarts.

Remove safety reconciliation calls to ordinary `hold_strength`. Preserve idle manual `hold_strength` behavior.

An unchanged overheat report must call reconciliation whenever pending work exists.

- [ ] **Step 4: Run focused and impacted tests**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_output_coordinator tests.test_game_loop_timeline tests.test_session_endpoints tests.test_game_loop_cycle tests.test_cycle_runner tests.test_timeline_session -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/safety.py backend/game_loop.py backend/main.py tests/test_game_loop_timeline.py tests/test_session_endpoints.py tests/test_game_loop_cycle.py
git commit -m "fix: reconcile safety state after confirmed delivery"
```

---

### Task 3: Unified live, replay, clear, and manual arbitration

**Files:**
- Modify: `backend/game_loop.py`
- Modify: `backend/timeline/cycle_runner.py`
- Modify: `backend/timeline/player.py`
- Modify: `backend/timeline/session.py`
- Modify: `backend/main.py`
- Test: `tests/test_cycle_runner.py`
- Test: `tests/test_timeline_player.py`
- Test: `tests/test_timeline_session.py`
- Test: `tests/test_session_endpoints.py`

**Interfaces:**
- Consumes: coordinator transactions and generations from Tasks 1-2.
- Produces: truthful `(executed, dropped)`, confirmed clear lifecycle, and `GameLoop.execute_manual_action()` prerequisite enforcement.

- [ ] **Step 1: Write failing arbitration and delivery tests**

Add tests proving:

- `send_frame() == False` and transport exceptions place the high-level action in `dropped`, commit no helper waveform/current state, and create no `CycleRecord`;
- live/replay clear false or exception keeps cleanup pending;
- manual output after failed prerequisite clear is rejected and sends no manual device command;
- cancellation during prerequisite clear waits for safe cleanup or returns with lower-priority output blocked;
- a stale timeline generation cannot send after cap/disable/manual preemption;
- successful idle manual pulse/hold remains unchanged.

Example:

```python
async def test_manual_action_is_not_sent_after_failed_replay_clear(self):
    loop = make_replaying_loop()
    loop.device.fail_next_global_clear()
    with self.assertRaises(DeviceOutputError):
        await loop.execute_manual_action({"op": "hold_strength", "channel": "A", "value": 12})
    self.assertEqual(loop.device.manual_actions, [])
    self.assertTrue(loop.output_coordinator.pending("A").clear_required)
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -v
```

Expected: clear false is treated as success, manual action proceeds, or stale work sends.

- [ ] **Step 3: Route all output through coordinator**

Update GameLoop execution so each high-level action has one coordinator transaction. Helper waveform behavior, when valid for idle manual controls, belongs to the same transaction and rolls back/clears or reports the entire action dropped when the primary send fails.

Live/replay actions pass their coordinator generation and fail closed when stale. Clear helpers validate their `executed/dropped` result and only publish paused/idle/disabled after confirmed clear.

`execute_manual_action()` atomically:

1. owns/shields live pause or replay stop;
2. verifies confirmed global clear;
3. starts a manual-generation transaction;
4. executes the unchanged idle manual action.

- [ ] **Step 4: Verify impacted concurrency**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_output_coordinator tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_game_loop_timeline tests.test_session_endpoints -v
$env:PYTHONASYNCIODEBUG='1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -W error -m unittest tests.test_output_coordinator tests.test_cycle_runner tests.test_timeline_player tests.test_timeline_session tests.test_session_endpoints -v
Remove-Item Env:PYTHONASYNCIODEBUG
```

Expected: all pass without duplicate sends, pending tasks, or cancellation warnings.

- [ ] **Step 5: Commit**

```powershell
git add backend/game_loop.py backend/timeline/cycle_runner.py backend/timeline/player.py backend/timeline/session.py backend/main.py tests/test_cycle_runner.py tests/test_timeline_player.py tests/test_timeline_session.py tests/test_session_endpoints.py
git commit -m "fix: arbitrate all timeline and manual device output"
```

---

### Task 4: Single-owner record outbox

**Files:**
- Modify: `backend/timeline/cycle_runner.py`
- Test: `tests/test_cycle_runner.py`

**Interfaces:**
- Produces: ordered, keyed, at-least-once callback delivery without concurrent duplicate ownership.
- Preserves: synchronous production callback compatibility and `retry_pending_records()`.

- [ ] **Step 1: Write the failing concurrent retry test**

```python
async def test_waiting_retry_cannot_reinsert_stale_delivered_snapshot(self):
    harness = DeliveryHarness()
    await harness.queue_records([cycle(1), cycle(2)])
    harness.block_first_delivery()
    first = asyncio.create_task(harness.retry())
    await harness.first_delivery_started()
    second = asyncio.create_task(harness.retry())
    harness.release_delivery()
    await asyncio.gather(first, second)
    self.assertEqual(harness.delivered_indices, [1, 2])
    self.assertEqual(harness.pending_indices, [])
```

Also cover direct callback reentry, child-task retry after owner exit, callback cancellation, fail-then-retry ordering, and duplicate-key upsert.

- [ ] **Step 2: Verify RED**

Run:

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner -v
```

Expected: duplicate `[1, 2, 2]` delivery or stale pending record.

- [ ] **Step 3: Implement one drain owner**

Use one keyed ordered outbox and one owner task identity:

- insert/upsert under a short lock;
- if an owner exists, signal it and return/await as required without snapshots;
- the owner selects the current first key, releases the lock, awaits callback, then conditionally removes that same key;
- failure leaves the key in place and ends ownership;
- retry elects a new owner only when none exists.

Never hold the outbox lock while awaiting callback code.

- [ ] **Step 4: Verify GREEN**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_cycle_runner -v
$env:PYTHONASYNCIODEBUG='1'
D:\AI-for-Coyote\.venv\Scripts\python.exe -W error -m unittest tests.test_cycle_runner -v
Remove-Item Env:PYTHONASYNCIODEBUG
```

Expected: all pass with exactly-once delivery in non-ambiguous in-process retry cases.

- [ ] **Step 5: Commit**

```powershell
git add backend/timeline/cycle_runner.py tests/test_cycle_runner.py
git commit -m "fix: serialize cycle record delivery ownership"
```

---

### Task 5: Complete replay provenance fingerprints

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/timeline/session.py`
- Modify: `backend/timeline/models.py` only if a backward-compatible field is required
- Test: `tests/test_app_state_timeline.py`
- Test: `tests/test_timeline_session.py`
- Test: `tests/test_timeline_models.py`

**Interfaces:**
- Produces: stable application and DLC fingerprints covering effective behavior.
- Preserves: legacy archive loading and public-state redaction.

- [ ] **Step 1: Write failing fingerprint tests**

Add tests asserting fingerprint changes when only one of these changes:

- inline `character["prompt"]`;
- one effective example;
- prompt-file bytes;
- waveform policy;
- a tracked runtime source module other than `backend/main.py`.

Also assert stable ordering, legacy missing fields load, and HTTP/WS/replay-list state contains no hashes or seed.

- [ ] **Step 2: Verify RED**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_app_state_timeline tests.test_timeline_session tests.test_timeline_models -v
```

Expected: unchanged fingerprints for inline prompt/examples or unrelated source file.

- [ ] **Step 3: Implement canonical fingerprints**

Build canonical UTF-8 JSON for effective DLC behavior:

```python
{
    "role": ...,
    "profile": ...,
    "dlc": ...,
    "prompt": effective_inline_prompt,
    "examples": effective_examples,
    "prompt_file_sha256": ...,
    "waveform_policy": ...
}
```

Hash sorted, separator-stable JSON. Never store the prompt/examples themselves in public state.

Application fingerprint uses an explicit sorted allowlist of tracked runtime source and schema files, hashing both relative path and bytes. Prefer a stable release version when present and append the content fingerprint; do not fall back to hashing only `backend/main.py`.

Legacy manifests without new provenance remain loadable and replay as adjusted when identity cannot be confirmed.

- [ ] **Step 4: Verify focused tests**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_app_state_timeline tests.test_timeline_session tests.test_timeline_models tests.test_timeline_player -v
```

Expected: all pass and public redaction tests remain green.

- [ ] **Step 5: Commit**

```powershell
git add backend/main.py backend/timeline/session.py backend/timeline/models.py tests/test_app_state_timeline.py tests/test_timeline_session.py tests/test_timeline_models.py
git commit -m "fix: fingerprint effective replay provenance"
```

---

### Task 6: Integrated safety verification and operator documentation

**Files:**
- Modify: `tests/test_mvp1_timeline_integration.py`
- Modify: `tests/timeline_fakes.py`
- Modify: `README.md`
- Modify: `frontend/tests/timeline.test.mjs` only if public-state adapters change

**Interfaces:**
- Produces: composed proof for coordinator → GameLoop → runner/session → archive → no-RNG replay.
- Produces: operator guidance for pending safety failures and manual arbitration.

- [ ] **Step 1: Extend the end-to-end dry-run harness**

Add a real coordinator to `TimelineHarness` and tests proving:

1. live A/B cycles round-trip exactly without replay RNG;
2. cap lower during A gap sends no waveform helper and B continues independently;
3. failed overheat lower retries the same report;
4. failed disable/clear blocks runner and manual output;
5. successful retry reconciles confirmed state and resumes only allowed work;
6. record retry concurrency produces no duplicate archive cycle;
7. provenance changes mark replay adjusted.

- [ ] **Step 2: Update documentation**

Document:

- desired versus confirmed device state;
- pending safety reconciliation and retry behavior;
- manual commands pausing/stopping timeline first;
- failure responses and the rule that failed clear blocks dependent output;
- dry-run parity;
- real-device gate checklist for cap lower, overheat retry, disable, manual arbitration, and estop.

- [ ] **Step 3: Run complete automated verification**

```powershell
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests
$env:PATH='D:\AI-for-Coyote\.runtime\node\node-v24.19.0-win-x64;'+$env:PATH
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: every command exits 0, audit reports no high-severity vulnerabilities, and no paid LLM or real relay request occurs.

- [ ] **Step 4: Run final diff and privacy checks**

```powershell
git diff --check
rg -n "api[_-]?key|reasoning_content|waveform_hash|prompt_text|source_text" frontend/src backend/main.py
git status --short
```

Expected: no new public exposure, no whitespace errors, and only intended files changed before commit.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_mvp1_timeline_integration.py tests/timeline_fakes.py README.md frontend/tests/timeline.test.mjs
git commit -m "test: verify confirmed device output safety"
```

- [ ] **Step 6: Preserve external gates**

Do not push, merge, tag, call a paid LLM, or use a real device. Report those as explicit manual/external gates after automated review is clean.

