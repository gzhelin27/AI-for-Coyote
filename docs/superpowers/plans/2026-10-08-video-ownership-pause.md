# Video feedback clear ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The user selected a separate Sol task; use that authorized task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent ordinary device feedback from retiring video ownership while retrying that video's existing pending clear.

**Architecture:** Separate creation of a safety intent from conditional retry of an existing pending clear. Coordinator checks identity and pending state under its lock before transport side effects; only device-feedback reconciliation uses this path. Existing real external preemption and permanent token retirement remain authoritative.

**Tech Stack:** Python asyncio/unittest, existing DeviceOutputCoordinator and GameLoop, offline RelayClient/TickPhone, existing React/Vite frontend verification.

**Spec:** `docs/superpowers/specs/2026-10-08-video-ownership-pause-design.md`

## Global Constraints

- Runtime output ceiling remains 40 per channel in ignored local configuration; committed code must not raise it automatically.
- Video lease remains 1.0 seconds; lease loss stops output and does not auto-resume.
- Emergency stop, disconnect, overheat, reduced caps, channel disable and external output ownership always take priority.
- No ambient ownership token, automatic reclaim after retirement, blanket ownership bypass or permanent suppression of safety retries.
- Diagnosis and automated tests use temporary configuration and FakeRelay/TickPhone only; never target production ports 8000/9998 or switch production dry_run.
- Do not run tests/probe_llm.py; it performs paid external requests.
- Do not commit logs, private media/content, runtime configuration or inherited unrelated edits.

## Baseline and file responsibilities

The tested baseline is HEAD `e03f317` plus the current integrated working tree, not a fresh clean checkout. The root task will create the Sol task in the same directory. Before edits, record `git status --short`, `git diff -- backend/game_loop.py`, current HEAD, and hashes of files you will change. Preserve an exact local pre-edit copy of dirty GameLoop in `work/video-owner-oct08-game-loop-before.py` for a scoped delivery patch. Do not overwrite a pre-existing copy.

Files:

- `backend/output_coordinator.py`: conditional pending-clear retry, result type, read-only ownership diagnostics.
- `backend/game_loop.py`: opt device-feedback reconciliation into that API, place side effects inside its guarded transport; inherited dirty file.
- `backend/video/output.py`: expose bounded ownership diagnostics, with no control-flow change.
- `backend/video/session.py`: log first transport error and ownership mismatch details; no change to lease/error/claim behavior.
- `tests/test_output_coordinator.py`: atomic retry and concurrency contract.
- `tests/test_video_clear_ownership.py`: direct report interleaving regression and protection cases.
- `tests/test_video_feedback_ownership.py` (new): real RelayClient slots.patch + in-memory phone regression.
- `docs/superpowers/deliveries/2026-10-08-video-feedback-clear-integration.patch` (new): only this task's GameLoop delta if inherited dirty remains; follow the prior delivery-patch convention.

### Task 1: Conditional coordinator retry

**Interfaces:** `PendingClearRetryResult(status, outcome)` and `DeviceOutputCoordinator.retry_pending_clear(channel, operation)` as specified. `operation` accepts ConfirmedChannelOutput and returns an awaitable TransportOutcome. Result status is `not_pending`, `superseded`, or `attempted`; outcome is optional except for attempted.

- [ ] Add a failing test to `tests/test_output_coordinator.py`:

```python
async def test_pending_clear_retry_preserves_existing_owner(self):
    coordinator = DeviceOutputCoordinator()
    coordinator.require_clear('A')
    owner = OutputOwnership(coordinator)
    generation = coordinator.generation('A')
    calls = []
    async def clear(snapshot):
        calls.append(snapshot)
        return TransportOutcome(True, {
            'strength': 0, 'waveform': None, 'waveform_mode': None})
    result = await coordinator.retry_pending_clear('A', clear)
    self.assertEqual(result.status, 'attempted')
    self.assertTrue(result.outcome.sent)
    self.assertTrue(owner.is_current())
    self.assertEqual(coordinator.generation('A'), generation)
    again = await coordinator.retry_pending_clear('A', clear)
    self.assertEqual(again.status, 'not_pending')
    self.assertIsNone(again.outcome)
    self.assertEqual(len(calls), 1)
```

