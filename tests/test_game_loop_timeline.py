import asyncio
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.config import DEFAULTS
from backend.game_loop import GameLoop
from backend.safety import DeviceOutputError, SafetyManager
from backend.timeline.cycle_runner import RunnerPhase
from backend.timeline.models import CycleGapPolicy
from backend.timeline.replay_store import ReplayStore
from backend.timeline.session import SessionController
from tests.timeline_fakes import (
    ControlledSleeper,
    FakeTimelineResolver,
    SequenceGapRandom,
)


class FakeRelay:
    def __init__(self) -> None:
        self.status = "paired"
        self.controller_id = "controller-test"
        self.clients = {}
        self.sent_frames = []
        self._strength_failures = []
        self._clear_failures = []

    def first_client_id(self):
        return "client-test"

    def get_slot_id(self, _client_id=None):
        return "slot-test"

    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        inner = frame.get("data", {}) if isinstance(frame, dict) else {}
        method = inner.get("m")
        data = inner.get("data", {}) if isinstance(inner, dict) else {}
        if method == "device.op" and data.get("t") == 3:
            channel = data.get("c")
            for index, (failed_channel, failure) in enumerate(
                self._strength_failures
            ):
                if failed_channel is None or failed_channel == channel:
                    self._strength_failures.pop(index)
                    if isinstance(failure, BaseException):
                        raise failure
                    return False
        if method == "device.op.clear":
            channel = data.get("c")
            for index, (failed_channel, failure) in enumerate(
                self._clear_failures
            ):
                if failed_channel is None or failed_channel == channel:
                    self._clear_failures.pop(index)
                    if isinstance(failure, BaseException):
                        raise failure
                    return False
        return True

    def fail_next_strength_delta(self, channel=None, failure=False):
        numeric = {"A": 0, "B": 1}.get(channel, channel)
        self._strength_failures.append((numeric, failure))

    def fail_next_clear(self, channel=None, failure=False):
        numeric = {"A": 0, "B": 1}.get(channel, channel)
        self._clear_failures.append((numeric, failure))

    def to_state(self):
        return {
            "status": self.status,
            "controller_id": self.controller_id,
            "url": "ws://relay.test",
            "clients": [],
            "last_error": "",
        }


def relay_output_operations(frames):
    """Decode the real DeviceOps frames at the GameLoop relay boundary."""
    operations = []
    for frame in frames:
        inner = frame["data"]
        method = inner["m"]
        data = inner.get("data") or {}
        if method == "device.op.clear":
            operations.append(("clear", data.get("c")))
        elif method == "device.op" and data.get("t") == 0:
            operations.append(("waveform", data["c"], data["d"]))
        elif method == "device.op" and data.get("t") == 3:
            operations.append(("strength_delta", data["c"], data["v"]))
        elif method == "device.op" and data.get("t") == 7:
            operations.append(("reset", data["c"]))
    return operations


