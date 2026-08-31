import asyncio
import random
import unittest

from backend.timeline.cycle_runner import CycleDirective, RunnerPhase
from backend.timeline.randomizer import derive_stream_seed
from tests.timeline_fakes import (
    BlockingCycleCallback,
    CycleHarness,
    DeferredRunnerStart,
    DurableCycleRecorder,
    SequenceGapRandom,
)


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

    async def test_stop_clears_before_waiting_for_blocked_record_callback(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"] * 12}, on_cycle=callback
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        stop_task = asyncio.create_task(
            harness.runner.stop(clear=True, reason="estop")
        )
        try:
            await asyncio.wait_for(callback.entered.wait(), timeout=0.2)
            self.assertEqual(harness.clear_calls, ["A"])
            self.assertFalse(stop_task.done())
        finally:
            callback.release.set()
            await asyncio.wait_for(stop_task, timeout=0.2)

    async def test_cancelled_public_stop_still_finishes_clear_and_worker_teardown(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"] * 12},
            on_cycle=callback,
            block_clear=True,
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        stop_task = asyncio.create_task(
            harness.runner.stop(clear=True, reason="estop")
        )
        await asyncio.wait_for(harness.executor.clear_started.wait(), timeout=0.2)
        await asyncio.wait_for(callback.entered.wait(), timeout=0.2)
        stop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        harness.executor.release_clear.set()
        callback.release.set()
        state = await asyncio.wait_for(harness.runner.wait_stopped(), timeout=0.2)

        self.assertEqual(state.phase, RunnerPhase.STOPPED)
        self.assertFalse(state.worker_active)
        self.assertEqual(harness.clear_calls, ["A"])

    async def test_concurrent_estop_upgrades_active_stop_to_one_clear(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"] * 12}, on_cycle=callback
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))

        stop_task = asyncio.create_task(
            harness.runner.stop(clear=False, reason="operator_stop")
        )
        await asyncio.wait_for(callback.entered.wait(), timeout=0.2)
        estop_task = asyncio.create_task(
            harness.runner.stop(clear=True, reason="estop")
        )
        try:
            await harness.flush()
            self.assertEqual(harness.clear_calls, ["A"])
        finally:
            callback.release.set()
            await asyncio.wait_for(
                asyncio.gather(stop_task, estop_task), timeout=0.2
            )

        self.assertEqual(harness.clear_calls, ["A"])
        self.assertEqual(harness.runner.state().phase, RunnerPhase.STOPPED)

    async def test_cancelled_record_callback_stops_with_record_preserved(self):
        async def cancel_callback(record):
            raise asyncio.CancelledError

        harness = self.make_harness(
            frames={"呼吸": ["f"]}, gap_tenths=[0], on_cycle=cancel_callback
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        harness.sleeper.advance(100)
        await harness.flush()

        state = harness.runner.state()
        self.assertEqual(state.phase, RunnerPhase.STOPPED)
        self.assertIn("callback", (state.failure or "").lower())
        self.assertEqual(len(harness.runner.pending_records()), 1)

    async def test_record_callback_exception_surfaces_as_stopped_failure(self):
        async def fail_callback(record):
            raise RuntimeError("recorder failed")

        harness = self.make_harness(
            frames={"呼吸": ["f"]}, gap_tenths=[0], on_cycle=fail_callback
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        harness.sleeper.advance(100)
        await harness.flush()

        state = await asyncio.wait_for(harness.runner.wait_stopped(), timeout=0.2)
        self.assertEqual(state.phase, RunnerPhase.STOPPED)
        self.assertIn("recorder failed", state.failure or "")
        self.assertEqual(len(harness.runner.pending_records()), 1)

    async def test_failed_record_delivery_is_durable_and_recoverable(self):
        for failure in ("raise", "cancel"):
            with self.subTest(failure=failure):
                recorder = DurableCycleRecorder(failure)
                harness = self.make_harness(
                    frames={"呼吸": ["f"]},
                    gap_tenths=[0],
                    on_cycle=recorder,
                )
                await harness.runner.submit(
                    CycleDirective("A", f"evt-{failure}", "呼吸", 20)
                )
                harness.sleeper.advance(100)
                state = await asyncio.wait_for(
                    harness.runner.wait_stopped(), timeout=0.2
                )

                self.assertEqual(recorder.records, {})
                pending = harness.runner.pending_records()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].cycle_index, 1)
                self.assertEqual(state.phase, RunnerPhase.STOPPED)

                recorder.failure = "none"
                await harness.runner.retry_pending_records()
                self.assertEqual(recorder.records[("A", 1)], pending[0])
                self.assertEqual(harness.runner.pending_records(), ())
                self.assertEqual(harness.runner.state().phase, RunnerPhase.STOPPED)
                await harness.runner.retry_pending_records()
                self.assertEqual(len(recorder.records), 1)

    async def test_latest_pending_normal_change_wins(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"], "潮汐": ["g"], "律动": ["h"]}
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 22))
        await harness.runner.submit(CycleDirective("A", "evt-3", "律动", 24))

        await harness.complete_cycle()

        self.assertEqual(harness.sent_patterns, ["呼吸", "律动"])

    async def test_zero_gap_boundary_rechecks_pending_before_next_pulse(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"], "潮汐": ["g"]},
            gap_tenths=[0],
            on_cycle=callback,
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        harness.sleeper.advance(100)
        await asyncio.wait_for(callback.entered.wait(), timeout=0.2)

        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 22))
        callback.release.set()
        await harness.flush()

        self.assertEqual(harness.sent_patterns[:2], ["呼吸", "潮汐"])

    async def test_directive_during_callback_supersedes_older_boundary_pending(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"], "潮汐": ["g"], "律动": ["h"]},
            on_cycle=callback,
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.submit(CycleDirective("A", "evt-2", "潮汐", 22))
        harness.sleeper.advance(100)
        await asyncio.wait_for(callback.entered.wait(), timeout=0.2)

        await harness.runner.submit(CycleDirective("A", "evt-3", "律动", 24))
        callback.release.set()
        await harness.flush()

        self.assertEqual(harness.sent_patterns[:2], ["呼吸", "律动"])

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

    async def test_stop_before_worker_first_step_releases_initial_submit(self):
        harness = self.make_harness(frames={"呼吸": ["f"]})
        loop = asyncio.get_running_loop()
        deferred_start = DeferredRunnerStart()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(deferred_start)
        self.addCleanup(loop.set_task_factory, previous_factory)
        submit_task = asyncio.create_task(
            harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        )
        await asyncio.wait_for(deferred_start.created.wait(), timeout=0.2)

        stop_task = asyncio.create_task(
            harness.runner.stop(clear=False, reason="concurrent_stop")
        )
        results = await asyncio.wait_for(
            asyncio.gather(submit_task, stop_task, return_exceptions=True),
            timeout=0.2,
        )

        self.assertNotIsInstance(results[0], asyncio.TimeoutError)
        self.assertEqual(harness.runner.state().phase, RunnerPhase.STOPPED)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await harness.runner.submit(CycleDirective("A", "evt-2", "呼吸", 20))

    async def test_stopped_after_rejection_is_terminal_across_pause_resume(self):
        harness = self.make_harness(frames={"呼吸": ["f"]}, fail_on_cycle=1)
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.runner.wait_stopped()
        stopped_state = harness.runner.state()

        await harness.runner.pause(reason="late_pause")
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await harness.runner.resume()

        final_state = harness.runner.state()
        self.assertEqual(final_state.phase, RunnerPhase.STOPPED)
        self.assertEqual(final_state.failure, stopped_state.failure)
        self.assertEqual(final_state.disconnected, stopped_state.disconnected)
        self.assertEqual(harness.sent_patterns, [])

    async def test_executor_exception_during_activation_stops_recordlessly(self):
        harness = self.make_harness(
            frames={"呼吸": ["f"]}, raise_on_cycle=1
        )

        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        state = await harness.runner.wait_stopped()

        self.assertEqual(state.phase, RunnerPhase.STOPPED)
        self.assertIn("injected executor failure", state.failure or "")
        self.assertEqual(harness.records, [])
        self.assertEqual(harness.runner.pending_records(), ())

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

    async def test_old_paused_worker_cannot_emit_or_clear_resumed_cycle_record(self):
        callback = BlockingCycleCallback()
        harness = self.make_harness(
            frames={"呼吸": ["f"]}, gap_tenths=[0, 0], on_cycle=callback
        )
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        harness.sleeper.advance(100)
        await asyncio.wait_for(callback.entered.wait(), timeout=0.2)

        pause_task = asyncio.create_task(
            harness.runner.pause(reason="operator_pause")
        )
        for _ in range(10):
            if harness.runner.state().phase is RunnerPhase.PAUSED:
                break
            await asyncio.sleep(0)
        self.assertEqual(harness.runner.state().phase, RunnerPhase.PAUSED)

        await harness.runner.resume()
        self.assertEqual(harness.sent_patterns, ["呼吸", "呼吸"])
        callback.release.set()
        await asyncio.wait_for(pause_task, timeout=0.2)
        await harness.flush()

        self.assertEqual(
            [(record.cycle_index, record.completed) for record in callback.records],
            [(1, True)],
        )
        self.assertEqual(harness.runner.pending_records(), ())
        self.assertEqual(harness.runner.state().phase, RunnerPhase.CYCLE)

        harness.sleeper.advance(100)
        await harness.flush()
        self.assertEqual(
            [(record.cycle_index, record.completed) for record in callback.records],
            [(1, True), (2, True)],
        )

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

    async def test_activation_selects_effective_strength_for_exact_action_and_channel(self):
        harness = self.make_harness(
            channel="A",
            frames={"呼吸": ["f"]},
            effective_strength=18,
        )
        execute = harness.executor.execute

        async def execute_with_unrelated_hold(actions):
            executed, dropped = await execute(actions)
            executed.insert(
                0,
                {
                    "action": {"op": "hold_strength", "channel": "B", "value": 99},
                    "effective": {
                        "op": "hold_strength",
                        "channel": "B",
                        "requested_strength": 99,
                        "effective_strength": 99,
                    },
                },
            )
            return executed, dropped

        harness.executor.execute = execute_with_unrelated_hold
        await harness.runner.submit(CycleDirective("A", "evt-1", "呼吸", 20))
        await harness.complete_cycle()

        self.assertEqual(harness.records[0].effective_strength, 18)

    async def test_missing_or_invalid_effective_strength_stops_runner(self):
        cases = (
            {"omit_effective_strength": True},
            {"invalid_effective_strength": True},
        )
        for index, executor_case in enumerate(cases, 1):
            with self.subTest(executor_case=executor_case):
                harness = self.make_harness(
                    frames={"呼吸": ["f"]}, **executor_case
                )
                await harness.runner.submit(
                    CycleDirective("A", f"evt-{index}", "呼吸", 20)
                )

                self.assertEqual(
                    harness.runner.state().phase, RunnerPhase.STOPPED
                )
                state = await harness.runner.wait_stopped()
                self.assertIn("effective_strength", state.failure or "")
                self.assertEqual(harness.records, [])
                self.assertEqual(harness.runner.pending_records(), ())

    async def test_invalid_activation_result_takes_precedence_over_rejection_record(self):
        cases = (
            {"omit_effective_strength": True, "fail_on_cycle": 1},
            {"invalid_effective_strength": True, "fail_on_cycle": 1},
        )
        for index, executor_case in enumerate(cases, 1):
            with self.subTest(executor_case=executor_case):
                harness = self.make_harness(
                    frames={"呼吸": ["f"]}, **executor_case
                )
                await harness.runner.submit(
                    CycleDirective("A", f"evt-rejected-{index}", "呼吸", 20)
                )

                state = await harness.runner.wait_stopped()
                self.assertEqual(state.phase, RunnerPhase.STOPPED)
                self.assertIn("effective_strength", state.failure or "")
                self.assertEqual(harness.records, [])
                self.assertEqual(harness.runner.pending_records(), ())

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