Import OutputOwnership with the existing coordinator symbols. Run:

```powershell
D:/AI-for-Coyote/.venv/Scripts/python.exe -m unittest tests.test_output_coordinator -v
```

Expected before implementation: the new test fails because retry_pending_clear is absent.

- [ ] Implement the result type and shielded method. Reuse `_execute`, `_valid_effective`, `_commit`, `_rejection`, `_refresh_priority`, and `_await_cleanup`; do not change ordinary `run`/`run_global`. The following is the implementation shape, with exact existing helpers:

```python
async def retry_pending_clear(self, channel, operation):
    slot = self._slot(channel)
    generation, epoch = slot.generation, slot.normal_epoch
    task = asyncio.create_task(self._retry_pending_clear_locked(
        slot, generation, epoch, operation))
    return await _await_cleanup(task)

async def _retry_pending_clear_locked(self, slot, generation, epoch, operation):
    async with slot.lock:
        if (slot.generation, slot.normal_epoch) != (generation, epoch):
            return PendingClearRetryResult('superseded', None)
        if not slot.pending.clear_required:
            return PendingClearRetryResult('not_pending', None)
        kind = OutputIntentKind.CLEAR_OR_DISABLE
        rejection = self._rejection(slot, generation, kind)
        if rejection is not None:
            return PendingClearRetryResult('attempted', rejection)
        outcome = await self._execute(operation, slot.confirmed)
        changed = (slot.generation, slot.normal_epoch) != (generation, epoch)
        newer_pending = slot.pending
        committed = self._commit(slot, kind, outcome)
        if changed:
            slot.pending = newer_pending
            self._refresh_priority(slot)
            return PendingClearRetryResult('superseded', committed)
        return PendingClearRetryResult('attempted', committed)
```

Use frozen dataclass plus Literal for status typing, and export the result type. This shape deliberately captures newer pending state after the await and restores it after publication; a late physical clear must not erase a new request. Validate cancellation with the existing shielding convention, not a new cancellation mechanism.

- [ ] Add event-barrier tests for the cases below. Every forbidden-operation callback must fail the test if called; track callback count to verify coalescing. Use `asyncio.Event` and the existing controlled transport helpers, not wall-clock sleeps.

| Case | Required assertion |
| --- | --- |
| Two retries queued for one pending clear | One transport, second not_pending, unchanged generation |
| First transport fails, second succeeds | Pending true after failure, false after success, unchanged generation |
| Pending already completed while retry waits for lock | not_pending, no callback |
| New require_clear or policy invalidation before lock | superseded, no callback, new pending retained |
| New clear/estop/cap-policy change during transport await | superseded, confirmed physical state truthful, new pending and priority retained |
| Estop already latched | No lower-priority transport; pending remains |
| Caller cancelled during awaited transport | Owned cleanup finishes, cancellation propagates, pending matches actual result |

- [ ] Run coordinator and ownership tests; inspect results. Commit only clean coordinator/test files after checking the staged diff:

```powershell
git add -- backend/output_coordinator.py tests/test_output_coordinator.py
git diff --cached --stat
git commit -m "fix: retry pending output clears without creating new ownership"
```

### Task 2: Route feedback retries and prove video continuity

**Interfaces consumed:** Task 1 retry method/result. **Produced:** `_reconcile_channel_safety_locked(channel, *, retry_existing_clear=False)` and `_retry_coordinated_clear(channel, operation)`; no public HTTP/frontend API change.

- [ ] Preserve the two diagnostic probes as uncommitted evidence and run both against the baseline before changing GameLoop:

```powershell
$env:PYTHONPATH=(Get-Location).Path
D:/AI-for-Coyote/.venv/Scripts/python.exe work/video-owner-oct08-probe.py
D:/AI-for-Coyote/.venv/Scripts/python.exe work/video-owner-oct08-relay-probe.py
```

Both must fail `paused != playing`. Their barrier exposes preempt -> action-lock acquisition. The relay probe uses actual `_handle_frame(slots.patch)`, the same callback structure as AppState, and fake ACKs; it opens no websocket connection.

- [ ] Move the direct regression into `VideoClearOwnershipTests` and the relay regression into the new test module. Reuse fixture/helper logic from the probe files, renaming the classes/tests and removing command-line-only code. Keep all source data synthetic. For the direct test, the central interleaving is:

