import asyncio
import unittest

from backend.timeline.player import ReplayPlaybackError
from tests.timeline_fakes import ReplayHarness


class RecordedCyclePlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_uses_recorded_cycle_starts_without_rng(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0, 7, 20])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.requested_gap_tenths, [0, 7, 20])
        self.assertEqual(harness.rng_calls, 0)
        self.assertEqual(harness.sleeper.sleep_calls, [200, 340, 200])

    async def test_normal_completion_waits_for_last_raw_cycle_before_clear(self):
        harness = ReplayHarness.from_cycles(gap_tenths=[0])
        self.addAsyncCleanup(harness.close)

        await harness.player.start()
        await harness.player.wait()

        self.assertEqual(harness.sleeper.sleep_calls, [200])
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

        self.assertEqual(paused_cursor, 1)
        self.assertEqual(
            [action["channel"] for action in harness.executor.requested_cycle_actions],
            ["A", "A"],
        )

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
