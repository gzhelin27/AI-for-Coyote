import asyncio
import unittest

from backend.timeline.player import ReplayPlaybackError
from tests.timeline_fakes import ReplayHarness


class RecordedCyclePlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_name_and_frame_count_with_changed_frames_marks_adjusted(self):
        cycle = ReplayHarness._cycle(
            channel="A", cycle_index=1, offset_ms=0, gap_tenths=0
        )
        harness = ReplayHarness([cycle], frames={"呼吸": ("x0", "x1")})
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertTrue(harness.player.adjusted)

    async def test_matching_identity_and_safety_stays_exact(self):
        harness = ReplayHarness.from_strength(original=20, current_cap=40)
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertFalse(harness.player.adjusted)

    async def test_empty_replay_runs_terminal_clear(self):
        harness = ReplayHarness([])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertFalse(harness.player.running)
        self.assertEqual(harness.executor.clear_calls, [None])

    async def test_replay_uses_recorded_cycle_starts_without_rng(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 7, 20])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.requested_gap_tenths, [0, 7, 20])
        self.assertEqual(harness.rng_calls, 0)
        self.assertEqual(harness.sleeper.sleep_calls, [200, 340, 600])

    async def test_normal_completion_waits_for_last_raw_cycle_before_clear(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.sleeper.sleep_calls, [200])
        self.assertEqual(harness.executor.clear_calls, [None])

    async def test_terminal_actual_gap_is_preserved_before_completion_clear(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[10])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.sleeper.sleep_calls, [400])
        self.assertEqual(harness.executor.clear_calls, [None])

    async def test_monotonic_schedule_accounts_for_executor_elapsed_time(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0])
        self.addAsyncCleanup(harness.close)
        execute_actions = harness.executor.execute_actions

        async def execute_with_elapsed_time(actions):
            result = await execute_actions(actions)
            harness.sleeper.now_ms += 50
            return result

        harness.executor.execute_actions = execute_with_elapsed_time

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.sleeper.sleep_calls, [150, 150])

    async def test_current_safety_clamp_marks_adjusted(self):
        harness = ReplayHarness.from_strength(original=40, current_cap=30)
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertTrue(harness.player.adjusted)

    async def test_effective_result_must_match_exact_operation_and_channel(self):
        harness = ReplayHarness.from_strength(
            original=40,
            current_cap=30,
            decoy_strength_result=True,
        )
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertTrue(harness.player.adjusted)

    async def test_missing_replay_strength_result_fails_activation(self):
        harness = ReplayHarness.from_strength(original=20, current_cap=40)
        self.addAsyncCleanup(harness.close)
        execute_actions = harness.executor.execute_actions

        async def omit_strength_result(actions):
            executed, dropped = await execute_actions(actions)
            return [
                item
                for item in executed
                if item.get("action", {}).get("op") != "hold_strength"
            ], dropped

        harness.executor.execute_actions = omit_strength_result

        await harness.player.start()
        with self.assertRaisesRegex(
            ReplayPlaybackError, "strength prerequisite"
        ):
            await harness.player.wait()

    async def test_pause_and_resume_from_cursor_restarts_at_safe_cycle(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if len(harness.executor.requested_cycle_actions) == 1:
                break
            await asyncio.sleep(0)

        await harness.player.pause()
        paused_cursor = harness.player.cursor
        await harness.player.resume(paused_cursor)
        for _ in range(20):
            if not harness.player.running:
                break
            remaining = harness.sleeper.next_remaining_ms
            if remaining is not None:
                harness.sleeper.advance(remaining)
            await asyncio.sleep(0)
        await harness.player.wait()

        self.assertEqual(paused_cursor, 0)
        self.assertEqual(
            [action["channel"] for action in harness.executor.requested_cycle_actions],
            ["A", "A", "A"],
        )

    async def test_pause_mid_raw_cycle_resumes_same_cycle_from_frame_zero_immediately(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[10], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if len(harness.executor.requested_cycle_actions) == 1:
                break
            await asyncio.sleep(0)
        harness.sleeper.advance(50)

        await harness.player.pause()
        paused_cursor = harness.player.cursor
        await harness.player.resume()
        for _ in range(20):
            if len(harness.executor.requested_cycle_actions) == 2:
                break
            await asyncio.sleep(0)

        self.assertEqual(paused_cursor, 0)
        self.assertEqual(len(harness.executor.requested_cycle_actions), 2)
        self.assertEqual(harness.executor.cycle_start_frames, [0, 0])

    async def test_public_channel_state_tracks_replay_cycle_gap_and_next_start(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[10, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if len(harness.executor.requested_cycle_actions) == 1:
                break
            await asyncio.sleep(0)

        cycle_state = harness.player.channel_states()["A"]
        self.assertEqual(
            cycle_state,
            {
                "phase": "cycle",
                "pattern": "呼吸",
                "strength": 20,
                "cycle_index": 1,
                "next_cycle_start_ms": 400,
            },
        )
        harness.sleeper.advance(200)
        gap_state = harness.player.channel_states()["A"]
        self.assertEqual(gap_state["phase"], "gap")
        self.assertEqual(gap_state["pattern"], "呼吸")

        await harness.player.pause()

        self.assertEqual(harness.player.channel_states()["A"]["phase"], "paused")
        self.assertIsNone(harness.player.channel_states()["A"]["pattern"])

    async def test_resume_at_end_creates_terminal_clear_task(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)
        await harness.player.pause()

        await harness.player.resume(len(harness.player.ordered_cycles))
        result = await asyncio.gather(harness.player.wait(), return_exceptions=True)

        self.assertIsNone(result[0])
        self.assertFalse(harness.player.running)
        self.assertEqual(harness.executor.clear_calls, [None, None])

    async def test_running_replay_still_validates_replacement_cursor(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()

        with self.assertRaisesRegex(ValueError, "cursor"):
            await harness.player.resume(3)

    async def test_pause_quiesces_in_flight_executor_before_final_clear(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0], controlled=True)
        self.addAsyncCleanup(harness.close)
        execute_actions = harness.executor.execute_actions
        clear_output = harness.executor.clear_output
        executor_started = asyncio.Event()
        order: list[str] = []

        async def cancellation_aware_execute(actions):
            if any(action.get("op") == "pulse_cycle" for action in actions):
                executor_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    order.append("executor_terminal")
                    raise
            return await execute_actions(actions)

        async def ordered_clear(channel=None):
            order.append("clear")
            return await clear_output(channel)

        harness.executor.execute_actions = cancellation_aware_execute
        harness.executor.clear_output = ordered_clear
        await harness.player.start()
        await asyncio.wait_for(executor_started.wait(), timeout=0.2)

        await harness.player.pause()

        self.assertEqual(order, ["executor_terminal", "clear"])

    async def test_failed_pause_clear_is_retryable_by_pause(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)
        harness.executor.clear_failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await harness.player.pause()
        await harness.player.pause()

        self.assertEqual(harness.executor.clear_calls, [None, None])

    async def test_false_pause_clear_result_is_retryable_and_not_published_paused(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)
        clear_output = harness.executor.clear_output
        clear_attempts = 0

        async def false_once(channel=None):
            nonlocal clear_attempts
            clear_attempts += 1
            if clear_attempts == 1:
                harness.executor.clear_calls.append(channel)
                return [], [
                    {
                        "action": {"op": "stop"},
                        "reason": "injected false clear",
                        "sent": False,
                    }
                ]
            return await clear_output(channel)

        harness.executor.clear_output = false_once

        with self.assertRaisesRegex(ReplayPlaybackError, "clear"):
            await harness.player.pause()

        self.assertEqual(harness.player.channel_states()["A"]["phase"], "stopped")
        await harness.player.pause()
        self.assertTrue(harness.player.paused)
        self.assertEqual(harness.executor.clear_calls, [None, None])

    async def test_failed_stop_clear_is_retryable_by_stop(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)
        harness.executor.clear_failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await harness.player.stop()
        await harness.player.stop()

        self.assertEqual(harness.executor.clear_calls, [None, None])

    async def test_load_after_pause_drops_the_cancelled_task(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)
        await harness.player.pause()

        harness.player.load(harness.bundle)
        result = await asyncio.gather(harness.player.wait(), return_exceptions=True)

        self.assertIsNone(result[0])

    async def test_load_after_failure_drops_the_failed_task(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0], fail_on_cycle=1)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        with self.assertRaises(ReplayPlaybackError):
            await harness.player.wait()

        harness.player.load(harness.bundle)
        result = await asyncio.gather(harness.player.wait(), return_exceptions=True)

        self.assertIsNone(result[0])

    async def test_simultaneous_channel_starts_keep_archive_tie_order(self):
        harness = ReplayHarness.from_channel_cycles(
            [("B", 0, 0), ("A", 0, 0), ("A", 200, 0), ("B", 200, 0)]
        )
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(
            [action["channel"] for action in harness.executor.requested_cycle_actions],
            ["B", "A", "A", "B"],
        )

    async def test_same_channel_ties_follow_cycle_index_not_archive_position(self):
        later = ReplayHarness._cycle(
            channel="A",
            cycle_index=2,
            offset_ms=0,
            gap_tenths=0,
            requested_strength=22,
            effective_strength=22,
        )
        earlier = ReplayHarness._cycle(
            channel="A",
            cycle_index=1,
            offset_ms=0,
            gap_tenths=0,
            requested_strength=11,
            effective_strength=11,
        )
        harness = ReplayHarness([later, earlier])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        requested_strengths = [
            call[0]["value"]
            for call in harness.executor.execute_calls
            if call and call[0].get("op") == "hold_strength"
        ]
        self.assertEqual(requested_strengths, [11, 22])

    async def test_executor_failure_stops_and_clears_output(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0], fail_on_cycle=1)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()

        with self.assertRaisesRegex(ReplayPlaybackError, "executor failure"):
            await harness.player.wait()

        self.assertEqual(harness.executor.clear_calls, [None])

    async def test_failed_clear_remains_retryable_by_stop(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0])
        self.addAsyncCleanup(harness.close)
        harness.executor.clear_failures_remaining = 2
        await harness.player.start()

        with self.assertRaisesRegex(ReplayPlaybackError, "clear"):
            await harness.player.wait()
        await harness.player.stop()

        self.assertEqual(harness.executor.clear_calls, [None, None, None])

    async def test_repeated_pause_and_stop_clear_only_once(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 0], controlled=True)
        self.addAsyncCleanup(harness.close)
        await harness.player.start()
        for _ in range(20):
            if harness.player.cursor == 1:
                break
            await asyncio.sleep(0)

        await harness.player.pause()
        await harness.player.pause()
        await harness.player.stop()

        self.assertEqual(harness.executor.clear_calls, [None])


if __name__ == "__main__":
    unittest.main()
