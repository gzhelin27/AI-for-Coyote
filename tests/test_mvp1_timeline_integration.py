import asyncio
import unittest

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


if __name__ == "__main__":
    unittest.main()