def make_game_loop_for_test(
    replay_root: Path,
    *,
    autopilot_interval: float = 12,
    gap_tenths=(5,),
):
    cfg = deepcopy(DEFAULTS)
    cfg["app"]["dry_run"] = True
    cfg["autopilot"] = {"enabled": False, "interval_s": autopilot_interval}
    cfg["character_file"] = str(replay_root / "private-character.yaml")
    cfg["log_dir"] = str(replay_root / "private-logs")
    cfg["character"] = {
        "name": "Timeline Test",
        "role": "触手",
        "role_title": "主人",
        "roles": [
            {
                "name": "触手",
                "label": "触手",
                "profiles": [{"name": "纯爱", "available": True}],
            },
            {
                "name": "装置",
                "label": "装置",
                "profiles": [{"name": "调教", "available": True}],
            },
        ],
        "profile": "纯爱",
        "profiles": ["纯爱"],
        "profile_available": {"纯爱": True},
        "profile_level": "中",
        "rage_baseline": 0,
        "player_nick": "tester",
    }
    raw_frames = ["RAW_FRAME_SECRET_0"]
    cfg["presets"]["呼吸"] = {
        "waveform": "wave_test",
        "label": "呼吸",
        "category": "test",
        "frames": raw_frames,
        "default_duration_s": 5,
        "max_duration_s": 10,
    }
    cfg["llm"]["api_key"] = "API_KEY_SECRET"

    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=(
                "timeline line",
                [{"op": "hold_strength", "channel": "A", "value": 20}],
            )
        )
    )
    safety = SafetyManager(cfg)
    relay = FakeRelay()
    loop = GameLoop(cfg, llm, safety, relay)
    clock = ControlledSleeper()
    resolver = FakeTimelineResolver()
    cycle_rngs = {
        "A": SequenceGapRandom(gap_tenths),
        "B": SequenceGapRandom(()),
    }
    store = ReplayStore(replay_root)
    controller = SessionController(
        game_loop=loop,
        store=store,
        seed=20260831,
        frames={"呼吸": tuple(raw_frames)},
        strength_jitter=4,
        cycle_gap_policy=CycleGapPolicy(),
        clock=lambda: clock.now_ms / 1000,
        sleeper=clock,
        resolver_factory=lambda _seed: resolver,
        cycle_rngs=cycle_rngs,
        timestamp_factory=lambda: "2026-08-31T00:00:00+00:00",
    )
    loop.timeline_session = controller
    return SimpleNamespace(
        cfg=cfg,
        llm=llm,
        safety=safety,
        relay=relay,
        loop=loop,
        clock=clock,
        resolver=resolver,
        cycle_rngs=cycle_rngs,
        store=store,
        controller=controller,
    )


class GameLoopTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.harness = make_game_loop_for_test(Path(self.temporary))

    async def asyncTearDown(self):
        with suppress(Exception):
            await self.harness.controller.stop()

    async def _automatic_turn(self):
        with patch("backend.game_loop.reload_character"):
            return await self.harness.loop._autopilot_turn()

    async def _start_physical_live(self, strength):
        self.harness.safety.dry_run = False
        self.harness.llm.chat.return_value = (
            "timeline line",
            [{"op": "hold_strength", "channel": "A", "value": strength}],
        )
        await self.harness.controller.start_live()
        await self._automatic_turn()
        self.assertEqual(self.harness.safety.current["A"], strength)

    async def _advance_until_phase(self, channel, phase):
        for _ in range(20):
            if self.harness.controller.runners[channel].state().phase is phase:
                return
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.fail(f"runner {channel} did not reach {phase.value}")

    async def _complete_active_cycle(self, channel):
        initial = sum(
            record.channel == channel and record.completed
            for record in self.harness.controller.recorded_cycles
        )
        for _ in range(60):
            completed = [
                record
                for record in self.harness.controller.recorded_cycles
                if record.channel == channel and record.completed
            ]
            if len(completed) > initial:
                return
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.fail("active cycle did not complete")

    async def _assert_live_turn_finishing_late_is_discarded(self, invoke):
        entered_llm = asyncio.Event()
        release_llm = asyncio.Event()

        async def delayed_chat(*_args, **_kwargs):
            entered_llm.set()
            await release_llm.wait()
            return (
                "late timeline line",
                [{"op": "hold_strength", "channel": "A", "value": 20}],
            )

        await self.harness.controller.start_live()
        self.harness.loop.turn_count = 1
        self.harness.llm.chat.side_effect = delayed_chat
        with patch("backend.game_loop.reload_character"):
            turn = asyncio.create_task(invoke())
            await asyncio.wait_for(entered_llm.wait(), timeout=0.2)
            await self.harness.loop.finish_timeline_session()
            release_llm.set()
            result = await turn

        self.assertEqual(result["executed"], [])
        self.assertEqual(result["dropped"], [])
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.loop.patterns, {"A": None, "B": None})
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")

    async def test_user_turn_started_live_is_discarded_after_finish(self):
        await self._assert_live_turn_finishing_late_is_discarded(
            lambda: self.harness.loop.handle_user_message("hello")
        )

    async def test_auto_open_started_live_is_discarded_after_finish(self):
        await self._assert_live_turn_finishing_late_is_discarded(
            self.harness.loop.auto_open
        )

    async def test_observation_started_live_is_discarded_after_finish(self):
        await self._assert_live_turn_finishing_late_is_discarded(
            self.harness.loop._auto_observe_turn
        )

    async def test_autopilot_turn_started_live_is_discarded_after_finish(self):
        await self._assert_live_turn_finishing_late_is_discarded(
            self.harness.loop._autopilot_turn
        )

    async def test_automatic_turn_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        result = await self._automatic_turn()

        self.assertEqual(result["line"], "timeline line")
        self.assertEqual(self.harness.controller.to_state().cursor, 1)
        self.assertEqual(
            self.harness.controller.runners["A"].state().phase,
            RunnerPhase.CYCLE,
        )

    async def test_auto_open_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop.auto_open()

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_observation_turn_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop._auto_observe_turn()

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_user_turn_routes_ai_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop.handle_user_message("hello")

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_cycle_completion_uses_cycle_rng_without_another_llm_turn(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        self.harness.llm.chat.reset_mock()

        await self._advance_until_phase("A", RunnerPhase.GAP)

        self.assertEqual(len(self.harness.resolver.calls), 1)
        self.assertEqual(self.harness.cycle_rngs["A"].calls, 1)
        self.harness.llm.chat.assert_not_awaited()

    async def test_cap_reduction_during_raw_cycle_uses_only_safety_delta_before_runner_restart(self):
        await self._start_physical_live(30)
        self.assertIs(
            self.harness.controller.runners["A"].state().phase,
            RunnerPhase.CYCLE,
        )
        self.harness.safety.pulse_until["A"] = 0
        self.harness.relay.sent_frames.clear()

        await self.harness.loop.set_runtime_cap("A", 10)

        operations = relay_output_operations(self.harness.relay.sent_frames)
        self.assertEqual(
            [operation for operation in operations if operation[0] == "strength_delta"],
            [("strength_delta", 0, -20)],
        )
        self.assertNotIn(("waveform", 0, 30000), operations)
        self.assertNotIn("A", self.harness.loop.loop_tasks)
        self.assertEqual(
            self.harness.loop.output_coordinator.confirmed("A").strength,
            10,
        )

    async def test_cap_reduction_during_gap_does_not_install_default_waveform_loop(self):
        await self._start_physical_live(30)
        await self._advance_until_phase("A", RunnerPhase.GAP)
        self.harness.safety.pulse_until["A"] = 0
        self.harness.relay.sent_frames.clear()

        await self.harness.loop.set_runtime_cap("A", 10)

        operations = relay_output_operations(self.harness.relay.sent_frames)
        self.assertEqual(
            [operation for operation in operations if operation[0] == "strength_delta"],
            [("strength_delta", 0, -20)],
        )
        self.assertNotIn(("waveform", 0, 30000), operations)
        self.assertNotIn("A", self.harness.loop.loop_tasks)
        await self._complete_active_cycle("A")
        completed = [
            record
            for record in self.harness.controller.recorded_cycles
            if record.channel == "A" and record.completed
        ]
        self.assertEqual(completed[-1].effective_strength, 10)

    async def test_failed_disable_keeps_runner_terminal_and_clear_pending(self):
        await self._start_physical_live(25)
        self.harness.relay.sent_frames.clear()
        self.harness.relay.fail_next_clear("A")

        with self.assertRaises(DeviceOutputError):
            await self.harness.loop.set_channel_enabled("A", False)

        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(self.harness.safety.enabled["A"])
        self.assertEqual(self.harness.safety.current["A"], 25)
        frames_after_failure = len(self.harness.relay.sent_frames)
        for _ in range(20):
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.assertEqual(len(self.harness.relay.sent_frames), frames_after_failure)
        self.assertNotIn("A", self.harness.controller.runners)
        self.assertIn("B", self.harness.controller.runners)

    async def test_state_exposes_only_current_runner_schedule_fields(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        await self._advance_until_phase("A", RunnerPhase.GAP)

        state = self.harness.loop.build_state()

        self.assertEqual(
            set(state["session"]),
            {
                "status",
                "mode",
                "session_id",
                "replay_id",
                "cursor",
                "current_event_id",
                "adjusted",
                "channels",
            },
        )
        self.assertEqual(state["session"]["channels"], state["runners"])
        self.assertEqual(
            state["runners"]["A"],
            {
                "phase": "gap",
                "pattern": "呼吸",
                "strength": 20,
                "cycle_index": 1,
                "next_cycle_start_ms": 150,
            },
        )

    async def test_autopilot_loop_waits_the_configured_turn_interval(self):
        harness = make_game_loop_for_test(
            Path(self.temporary) / "cadence", autopilot_interval=12
        )
        self.addAsyncCleanup(harness.controller.stop)
        harness.safety.estop_active = True
        observed_timeouts = []

        async def record_wait(awaitable, *, timeout):
            observed_timeouts.append(timeout)
            awaitable.close()
            harness.loop.autopilot_stop.set()
            raise asyncio.TimeoutError

        with patch("backend.game_loop.asyncio.wait_for", side_effect=record_wait):
            await harness.loop._autopilot_loop()

        self.assertEqual(harness.loop.autopilot_interval, 12)
        self.assertEqual(observed_timeouts, [12])
        self.assertFalse(hasattr(harness.controller, "next_interval_s"))

    async def test_manual_pulse_controls_bypass_timeline_scheduling(self):
        await self.harness.controller.start_live()

        for action in (
            {
                "op": "pulse",
                "channel": "A",
                "pattern": "呼吸",
                "duration_s": 1,
            },
            {"op": "pulse_hold", "channel": "B", "pattern": "呼吸"},
        ):
            with self.subTest(op=action["op"]):
                executed, dropped = await self.harness.loop.execute_actions([action])
                self.assertEqual(dropped, [])
                self.assertEqual(len(executed), 1)

        self.assertEqual(self.harness.controller.to_state().cursor, 0)
        self.assertTrue(
            all(
                runner.state().phase is RunnerPhase.IDLE
                for runner in self.harness.controller.runners.values()
            )
        )

    async def test_paused_live_session_blocks_ai_actions_from_direct_fallback(self):
        await self.harness.controller.start_live()
        await self.harness.controller.pause()

        executed, dropped, timeline_managed = (
            await self.harness.loop._execute_ai_actions(
                [{"op": "hold_strength", "channel": "A", "value": 20}]
            )
        )

        self.assertTrue(timeline_managed)
        self.assertEqual(executed, [])
        self.assertEqual(dropped, [])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertEqual(self.harness.controller.to_state().cursor, 0)

    async def test_user_turn_started_during_replay_is_discarded_after_stop(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        await self._advance_until_phase("A", RunnerPhase.GAP)
        summary = await self.harness.controller.finish()
        await self.harness.controller.start_replay(summary.replay_id, cursor=0)

        entered_llm = asyncio.Event()
        release_llm = asyncio.Event()

        async def delayed_chat(*_args, **_kwargs):
            entered_llm.set()
            await release_llm.wait()
            return (
                "late replay line",
                [{"op": "hold_strength", "channel": "A", "value": 20}],
            )

        self.harness.loop.turn_count = 1
        self.harness.llm.chat.side_effect = delayed_chat
        with patch("backend.game_loop.reload_character"):
            turn = asyncio.create_task(
                self.harness.loop.handle_user_message("during replay")
            )
            await asyncio.wait_for(entered_llm.wait(), timeout=0.2)
            await self.harness.controller.stop()
            release_llm.set()
            result = await turn

        self.assertEqual(result["executed"], [])
        self.assertEqual(result["dropped"], [])
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.loop.patterns, {"A": None, "B": None})
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")

    async def _install_stubborn_autopilot_task(self):
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_task():
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()

        self.harness.loop.autopilot = True
        self.harness.loop.autopilot_task = asyncio.create_task(stubborn_task())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        return cancellation_seen, release

    async def _install_blocked_runner_watcher_teardown(self):
        original = self.harness.controller._runner_watchers.pop("A")
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        cancellation_started = asyncio.Event()
        release = asyncio.Event()

        async def watcher_blocked_during_teardown():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await release.wait()

        self.harness.controller._runner_watchers["A"] = asyncio.create_task(
            watcher_blocked_during_teardown()
        )
        await asyncio.sleep(0)
        return cancellation_started, release

    async def test_cancelled_automatic_off_has_already_paused_and_cleared(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        cancellation_seen, release = await self._install_stubborn_autopilot_task()

        stopping = asyncio.create_task(self.harness.loop.set_autopilot(False))
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
        stopping.cancel()
        result = await asyncio.gather(stopping, return_exceptions=True)
        release.set()

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.controller.to_state().status.value, "paused")
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.store.list(), [])

    async def test_cancelled_finish_wrapper_has_already_finished_and_cleared(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        cancellation_seen, release = await self._install_stubborn_autopilot_task()

        finishing = asyncio.create_task(
            self.harness.loop.finish_timeline_session()
        )
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
        finishing.cancel()
        result = await asyncio.gather(finishing, return_exceptions=True)
        release.set()

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(len(self.harness.store.list()), 1)

    async def test_cancelled_automatic_off_awaits_controller_teardown_and_clear(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        self.assertEqual(self.harness.safety.current["A"], 20)
        teardown_started, release = (
            await self._install_blocked_runner_watcher_teardown()
        )

        stopping = asyncio.create_task(self.harness.loop.set_autopilot(False))
        await asyncio.wait_for(teardown_started.wait(), timeout=0.2)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.wait({stopping}, timeout=0.05)
        completed_before_release = stopping.done()
        release.set()
        result = await asyncio.gather(stopping, return_exceptions=True)

        self.assertFalse(completed_before_release)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.controller.to_state().status.value, "paused")
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.loop.patterns, {"A": None, "B": None})
        self.assertEqual(self.harness.store.list(), [])

    async def test_cancelled_finish_awaits_controller_teardown_clear_and_archive(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        self.assertEqual(self.harness.safety.current["A"], 20)
        teardown_started, release = (
            await self._install_blocked_runner_watcher_teardown()
        )

        finishing = asyncio.create_task(
            self.harness.loop.finish_timeline_session()
        )
        await asyncio.wait_for(teardown_started.wait(), timeout=0.2)
        finishing.cancel()
        await asyncio.wait({finishing}, timeout=0.05)
        completed_before_release = finishing.done()
        release.set()
        result = await asyncio.gather(finishing, return_exceptions=True)

        self.assertFalse(completed_before_release)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.loop.patterns, {"A": None, "B": None})
        self.assertEqual(len(self.harness.store.list()), 1)

    async def test_resume_transition_serializes_against_finish(self):
        await self.harness.controller.start_live()
        await self.harness.controller.pause()
        resume_timeline_session = self.harness.loop.resume_timeline_session
        original_resume = self.harness.controller.resume
        resume_applied = asyncio.Event()
        allow_resume_return = asyncio.Event()

        async def blocking_resume(cursor=None):
            result = await original_resume(cursor)
            resume_applied.set()
            await allow_resume_return.wait()
            return result

        with patch.object(
            self.harness.controller, "resume", side_effect=blocking_resume
        ):
            resume_task = asyncio.create_task(resume_timeline_session(None))
            await asyncio.wait_for(resume_applied.wait(), timeout=0.2)
            finish_task = asyncio.create_task(
                self.harness.loop.finish_timeline_session()
            )
            await asyncio.sleep(0)

            self.assertFalse(finish_task.done())
            allow_resume_return.set()
            await resume_task
            await finish_task

        self.assertFalse(self.harness.loop.autopilot)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")
        self.assertEqual(len(self.harness.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