```python
await self.prepare()
original = self.h.loop.clear_output
entered, release = asyncio.Event(), asyncio.Event()
once = False
async def delayed(channel=None, **kwargs):
    nonlocal once
    if not once:
        once = True
        entered.set()
        await release.wait()
    return await original(channel, **kwargs)
try:
    with patch.object(self.h.loop, 'clear_output', delayed):
        self.now = 1.05
        await self.observe(1050)
        await asyncio.wait_for(entered.wait(), 1)
        generation = self.h.loop.output_coordinator.generation('A')
        await self.h.loop.update_device_state(
            {'intensityA': 20, 'intensityB': 20}, None)
        self.assertEqual(self.h.loop.output_coordinator.generation('A'), generation)
        self.assertTrue(self.output.owns_control())
        await self.session.tick()
        release.set()
        await asyncio.wait_for(self.session.flush(), 2)
        self.assertEqual(self.session.status, 'playing')
        self.assertEqual(self.session.epoch, 1)
        self.assertFalse(self.session.state()['clear_pending'])
finally:
    release.set()
```

Use a per-channel fake-phone strength map when extending the relay probe to independent A/B final-strength assertions; the existing TickPhone has one strength variable and suffices only for its original single-channel ACK seam.

- [ ] Make only the feedback call opt in:

```python
result = await self._reconcile_channel_safety_locked(
    channel, retry_existing_clear=True)
```

In the pending-clear branch, false retains the old explicit-intent behavior. True routes to `_retry_coordinated_clear`; its guarded operation cancels channel loops and interrupts channel ACK waiters only after coordinator lock/identity/pending validation:

```python
async def _retry_coordinated_clear(self, channel, operation):
    async def guarded(snapshot):
        self._cancel_loops(channel, reset_pulse=False)
        self._interrupt_relay_waits((channel,))
        return await operation(snapshot)
    try:
        return await self.output_coordinator.retry_pending_clear(channel, guarded)
    finally:
        self._publish_coordinator_confirmed(channel)
```

For attempted, apply existing `_require_safety_outcome` and confirmed-state mirrors. For not_pending, do not append an executed clear and proceed only using newly read coordinator state. For superseded, return this stale report attempt without confirming a new clear, consuming new pending state or starting a reduction from stale assumptions. Keep all other caller paths and priority rules unchanged. Do not fetch/pass a video token in this wrapper.

- [ ] Extend regression coverage using the same event barriers: mirrored A/B zero transition; both-zero/gap clear; repeated ordinary reports; report queued behind active clear returning without extra ACK interruption; failure then retry; no-pending report strict no-op. External clear, takeover, estop, disable, runtime cap decrease, genuine over-cap/overheat feedback, disconnect and expired lease must stop the old video before or during the retry ACK wait. Assert no positive normal output after retirement and no automatic resume.

- [ ] Run both probes again: expected green. Run:

```powershell
D:/AI-for-Coyote/.venv/Scripts/python.exe -m unittest tests.test_output_coordinator tests.test_output_ownership tests.test_video_clear_ownership tests.test_video_feedback_ownership tests.test_video_direct_strength tests.test_video_session tests.test_video_integration tests.test_relay_acknowledgement -v
```

- [ ] Create a GameLoop-only delivery patch from the pre-edit integrated copy to the final file using `difflib.unified_diff` with `a/backend/game_loop.py` and `b/backend/game_loop.py` headers. Verify applying that patch to an isolated copy of the saved pre-edit file reproduces the final file byte-for-byte after newline normalization. Stage that patch and only the clean/new task test files; do not stage dirty GameLoop itself. If tests chosen for editing already had inherited changes, use the same scoped-patch approach. Commit `fix: preserve video ownership during device feedback clear retries`.

### Task 3: First-failure diagnostics and release evidence

**Interfaces:** observation-only `OutputOwnership.diagnostics() -> dict`, `GameLoopVideoOutput.ownership_diagnostics() -> dict`; no mutable token or authority returned.

- [ ] Add read-only diagnostics exposing expected/current generation and policy, retired state, and pending flags. Assert with a test that calling diagnostics before/after external invalidation does not change generation, pending work, or retirement behavior. Suggested output shape:

