import random
import unittest

from backend.timeline.cycle_runner import CycleDirective, RunnerPhase
from backend.timeline.randomizer import derive_stream_seed
from tests.timeline_fakes import CycleHarness, SequenceGapRandom


class CycleRunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_harness(self, **kwargs) -> CycleHarness:
        harness = CycleHarness(**kwargs)
        self.addAsyncCleanup(harness.close)
        return harness

    async def test_one_cycle_uses_complete_raw_frame_count(self):
        harness = self.make_harness(frames={"呼吸": ["f"] * 12}, gap_tenths=[7])

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 24))
        await harness.complete_cycle()

        self.assertEqual(harness.sent_cycles, [("A", "呼吸", 12)])
        self.assertEqual(harness.records[0].raw_duration_ms, 1200)
        self.assertEqual(harness.records[0].planned_gap_ms, 840)
        self.assertEqual(harness.records[0].actual_gap_ms, 840)

    async def test_normal_change_waits_for_cycle_boundary(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"] * 12, "潮汐": ["g"] * 23}
        )

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 25))
        self.assertEqual(harness.sent_patterns, ["呼吸"])

        await harness.complete_cycle()

        self.assertEqual(harness.sent_patterns, ["呼吸", "潮汐"])
        self.assertEqual(harness.executor.strength_calls, [("A", 20), ("A", 25)])
        self.assertEqual(harness.records[0].gap_tenths, 0)

    async def test_new_event_during_gap_ends_gap_immediately(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"] * 12, "潮汐": ["g"] * 23},
            gap_tenths=[20],
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.enter_gap()
        harness.sleeper.advance(100)

        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 25))
        await harness.flush()

        self.assertEqual(harness.sent_patterns[-1], "潮汐")
        self.assertEqual(harness.records[0].actual_gap_ms, 100)
        self.assertLess(
            harness.records[0].actual_gap_ms, harness.records[0].planned_gap_ms
        )

    async def test_stop_cancels_cycle_and_clears_immediately(self):
        harness = self.make_harness(frames={"呼吸": ["f"] * 12})
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        await harness.runner.stop(clear=True, reason="estop")

        self.assertEqual(harness.clear_calls, ["A"])
        self.assertGreaterEqual(harness.sleeper.cancellations, 1)
        self.assertEqual(harness.records[0].interruption_reason, "estop")
        self.assertFalse(harness.records[0].completed)
        self.assertEqual(harness.runner.state().phase, RunnerPhase.STOPPED)

    async def test_latest_pending_normal_change_wins(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"], "潮汐": ["g"], "律动": ["h"]}
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 22))
        await harness.runner.submit(CycleDirective("A", "evt-3", "律动", 24))

        await harness.complete_cycle()

        self.assertEqual(harness.sent_patterns, ["呼吸", "律动"])

    async def test_executor_rejection_stops_runner_and_records_failure(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"]}, fail_on_cycle=1
        )

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.wait_stopped()

        state = harness.runner.state()
        self.assertEqual(state.phase, RunnerPhase.STOPPED)
        self.assertIn("injected rejection", state.failure or "")
        self.assertEqual(harness.records[0].interruption_reason, "executor_rejected")

    async def test_executor_exception_stops_runner_and_records_failure(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"]}, raise_on_cycle=1
        )

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.wait_stopped()

        self.assertIn("injected executor failure", harness.runner.state().failure or "")
        self.assertEqual(harness.records[0].interruption_reason, "executor_failure")

    async def test_confirmed_disconnect_is_surfaced(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"]}, disconnect_on_cycle=1
        )

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.wait_stopped()

        self.assertTrue(harness.runner.state().disconnected)

    async def test_pause_resume_restarts_complete_cycle_without_reseeding_rng(self):
        rng = SequenceGapRandom([7, 12])
        harness = self.make_harness(frames={"呼吸": ["f"] * 4}, rng=rng)
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        harness.sleeper.advance(100)

        await harness.runner.pause(reason="operator_pause")

        self.assertEqual(rng.calls, 0)
        self.assertFalse(harness.records[0].completed)
        self.assertEqual(harness.records[0].interruption_reason, "operator_pause")

        await harness.runner.resume()
        await harness.complete_cycle()

        self.assertEqual(rng.calls, 1)
        self.assertEqual(harness.sent_cycles, [("A", "呼吸", 4), ("A", "呼吸", 4)])
        self.assertEqual(harness.executor.strength_calls, [("A", 20), ("A", 20)])

    async def test_zero_gap_does_not_call_sleeper_with_zero(self):
        harness = self.make_harness(frames={"呼吸": ["f"] * 2}, gap_tenths=[0])
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        await harness.complete_cycle()
        await harness.flush()

        self.assertEqual(harness.records[0].gap_tenths, 0)
        self.assertNotIn(0, harness.sleeper.sleep_calls)
        self.assertGreaterEqual(len(harness.sent_cycles), 2)
        self.assertEqual(harness.executor.strength_calls, [("A", 20)])

    async def test_repeated_cycles_retain_executor_effective_strength(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"]},
            gap_tenths=[0, 0],
            effective_strength=18,
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        await harness.complete_cycle()
        await harness.complete_cycle()

        self.assertEqual(
            [record.effective_strength for record in harness.records[:2]],
            [18, 18],
        )

    async def test_ab_gap_stream_is_independent_of_extra_a_cycle(self):
        session_seed = 0
        frames = {"呼吸": ["f"]}
        harness_a = self.make_harness(
            channel="A",
            frames=frames,
            rng=random.Random(derive_stream_seed(session_seed, "cycle:A")),
        )
        harness_b = self.make_harness(
            channel="B",
            frames=frames,
            rng=random.Random(derive_stream_seed(session_seed, "cycle:B")),
        )
        standalone_b = self.make_harness(
            channel="B",
            frames=frames,
            rng=random.Random(derive_stream_seed(session_seed, "cycle:B")),
        )
        await harness_a.runner.submit(CycleDirective("A", "evt-a", "呼吸", 20))
        await harness_b.runner.submit(CycleDirective("B", "evt-b", "呼吸", 20))
        await standalone_b.runner.submit(CycleDirective("B", "evt-b", "呼吸", 20))

        extra_a = await harness_a.complete_cycle()
        self.assertEqual(extra_a.gap_tenths, 0)
        for _ in range(10):
            await harness_b.complete_cycle()
            await standalone_b.complete_cycle()

        self.assertEqual(
            [record.gap_tenths for record in harness_b.records[:10]],
            [record.gap_tenths for record in standalone_b.records[:10]],
        )


if __name__ == "__main__":
    unittest.main()
