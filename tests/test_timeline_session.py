import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from backend.timeline.models import SessionStatus
from tests.timeline_fakes import SessionHarness, make_replay_bundle


class SessionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def _install_slow_cancel_watcher(self, controller, channel):
        original = controller._runner_watchers.pop(channel)
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        cancellation_started = asyncio.Event()

        async def delay_first_cancellation():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await asyncio.Future()

        watcher = asyncio.create_task(delay_first_cancellation())
        controller._runner_watchers[channel] = watcher
        await asyncio.sleep(0)
        return cancellation_started

    async def test_pause_clears_both_channels_but_does_not_save(self):
        controller = SessionHarness.create(seed=9)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )

        await controller.pause()

        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])
        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)

    async def test_pause_clear_failure_blocks_output_until_pause_retries(self):
        controller = SessionHarness.create(seed=22)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.game_loop.clear_failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.pause()

        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.resume()
        await controller.pause()
        await controller.resume()
        self.assertEqual(controller.clear_calls, [None, None])
        self.assertEqual(controller.to_state().status, SessionStatus.RUNNING)

    async def test_false_live_clear_result_keeps_transition_pending(self):
        controller = SessionHarness.create(seed=221)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        clear_output = controller.game_loop.clear_output
        clear_attempts = 0

        async def false_once(channel=None):
            nonlocal clear_attempts
            clear_attempts += 1
            if clear_attempts == 1:
                controller.game_loop.clear_calls.append(channel)
                return [], [
                    {
                        "action": {"op": "stop"},
                        "reason": "injected false clear",
                        "sent": False,
                    }
                ]
            return await clear_output(channel)

        controller.game_loop.clear_output = false_once

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.pause()

        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        await controller.pause()
        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.clear_calls, [None, None])

    async def test_cancelled_pause_before_quiesce_is_retryable(self):
        controller = SessionHarness.create(seed=33)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        cancellation_started = await self._install_slow_cancel_watcher(
            controller, "A"
        )

        pause = asyncio.create_task(controller.pause())
        await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
        pause.cancel()
        result = await asyncio.gather(pause, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        self.assertEqual(controller.clear_calls, [])
        cycle_count = len(controller.game_loop.requested_cycle_actions)

        await controller.pause()
        controller.clock.advance(10000)
        await asyncio.sleep(0)

        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.game_loop.safety.current["A"], 0)
        self.assertEqual(
            len(controller.game_loop.requested_cycle_actions), cycle_count
        )

    async def test_cancel_during_runner_quiesce_clears_after_worker_terminal(self):
        controller = SessionHarness.create(seed=35)
        self.addAsyncCleanup(controller.close)
        execute_actions = controller.game_loop.execute_actions
        second_cycle_started = asyncio.Event()
        cycle_calls = 0

        async def cancellation_aware_execute(actions):
            nonlocal cycle_calls
            if any(action.get("op") == "pulse_cycle" for action in actions):
                cycle_calls += 1
                if cycle_calls == 2:
                    second_cycle_started.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        await execute_actions(actions)
                        raise
            return await execute_actions(actions)

        controller.game_loop.execute_actions = cancellation_aware_execute
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        remaining = controller.clock.next_remaining_ms
        self.assertIsNotNone(remaining)
        controller.clock.advance(remaining)
        await asyncio.wait_for(second_cycle_started.wait(), timeout=0.2)
        runner = controller.runners["A"]
        await runner._lock.acquire()
        pause_started = asyncio.Event()
        runner_pause = runner.pause

        async def signaled_pause(*, reason):
            pause_started.set()
            await runner_pause(reason=reason)

        runner.pause = signaled_pause
        pause = asyncio.create_task(controller.pause())
        await asyncio.wait_for(pause_started.wait(), timeout=0.2)
        pause.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
            if controller.clear_calls:
                break
        runner._lock.release()
        result = await asyncio.gather(pause, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(
            controller.game_loop.operation_log,
            ["cycle:A", "cycle:A", "clear:*"],
        )
        self.assertEqual(controller.game_loop.safety.current["A"], 0)

        await controller.pause()

        self.assertEqual(controller.clear_calls, [None])

    async def test_cancelled_replacement_disconnect_settlement_retries_on_stop(self):
        controller = SessionHarness.create(seed=34)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        channel_watcher = controller._runner_watchers["A"]
        channel_watcher.cancel()
        await asyncio.gather(channel_watcher, return_exceptions=True)
        cancellation_started = await self._install_slow_cancel_watcher(
            controller, "B"
        )
        controller.game_loop.disconnect_on_cycle = 1
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.game_loop.disconnect_on_cycle = None

        replacement = asyncio.create_task(
            controller.process_live_turn(
                [{"op": "hold_strength", "channel": "A", "value": 21}]
            )
        )
        await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
        replacement.cancel()
        result = await asyncio.gather(replacement, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        self.assertEqual(controller.clear_calls, [])

        await controller.stop()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None])

    async def test_finish_clear_failure_can_retry_finish_without_early_save(self):
        controller = SessionHarness.create(seed=23)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        controller.game_loop.clear_failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.finish()

        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        self.assertEqual(controller.store.list(), [])
        summary = await controller.finish()
        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(
            [item.replay_id for item in controller.store.list()],
            [summary.replay_id],
        )
        self.assertEqual(controller.clear_calls, [None, None])

    async def test_finish_returns_in_memory_summary_without_second_store_read(self):
        controller = SessionHarness.create(seed=231)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.complete_next_cycle("A")

        with patch.object(
            controller.store,
            "summary",
            side_effect=AssertionError("finish must not reload the saved archive"),
        ):
            summary = await controller.finish()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        saved = controller.store.load(summary.replay_id)
        self.assertEqual(summary.cycle_count, len(saved.timeline.cycles))
        self.assertEqual(summary.title, "回放 " + summary.replay_id[:8])

    async def test_stop_retries_clear_after_failed_finish_before_idle(self):
        controller = SessionHarness.create(seed=24)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        controller.game_loop.clear_failures_remaining = 2
        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.finish()

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.stop()
        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        await controller.stop()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None, None, None])
        self.assertEqual(controller.store.list(), [])

    async def test_finish_saves_generated_gaps_without_manual_pause_time(self):
        controller = SessionHarness.create(seed=9)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.complete_cycles("A", gap_tenths=[0, 7, 20])
        await controller.pause(manual_elapsed_ms=600000)
        await controller.resume()

        summary = await controller.finish()

        timeline = controller.store.load(summary.replay_id).timeline
        self.assertEqual([cycle.gap_tenths for cycle in timeline.cycles], [0, 7, 20])
        self.assertTrue(
            all(cycle.active_start_offset_ms < 600000 for cycle in timeline.cycles)
        )
        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)

    async def test_archive_offsets_exclude_pause_time_exactly(self):
        controller = SessionHarness.create(seed=42)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.complete_cycles("A", gap_tenths=[0, 7])
        await controller.pause(manual_elapsed_ms=600000)
        await controller.resume()
        await controller.complete_next_cycle("A")

        summary = await controller.finish()
        cycles = controller.store.load(summary.replay_id).timeline.cycles

        self.assertEqual(
            [cycle.active_start_offset_ms for cycle in cycles],
            [0, 200, 540],
        )

    async def test_disconnect_pauses_and_resume_starts_complete_cycle(self):
        controller = SessionHarness.create(seed=10)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.begin_partial_cycle("A")
        old_runner = controller.runners["A"]

        await controller.on_disconnect()
        await controller.resume()

        self.assertIsNot(controller.runners["A"], old_runner)
        self.assertEqual(controller.last_cycle_started_at_frame, 0)
        self.assertEqual(controller.store.list(), [])

    async def test_resume_reuses_cycle_rng_progress_without_duplicate_records(self):
        controller = SessionHarness.create(seed=18)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.gap_rngs["A"].feed([7, 20])
        await controller.complete_next_cycle("A")
        await controller.pause()
        await controller.resume()
        await controller.complete_next_cycle("A")

        summary = await controller.finish()

        cycles = controller.store.load(summary.replay_id).timeline.cycles
        self.assertEqual([cycle.gap_tenths for cycle in cycles], [7, 20])
        self.assertEqual(controller.gap_rngs["A"].calls, 2)
        self.assertEqual(
            len({(cycle.channel, cycle.cycle_index) for cycle in cycles}),
            len(cycles),
        )

    async def test_duplicate_callback_delivery_upserts_one_archive_record(self):
        controller = SessionHarness.create(seed=20)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.gap_rngs["A"].feed([7])
        await controller.complete_next_cycle("A")

        controller.redeliver_latest_cycle("A")
        summary = await controller.finish()

        cycles = controller.store.load(summary.replay_id).timeline.cycles
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].gap_tenths, 7)

    async def test_repeated_pause_and_disconnect_do_not_repeat_clear_or_save(self):
        controller = SessionHarness.create(seed=11)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )

        await controller.pause()
        await controller.pause()
        await controller.on_disconnect()

        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])

    async def test_resume_does_not_bypass_existing_emergency_stop(self):
        controller = SessionHarness.create(seed=17)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        await controller.pause()
        controller.game_loop.safety.estop_active = True

        with self.assertRaisesRegex(RuntimeError, "emergency stop"):
            await controller.resume()
        with self.assertRaisesRegex(RuntimeError, "emergency stop"):
            await controller.start_live()

        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.clear_calls, [None])

    async def test_pause_clears_during_an_in_flight_initial_submission(self):
        controller = SessionHarness.create(seed=21)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        execute_actions = controller.game_loop.execute_actions
        cycle_started = asyncio.Event()
        release_cycle = asyncio.Event()

        async def execute_with_blocked_cycle(actions):
            if any(action.get("op") == "pulse_cycle" for action in actions):
                cycle_started.set()
                await release_cycle.wait()
            return await execute_actions(actions)

        controller.game_loop.execute_actions = execute_with_blocked_cycle
        turn = asyncio.create_task(
            controller.process_live_turn(
                [{"op": "hold_strength", "channel": "A", "value": 20}]
            )
        )
        await asyncio.wait_for(cycle_started.wait(), timeout=0.2)
        pause = asyncio.create_task(controller.pause())
        try:
            for _ in range(20):
                if controller.clear_calls:
                    break
                await asyncio.sleep(0)
            self.assertEqual(controller.clear_calls, [None])
        finally:
            release_cycle.set()
            await asyncio.gather(turn, pause, return_exceptions=True)

    async def test_runner_disconnect_escalates_to_session_pause_and_clear(self):
        controller = SessionHarness.create(seed=12)
        self.addAsyncCleanup(controller.close)
        controller.game_loop.disconnect_on_cycle = 1
        await controller.start_live()

        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        for _ in range(20):
            if controller.to_state().status is SessionStatus.PAUSED:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])

    async def test_runner_executor_failure_clears_only_failed_channel(self):
        controller = SessionHarness.create(seed=16)
        self.addAsyncCleanup(controller.close)
        controller.game_loop.fail_on_cycle = 1
        await controller.start_live()

        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        for _ in range(20):
            if controller.clear_calls:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.clear_calls, ["A"])
        self.assertEqual(controller.to_state().status, SessionStatus.RUNNING)
        self.assertEqual(controller.store.list(), [])

    async def test_replacement_cannot_cancel_pending_failed_runner_clear(self):
        controller = SessionHarness.create(seed=25)
        self.addAsyncCleanup(controller.close)
        controller.game_loop.fail_on_cycle = 1
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.game_loop.fail_on_cycle = None
        self.assertEqual(controller.game_loop.operation_log, [])

        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 21}]
        )

        self.assertEqual(controller.game_loop.operation_log, ["clear:A", "cycle:A"])

    async def test_replacement_cannot_cancel_pending_disconnect_pause(self):
        controller = SessionHarness.create(seed=26)
        self.addAsyncCleanup(controller.close)
        controller.game_loop.disconnect_on_cycle = 1
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        controller.game_loop.disconnect_on_cycle = None
        self.assertEqual(controller.game_loop.operation_log, [])

        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 21}]
        )

        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.game_loop.operation_log, ["clear:*"])
        self.assertEqual(controller.game_loop.requested_cycle_actions, [])

    async def test_replacement_runner_waits_for_failed_channel_clear(self):
        controller = SessionHarness.create(seed=19)
        self.addAsyncCleanup(controller.close)
        controller.game_loop.fail_on_cycle = 1
        controller.game_loop.block_channel_clear = True
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        await asyncio.wait_for(controller.game_loop.clear_started.wait(), timeout=0.2)

        replacement = asyncio.create_task(
            controller.process_live_turn(
                [{"op": "hold_strength", "channel": "A", "value": 21}]
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertFalse(replacement.done())
        self.assertEqual(controller.game_loop.requested_cycle_actions, [])
        controller.game_loop.release_clear.set()
        await asyncio.wait_for(replacement, timeout=0.2)
        self.assertEqual(
            [action["channel"] for action in controller.game_loop.requested_cycle_actions],
            ["A"],
        )

    async def test_plot_stop_clears_only_requested_channel_immediately(self):
        controller = SessionHarness.create(seed=13)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        await controller.process_live_turn(
            [{"op": "hold_strength", "channel": "B", "value": 20}]
        )

        await controller.process_live_turn([{"op": "clear", "channel": "B"}])

        self.assertEqual(controller.clear_calls, ["B"])
        self.assertEqual(controller.to_state().status, SessionStatus.RUNNING)

    async def test_replay_resume_keeps_completion_tracking(self):
        controller = SessionHarness.create(seed=14)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0, 0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            if controller.player is not None and controller.player.cursor == 1:
                break
            await asyncio.sleep(0)

        await controller.pause()
        await controller.resume(cursor=1)
        for _ in range(40):
            if controller.to_state().status is SessionStatus.IDLE:
                break
            remaining = controller.clock.next_remaining_ms
            if remaining is not None:
                controller.clock.advance(remaining)
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(len(controller.store.list()), 1)

    async def test_replay_state_exposes_redacted_channel_playback(self):
        controller = SessionHarness.create(seed=41)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([5], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)

        state = await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            state = controller.to_state()
            if state.channels["A"].phase == "cycle":
                break
            await asyncio.sleep(0)

        channel = state.channels["A"]
        self.assertEqual(channel.phase, "cycle")
        self.assertEqual(channel.pattern, "呼吸")
        self.assertEqual(channel.strength, 20)
        self.assertEqual(channel.cycle_index, bundle.timeline.cycles[0].cycle_index)
        self.assertIsNone(channel.next_cycle_start_ms)
        self.assertNotIn("waveform_hash", state.to_dict()["channels"]["A"])

        await controller.pause()
        paused = controller.to_state().channels["A"]
        self.assertEqual(paused.phase, "paused")
        self.assertIsNone(paused.pattern)
        self.assertEqual(paused.strength, 0)

    async def test_legacy_replay_is_adjusted_when_provenance_cannot_be_confirmed(self):
        controller = SessionHarness.create(seed=141)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([20], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)

        state = await controller.start_replay(bundle.manifest.replay_id)

        self.assertTrue(state.adjusted)

    async def test_replay_provenance_mismatch_is_adjusted(self):
        controller = SessionHarness.create(seed=142)
        self.addAsyncCleanup(controller.close)
        controller._manifest_metadata.update(
            {"app_fingerprint": "current-app", "dlc_fingerprint": "current-dlc"}
        )
        bundle = make_replay_bundle([20], "completed")
        manifest = replace(
            bundle.manifest,
            app_fingerprint="current-app",
            dlc_fingerprint="archived-dlc",
        )
        controller.store.save(manifest, bundle.timeline)

        state = await controller.start_replay(manifest.replay_id)

        self.assertTrue(state.adjusted)

    async def test_empty_replay_clears_then_returns_controller_to_idle(self):
        controller = SessionHarness.create(seed=27)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)

        await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            if controller.to_state().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None])

    async def test_start_replay_rejects_none_cursor_before_output(self):
        controller = SessionHarness.create(seed=36)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)

        result = await asyncio.gather(
            controller.start_replay(bundle.manifest.replay_id, cursor=None),
            return_exceptions=True,
        )
        await asyncio.sleep(0)

        self.assertEqual(controller.game_loop.requested_cycle_actions, [])
        self.assertIsInstance(result[0], ValueError)
        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)

    async def test_replay_resume_at_end_replaces_completion_tracking(self):
        controller = SessionHarness.create(seed=28)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0, 0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            if controller.player is not None and controller.player.cursor == 1:
                break
            await asyncio.sleep(0)
        await controller.pause()

        await controller.resume(cursor=2)
        for _ in range(20):
            if controller.to_state().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None, None])

    async def test_resume_in_completion_watcher_window_is_idempotent(self):
        controller = SessionHarness.create(seed=32)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        await controller.start_replay(bundle.manifest.replay_id)
        player = controller.player
        self.assertIsNotNone(player)

        await controller._lock.acquire()
        try:
            resume = asyncio.create_task(controller.resume(cursor=0))
            await asyncio.sleep(0)
            for _ in range(20):
                remaining = controller.clock.next_remaining_ms
                if remaining is not None:
                    controller.clock.advance(remaining)
                await asyncio.sleep(0)
                if player is not None and not player.running:
                    break
            self.assertIsNotNone(player)
            self.assertFalse(player.running)
            self.assertEqual(len(controller.game_loop.requested_cycle_actions), 1)
        finally:
            controller._lock.release()

        await asyncio.wait_for(resume, timeout=0.2)
        for _ in range(20):
            if controller.to_state().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertFalse(player.running)
        self.assertEqual(len(controller.game_loop.requested_cycle_actions), 1)

    async def test_running_controller_still_validates_replay_cursor(self):
        controller = SessionHarness.create(seed=29)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0, 0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        await controller.start_replay(bundle.manifest.replay_id)

        with self.assertRaisesRegex(ValueError, "cursor"):
            await controller.resume(cursor=3)

    async def test_running_live_controller_rejects_replay_cursor(self):
        controller = SessionHarness.create(seed=30)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()

        with self.assertRaisesRegex(ValueError, "cursor"):
            await controller.resume(cursor=0)

    async def test_replay_executor_failure_returns_to_paused_after_clear(self):
        controller = SessionHarness.create(seed=15)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        controller.game_loop.fail_on_cycle = 1

        await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            if controller.to_state().status is SessionStatus.PAUSED:
                break
            await asyncio.sleep(0)

        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(len(controller.store.list()), 1)

    async def test_replay_stop_retries_failed_clear_before_idle(self):
        controller = SessionHarness.create(seed=31)
        self.addAsyncCleanup(controller.close)
        bundle = make_replay_bundle([0], "completed")
        controller.store.save(bundle.manifest, bundle.timeline)
        controller.game_loop.fail_on_cycle = 1
        controller.game_loop.clear_failures_remaining = 2
        await controller.start_replay(bundle.manifest.replay_id)
        for _ in range(20):
            if controller.to_state().status is SessionStatus.FINISHING:
                break
            await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await controller.stop()
        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        await controller.stop()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None, None, None])


if __name__ == "__main__":
    unittest.main()