```python
{'retired': bool, 'channels': {
    'A': {'expected_generation': int, 'generation': int,
          'expected_policy': int, 'policy': int,
          'clear_required': bool, 'reduction_pending': bool},
    'B': {'expected_generation': int, 'generation': int,
          'expected_policy': int, 'policy': int,
          'clear_required': bool, 'reduction_pending': bool}}}
```

For normal video ownership no policy expectation currently exists; use `None` for that field rather than inventing one. Snapshot methods must not call `is_current()` just to report state, since that method can retire a token.

- [ ] At the existing interruption warning, include bounded ownership diagnostics only for ownership-change events. In `_drive` exception handlers retain the original clear/output error as a bounded log detail (max 200 characters), with epoch/sequence/position. Preserve user-facing messages and all control flow. Test with `assertLogs` that one transport failure logs its original reason and a later ownership failure cannot erase that first log; no secrets/media/client identifiers appear. Do not log every successful report or watchdog tick.

- [ ] Run full release checks from the integrated tree and save logs only under `work/video-owner-oct08-*`:

```powershell
D:/AI-for-Coyote/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py" -v
D:/AI-for-Coyote/.venv/Scripts/python.exe -m compileall -q backend tests
$env:PATH="D:/AI-for-Coyote/.runtime/node;"+$env:PATH
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

Record actual test counts and any unrelated baseline failure, rather than asserting the historic 1221 count. `probe_llm.py` is not in the test_* discovery pattern and must never be run separately.

- [ ] Extend/reuse `work/video_owner_acceptance.py` or `tests/video_browser_harness.py` for a temporary offline browser harness on a fresh non-production port. Use a synthetic timeline covering at least three minutes with both-channel changes, zero channels and gaps, delayed 120 ms clear ACKs and ordinary slots.patch reports. Maintain observations within the 1-second lease; verify zero ownership interruptions while safe, correct final targets/caps, and immediate stop for lease expiry and an injected external clear. Log only synthetic metadata. No production mutation or real output.

- [ ] Inspect only the task diff, run staged whitespace checks, and commit clean diagnostics/test changes (include a scoped delivery patch for any inherited dirty file). Report exact commits, patch application evidence, green probe logs, full test/build results and remaining production uncertainty to the coordinating task. Do not push upstream. Deployment/restart and manual real-device acceptance belong to the coordinating task after these gates; Sol must not mark them complete without that evidence.

## Execution evidence (2026-10-08)

- Coordinator API and first five regressions were committed as `25365f0`; subsequent edge tests and integration files are in the delivery commit. The two diagnostic probes changed from `paused != playing` to `playing`, with unchanged generation and epoch; logs are under `work/video-owner-oct08-*-final.log`.
- Focused suite: 106 tests passed after the operator-disable and first-failure logging corrections. Syntax compilation passed. Frontend: clean `npm ci`, 62 tests passed, production build passed. No frontend source was changed.
- Independent offline browser run (coordinating task, `work/root-oct08-browser-final.log`): 182 seconds of synthetic video, 3,358 safe relay reports, 25 reports during pending clears and 16 delayed clear acknowledgements. The media remained in epoch 1 through ordinary feedback; an injected external clear paused it and left A/B zero. The harness reported `dry_run=true`, zero real frames, cap 40.
- Full Python suite ran twice. Both runs had exactly one failure in the unrelated `test_session_endpoints.SessionEndpointTests.test_stalled_replay_list_and_download_keep_event_loop_responsive` `read_validated` subcase: its 100 ms scheduling threshold measured 139 ms and 127 ms. The final run had 1,237 tests, seven skips and one failure. The same test passed once before and five more times after as an isolated test. This is reported as an unresolved full-suite timing result; no replay endpoint behavior or test threshold was changed in this video repair.
- The GameLoop delivery patch applies to the exact saved integrated pre-edit file and reproduces the working file after newline normalization. Baseline SHA-256 begins `b7d0b8dc`; result begins `4de82a64`. The inherited dirty GameLoop and unrelated local files remain outside the commit.
- Real-device acceptance remains pending. Existing production service/config were not modified by the Sol implementation task.
