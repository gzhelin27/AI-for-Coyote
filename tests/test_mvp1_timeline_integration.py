import asyncio
import unittest
from unittest.mock import patch

from backend.output_coordinator import OutputIntentKind, TransportOutcome
from backend.safety import DeviceOutputError
from backend.timeline.models import SessionStatus
from tests.timeline_fakes import SequenceGapRandom, TimelineHarness


class MVP1IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_cycle_session_round_trips_without_resampling(self):
        harness = await TimelineHarness.create(
            seed=20260831,
            dry_run=True,
            cycle_rngs={
                "A": SequenceGapRandom([0, 10]),
                "B": SequenceGapRandom([20, 0]),
            },
        )
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn(
            [
                {"op": "hold_strength", "channel": "A", "value": 20},
                {"op": "hold_strength", "channel": "B", "value": 12},
            ]
        )
        await harness.complete_cycles("A", count=3)
        await harness.complete_cycles("B", count=2)
        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)

        result = await harness.replay(replay)

        self.assertEqual(result.requested_cycles, replay.timeline.cycles)
        self.assertEqual(result.rng_calls, 0)
        self.assertEqual(harness.relay.frames, [])
        self.assertEqual({cycle.channel for cycle in replay.timeline.cycles}, {"A", "B"})
        self.assertFalse(result.adjusted)
        self.assertTrue(result.completed)
        self.assertEqual(result.final_status, SessionStatus.IDLE)
        self.assertEqual(
            result.executed_cycle_actions,
            tuple(
                (
                    {
                        "op": "hold_strength",
                        "channel": cycle.channel,
                        "value": cycle.requested_strength,
                    },
                    {
                        "op": "pulse_cycle",
                        "channel": cycle.channel,
                        "pattern": cycle.pattern,
                    },
                )
                for cycle in replay.timeline.cycles
            ),
        )

    async def test_waveform_mismatch_marks_executed_replay_adjusted(self):
        harness = await TimelineHarness.create(seed=20260907, dry_run=True)
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await harness.complete_cycles("A", count=1)
        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)
        pattern = replay.timeline.cycles[0].pattern
        harness.safety.presets[pattern]["frames"][0] = "mismatched-frame"

        result = await harness.replay(replay)

        self.assertTrue(result.adjusted)
        self.assertTrue(result.completed)

    async def test_cap_lower_in_a_gap_has_no_waveform_helper_and_b_continues(self):
        harness = await TimelineHarness.create(
            seed=20260901,
            dry_run=False,
            cycle_rngs={
                "A": SequenceGapRandom([20, 0]),
                "B": SequenceGapRandom([20, 0]),
            },
        )
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn(
            [
                {"op": "hold_strength", "channel": "A", "value": 20},
                {"op": "hold_strength", "channel": "B", "value": 12},
            ]
        )
        await harness.wait_for_phase("A", "gap")
        before = len(harness.relay.attempts)
        b_completed = harness.completed_cycles("B")

        lowered = await harness.loop.set_runtime_cap("A", 5)

        self.assertEqual(lowered["dropped"], [])
        self.assertFalse(
            any(harness.relay.is_waveform_helper(frame) for frame in harness.relay.attempts[before:])
        )
        await harness.complete_cycles("B", count=1)
        self.assertGreater(harness.completed_cycles("B"), b_completed)

    async def test_failed_overheat_lower_retries_the_same_report(self):
        harness = await TimelineHarness.create(seed=20260902, dry_run=False)
        self.addAsyncCleanup(harness.close)

        await harness.loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 30}]
        )
        harness.relay.fail_next_strength_delta("A")
        report = {"channelA": {"comfortLimit": {"overheat": True}}}

        first = await harness.loop.update_device_state(None, report)
        second = await harness.loop.update_device_state(None, report)

        self.assertTrue(first["A"]["dropped"])
        self.assertEqual(second["A"]["dropped"], [])
        self.assertEqual(harness.relay.strength_deltas("A")[-2:], [-10, -10])
        self.assertEqual(harness.loop.output_coordinator.confirmed("A").strength, 20)
        self.assertIsNone(harness.loop.output_coordinator.pending("A").target_strength)

    async def test_failed_disable_clear_blocks_runner_and_manual_output(self):
        harness = await TimelineHarness.create(seed=20260903, dry_run=False)

        async def close_after_allowing_clear():
            harness.relay.fail_all_clears = False
            await harness.close()

        self.addAsyncCleanup(close_after_allowing_clear)

        await harness.start()
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        harness.relay.fail_all_clears = True
        with self.assertRaises(DeviceOutputError):
            await harness.loop.set_channel_enabled("A", False)

        self.assertTrue(harness.loop.output_coordinator.pending("A").clear_required)
        self.assertNotIn("A", harness.controller.runners)
        attempted_before = len(harness.relay.attempts)
        with self.assertRaises(DeviceOutputError):
            await harness.loop.execute_manual_action(
                {"op": "hold_strength", "channel": "A", "value": 10}
            )
        self.assertGreater(len(harness.relay.attempts), attempted_before)
        self.assertFalse(
            any(
                harness.relay.is_positive_manual_output(frame)
                for frame in harness.relay.attempts[attempted_before:]
            )
        )

    async def test_successful_retry_reconciles_and_resumes_only_allowed_work(self):
        harness = await TimelineHarness.create(seed=20260904, dry_run=False)
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn(
            [
                {"op": "hold_strength", "channel": "A", "value": 20},
                {"op": "hold_strength", "channel": "B", "value": 12},
            ]
        )
        harness.relay.fail_next_strength_delta("A")
        with self.assertRaises(DeviceOutputError):
            await harness.loop.set_runtime_cap("A", 5)

        self.assertNotIn("A", harness.controller.runners)
        self.assertIn("B", harness.controller.runners)
        reconciled = await harness.loop.set_runtime_cap("A", 5)

        self.assertEqual(reconciled["dropped"], [])
        self.assertEqual(harness.loop.output_coordinator.confirmed("A").strength, 5)
        self.assertIsNone(harness.loop.output_coordinator.pending("A").target_strength)
        self.assertIn("A", harness.controller.runners)
        self.assertIn("B", harness.controller.runners)

    async def test_concurrent_record_retry_archives_one_cycle(self):
        harness = await TimelineHarness.create(
            seed=20260905,
            dry_run=True,
            cycle_rngs={"A": SequenceGapRandom([0])},
        )
        self.addAsyncCleanup(harness.close)

        await harness.start()
        runner = harness.controller.runners["A"]
        original_callback = runner._on_cycle
        failed_once = True

        async def fail_once(record):
            nonlocal failed_once
            if failed_once:
                failed_once = False
                raise RuntimeError("transient archive outage")
            original_callback(record)

        runner._on_cycle = fail_once
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await harness.wait_for_pending_record("A")
        await runner.wait_stopped()
        await asyncio.gather(
            runner.retry_pending_records(), runner.retry_pending_records()
        )

        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)
        self.assertEqual(len(replay.timeline.cycles), 1)
        self.assertEqual(replay.timeline.cycles[0].channel, "A")

    async def test_changed_provenance_marks_replay_adjusted(self):
        harness = await TimelineHarness.create(seed=20260906, dry_run=True)
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn([{"op": "hold_strength", "channel": "A", "value": 20}])
        await harness.complete_cycles("A", count=1)
        saved = await harness.finish()
        harness.set_current_provenance(dlc_fingerprint="integration-dlc-changed")

        state = await harness.controller.start_replay(saved.replay_id)

        self.assertTrue(state.adjusted)

    async def test_report_helper_rollback_and_estop_preserve_safety_ownership(self):
        """Catch split report ownership or helper rollback that stalls estop.

        A safe report must leave A's continuous helper live.  An accepted
        over-cap B report must invalidate a queued normal waveform before it
        can reach transport.  A later B helper rollback must retire its queued
        resend before the global A-then-B estop completes.
        """
        harness = await TimelineHarness.create(seed=20260908, dry_run=False)
        self.addAsyncCleanup(harness.close)
        harness.loop.cfg["playback"]["frame_ms"] = 10
        harness.loop.cfg["playback"]["loop_batch_s"] = 0.4
        harness.loop.cfg["playback"]["loop_overlap_s"] = 0.2

        executed, dropped = await harness.loop.execute_actions(
            [
                {"op": "pulse_cycle", "channel": "B", "pattern": "呼吸"},
            ]
        )
        self.assertEqual(len(executed), 1)
        self.assertEqual(dropped, [])

        executed, dropped = await harness.loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 5}]
        )
        self.assertEqual(len(executed), 1)
        self.assertEqual(dropped, [])
        a_helper = harness.loop.loop_tasks["A"]
        a_helper_generation = harness.loop.output_coordinator.helper_generation("A")
        harness.safety.pulse_until["A"] = 0.0

        identical = await harness.loop.update_device_state(
            {"intensityA": 5}, None
        )

        self.assertEqual(identical, {})
        self.assertIs(harness.loop.loop_tasks["A"], a_helper)
        self.assertFalse(a_helper.done())
        self.assertEqual(
            harness.loop.output_coordinator.helper_generation("A"),
            a_helper_generation,
        )
        for _ in range(25):
            a_helper_frames = [
                frame
                for frame in harness.relay.attempts
                if harness.relay.is_waveform_helper(frame)
                and harness.relay._channel_name(harness.relay._operation(frame)[1])
                == "A"
            ]
            if len(a_helper_frames) >= 2:
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("identical report stopped A helper before its resend")

        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        reconciliation_started = asyncio.Event()
        reconciliation_returned = asyncio.Event()
        normal_queued = asyncio.Event()

        async def block_b_channel(snapshot):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective={"strength": int(snapshot.strength or 0)},
            )

        original_reconcile = harness.loop.output_coordinator.reconcile_reported_strength
        original_run_locked = harness.loop.output_coordinator._run_channel_locked

        async def observe_reconcile(*args, **kwargs):
            reconciliation_started.set()
            result = await original_reconcile(*args, **kwargs)
            reconciliation_returned.set()
            return result

        async def observe_normal_queue(*args, **kwargs):
            normal_queued.set()
            return await original_run_locked(*args, **kwargs)

        blocker = asyncio.create_task(
            harness.loop.output_coordinator.run(
                "B", OutputIntentKind.MANUAL, block_b_channel
            )
        )
        harness.safety.pulse_until["B"] = 0.0
        report = None
        normal = None
        try:
            with (
                patch.object(
                    harness.loop.output_coordinator,
                    "reconcile_reported_strength",
                    side_effect=observe_reconcile,
                ),
                patch.object(
                    harness.loop.output_coordinator,
                    "_run_channel_locked",
                    side_effect=observe_normal_queue,
                ),
            ):
                await asyncio.wait_for(blocker_started.wait(), timeout=1)
                report = asyncio.create_task(
                    harness.loop.update_device_state(
                        {"intensityB": 30},
                        {"channelB": {"comfortLimit": {"comfortMax": 20}}},
                    )
                )
                await asyncio.wait_for(reconciliation_started.wait(), timeout=1)
                normal = asyncio.create_task(
                    harness.loop.execute_actions(
                        [
                            {
                                "op": "pulse_cycle",
                                "channel": "B",
                                "pattern": "呼吸",
                            }
                        ]
                    )
                )
                await asyncio.wait_for(normal_queued.wait(), timeout=1)
                release_blocker.set()
                await asyncio.wait_for(reconciliation_returned.wait(), timeout=1)

                self.assertEqual(
                    harness.loop.output_coordinator.confirmed("B").strength, 30
                )
                self.assertEqual(
                    harness.loop.output_coordinator.pending("B").target_strength, 20
                )

                report_result, normal_result = await asyncio.gather(report, normal)

            self.assertEqual(report_result["B"]["dropped"], [])
            self.assertEqual(normal_result[0], [])
            self.assertEqual(len(normal_result[1]), 1)
            self.assertEqual(harness.loop.output_coordinator.confirmed("B").strength, 20)
        finally:
            release_blocker.set()
            await asyncio.gather(
                blocker,
                *(() if report is None else (report,)),
                *(() if normal is None else (normal,)),
                return_exceptions=True,
            )

        helper_delivered = asyncio.Event()
        resend_queued = asyncio.Event()
        primary_cancelled = asyncio.Event()
        original_send = harness.relay.send_frame
        original_run_locked = harness.loop.output_coordinator._run_channel_locked
        b_helper = None
        resend_task = None
        run_count = 0
        primary_cancellation_injected = False

        async def observe_helper_resend(*args, **kwargs):
            nonlocal resend_task, run_count
            run_count += 1
            if run_count == 2:
                resend_task = asyncio.current_task()
                resend_queued.set()
            return await original_run_locked(*args, **kwargs)

        async def cancel_b_primary(frame):
            nonlocal b_helper, primary_cancellation_injected
            method, payload = harness.relay._operation(frame)
            channel = harness.relay._channel_name(payload)
            if method == "device.op" and payload.get("t") == 0 and channel == "B":
                sent = await original_send(frame)
                helper_delivered.set()
                return sent
            if (
                not primary_cancellation_injected
                and method == "device.op"
                and payload.get("t") == 3
                and channel == "B"
            ):
                await helper_delivered.wait()
                b_helper = harness.loop.loop_tasks.get("B")
                await resend_queued.wait()
                primary_cancellation_injected = True
                primary_cancelled.set()
                raise asyncio.CancelledError
            return await original_send(frame)

        request = None
        stopping = None
        try:
            with (
                patch.object(
                    harness.loop.output_coordinator,
                    "_run_channel_locked",
                    side_effect=observe_helper_resend,
                ),
                patch.object(
                    harness.relay, "send_frame", side_effect=cancel_b_primary
                ),
            ):
                request = asyncio.create_task(
                    harness.loop.execute_manual_action(
                        {"op": "hold_strength", "channel": "B", "value": 5}
                    )
                )
                await asyncio.wait_for(primary_cancelled.wait(), timeout=1)
                estop_attempt_start = len(harness.relay.attempts)
                stopping = asyncio.create_task(harness.loop.estop())
                done, _ = await asyncio.wait({request, stopping}, timeout=1)

            self.assertIn(request, done)
            self.assertIn(stopping, done)
            request_result, estop_result = await asyncio.gather(
                request, stopping, return_exceptions=True
            )
            self.assertIsInstance(request_result, asyncio.CancelledError)
            self.assertEqual(estop_result, {"estop": True, "sent": True})
            self.assertIsNotNone(b_helper)
            self.assertTrue(b_helper.done())
            reset_channels = [
                payload.get("c")
                for frame in harness.relay.attempts[estop_attempt_start:]
                for method, payload in (harness.relay._operation(frame),)
                if method == "device.op" and payload.get("t") == 7
            ]
            self.assertEqual(reset_channels, [0, 1])
            self.assertEqual(harness.loop.output_coordinator.confirmed("A").strength, 0)
            self.assertEqual(harness.loop.output_coordinator.confirmed("B").strength, 0)
            self.assertFalse(
                harness.loop.output_coordinator.pending("A").clear_required
            )
            self.assertFalse(
                harness.loop.output_coordinator.pending("B").clear_required
            )
            self.assertEqual(harness.loop.loop_tasks, {})
            self.assertEqual(harness.loop.loop_events, {})
            attempts_after_settlement = len(harness.relay.attempts)
            await asyncio.sleep(0)
            self.assertEqual(len(harness.relay.attempts), attempts_after_settlement)
        finally:
            if resend_task is not None and not resend_task.done():
                resend_task.cancel()
            harness.loop._cancel_loops(None)
            for task in (request, stopping, b_helper, resend_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (request, stopping, b_helper, resend_task) if task),
                return_exceptions=True,
            )


if __name__ == "__main__":
    unittest.main()
