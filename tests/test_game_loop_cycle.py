import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock

from backend.config import DEFAULTS
from backend.game_loop import GameLoop
from backend.output_coordinator import OutputIntentKind, TransportOutcome
from backend.safety import DeviceOutputError, SafetyManager


class RecordingOps:
    def __init__(self) -> None:
        self.last_pulse_frames = None
        self.last_pulse_duration_ms = None
        self.pulse_calls = []
        self.clear_calls = []
        self.reset_channels = []
        self.strength_deltas = []

    def pulse(self, client_id, slot_id, channel, frames, duration_ms, immediate=True):
        self.last_pulse_frames = list(frames)
        self.last_pulse_duration_ms = duration_ms
        self.pulse_calls.append((channel, list(frames), duration_ms))
        return {"pulse": True}

    def clear(self, client_id, slot_id=None, channel=None):
        self.clear_calls.append((slot_id, channel))
        return {"clear": channel}

    def reset_intensity(self, client_id, slot_id, channel):
        self.reset_channels.append(channel)
        return {"reset": channel}

    def add_strength(self, client_id, slot_id, channel, delta):
        self.strength_deltas.append((channel, delta))
        return {"strength": [channel, delta]}


class ConnectedRelay:
    def __init__(self) -> None:
        self.sent_frames = []
        self._strength_failures = []
        self._clear_failures = []

    def first_client_id(self):
        return "client-1"

    def get_slot_id(self):
        return "slot-1"

    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        if isinstance(frame, dict) and "strength" in frame:
            channel = frame["strength"][0]
            for index, (failed_channel, failure) in enumerate(
                self._strength_failures
            ):
                if failed_channel is None or failed_channel == channel:
                    self._strength_failures.pop(index)
                    if isinstance(failure, BaseException):
                        raise failure
                    return False
        if isinstance(frame, dict) and "clear" in frame:
            channel = frame["clear"]
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


class GatedPhysicalRelay(ConnectedRelay):
    """Relay fake that records physical strength and gates one successful send."""

    def __init__(self) -> None:
        super().__init__()
        self.physical_strength = {0: 0, 1: 0}
        self.strength_sent = {0: asyncio.Event(), 1: asyncio.Event()}
        self.pulse_send_count = 0
        self.second_pulse_sent = asyncio.Event()
        self.successful_send = asyncio.Event()
        self.release_send = asyncio.Event()
        self._blocked_key = None

    def arm(self, frame_key):
        self._blocked_key = frame_key
        self.successful_send.clear()
        self.release_send.clear()

    def report_strength(self, channel, value):
        numeric = {"A": 0, "B": 1}[channel]
        self.physical_strength[numeric] = value
        return {"intensityA" if channel == "A" else "intensityB": value}

    async def send_frame(self, frame):
        sent = await super().send_frame(frame)
        if sent and isinstance(frame, dict):
            if "strength" in frame:
                channel, delta = frame["strength"]
                self.physical_strength[channel] = max(
                    0, self.physical_strength[channel] + delta
                )
                self.strength_sent[channel].set()
            elif "clear" in frame and frame["clear"] in (0, 1):
                self.physical_strength[frame["clear"]] = 0
            elif "reset" in frame and frame["reset"] in (0, 1):
                self.physical_strength[frame["reset"]] = 0
            elif "pulse" in frame:
                self.pulse_send_count += 1
                if self.pulse_send_count >= 2:
                    self.second_pulse_sent.set()
        if (
            sent
            and self._blocked_key is not None
            and isinstance(frame, dict)
            and self._blocked_key in frame
        ):
            self._blocked_key = None
            self.successful_send.set()
            await self.release_send.wait()
        return sent


class YieldingConnectedRelay(ConnectedRelay):
    async def send_frame(self, frame):
        await asyncio.sleep(0)
        return await super().send_frame(frame)


class RejectingConnectedRelay(ConnectedRelay):
    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        return False


class RaisingConnectedRelay(ConnectedRelay):
    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        raise OSError("relay write failed")


class StalledSafetyController:
    def __init__(self) -> None:
        self.suspend_started = asyncio.Event()
        self.release_suspend = asyncio.Event()

    async def suspend_channel_for_safety(self, channel, *, reason):
        self.suspend_started.set()
        await self.release_suspend.wait()
        return True

    async def resume_channel_after_safety(self, channel):
        return None


def make_game_loop_for_test(*, pattern="呼吸", frames=None, relay=None):
    cfg = deepcopy(DEFAULTS)
    cfg["app"]["dry_run"] = False
    cfg["presets"][pattern] = {
        "waveform": "wave_test",
        "frames": list(frames or ["f"]),
        "default_duration_s": 5,
        "max_duration_s": 10,
    }
    safety = SafetyManager(cfg)
    loop = GameLoop(cfg, None, safety, relay or ConnectedRelay())
    loop.ops = RecordingOps()
    return loop


async def activate_strength(loop, channel, value):
    loop.safety.pulse_until[channel] = float("inf")
    executed, dropped = await loop.execute_actions(
        [{"op": "hold_strength", "channel": channel, "value": value}]
    )
    if dropped or len(executed) != 1:
        raise AssertionError("test setup could not activate strength")
    loop.safety.pulse_until[channel] = 0
    loop.ops.strength_deltas.clear()
    loop.relay.sent_frames.clear()


async def wait_for_condition(predicate, *, timeout=1):
    async def observe():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(observe(), timeout=timeout)


class GameLoopCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_hold_strength_send_false_is_dropped_without_state_mutation(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a"])
        loop.relay = RejectingConnectedRelay()
        # Keep the test focused on the strength transport rather than the legacy
        # automatic-wave fallback.
        loop.safety.pulse_until["A"] = float("inf")

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertFalse(dropped[0]["sent"])
        self.assertEqual(dropped[0]["effective"]["effective_strength"], 0)
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertEqual(loop.last_strength["A"], 0)

    async def test_hold_strength_transport_exception_is_a_safe_drop(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a"])
        loop.relay = RaisingConnectedRelay()

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertFalse(dropped[0]["sent"])
        self.assertIn("relay write failed", dropped[0]["reason"])
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertIsNone(loop.patterns["A"])

    async def test_pulse_cycle_send_false_is_dropped_without_playback_state(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b"])
        loop.relay = RejectingConnectedRelay()
        loop.safety.current["A"] = 12

        executed, dropped = await loop.execute_actions(
            [{"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertFalse(dropped[0]["sent"])
        self.assertEqual(dropped[0]["effective"]["effective_strength"], 12)
        self.assertEqual(loop.patterns["A"], None)
        self.assertFalse(loop.safety.pulse_active()["A"])

    async def test_failed_temp_strength_does_not_schedule_a_later_revert(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a"])
        loop.relay = RejectingConnectedRelay()
        loop.safety.pulse_until["A"] = float("inf")
        loop._schedule_temp_revert = Mock()

        executed, dropped = await loop.execute_actions(
            [
                {
                    "op": "temp_strength",
                    "channel": "A",
                    "value": 20,
                    "duration_s": 1,
                }
            ]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        loop._schedule_temp_revert.assert_not_called()

    async def test_pulse_cycle_sends_one_unrepeated_frame_sequence(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])
        executed, dropped = await loop.execute_actions([
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        ])
        self.assertEqual(dropped, [])
        self.assertEqual(loop.ops.last_pulse_frames, ["a", "b", "c"])
        self.assertEqual(loop.ops.last_pulse_duration_ms, 300)
        self.assertEqual(
            executed[0]["effective"],
            {
                "op": "pulse_cycle",
                "channel": "A",
                "pattern": "呼吸",
                "effective_strength": 0,
                "duration_ms": 300,
            },
        )

    async def test_activation_and_cycle_batch_sends_only_the_requested_raw_cycle(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])

        executed, dropped = await loop.execute_actions([
            {"op": "hold_strength", "channel": "A", "value": 20},
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"},
        ])
        await asyncio.sleep(0)

        self.assertEqual(dropped, [])
        self.assertEqual(len(executed), 2)
        self.assertEqual(loop.ops.pulse_calls, [(0, ["a", "b", "c"], 300)])

    async def test_pulse_hold_keeps_frame_free_effective_result(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b"])
        loop.safety.dry_run = True

        executed, dropped = await loop.execute_actions([
            {"op": "pulse_hold", "channel": "A", "pattern": "呼吸"}
        ])

        self.assertEqual(dropped, [])
        self.assertEqual(
            executed[0]["effective"],
            {
                "op": "pulse_hold",
                "channel": "A",
                "pattern": "呼吸",
                "effective_strength": 0,
            },
        )

    async def test_manual_pulse_still_tiles_frames_to_requested_duration(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])

        executed, dropped = await loop.execute_actions([
            {
                "op": "pulse",
                "channel": "A",
                "pattern": "呼吸",
                "duration_s": 3,
            }
        ])

        self.assertEqual(dropped, [])
        self.assertEqual(loop.ops.last_pulse_frames, ["a", "b", "c"] * 10)
        self.assertEqual(loop.ops.last_pulse_duration_ms, 3000)
        self.assertEqual(executed[0]["effective"]["duration_ms"], 3000)

    async def test_strength_then_manual_pulse_keeps_legacy_default_wave_fallback(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])
        loop.relay = YieldingConnectedRelay()
        loop.safety.presets["潮汐"] = {
            "waveform": "wave_tide",
            "frames": ["x", "y"],
            "default_duration_s": 5,
            "max_duration_s": 10,
        }

        executed, dropped = await loop.execute_actions([
            {"op": "hold_strength", "channel": "A", "value": 20},
            {"op": "pulse", "channel": "A", "pattern": "潮汐", "duration_s": 3},
        ])

        self.assertEqual(dropped, [])
        self.assertEqual(len(executed), 2)
        self.assertEqual(
            [(frames[:3], len(frames), duration) for _, frames, duration in loop.ops.pulse_calls],
            [(["a", "b", "c"], 300, 30000), (["x", "y", "x"], 30, 3000)],
        )
        loop._cancel_loops(None)

    async def test_strength_then_pulse_hold_keeps_legacy_default_wave_fallback(self):
        loop = make_game_loop_for_test(pattern="呼吸", frames=["a", "b", "c"])
        loop.relay = YieldingConnectedRelay()
        loop.safety.presets["潮汐"] = {
            "waveform": "wave_tide",
            "frames": ["x", "y"],
            "default_duration_s": 5,
            "max_duration_s": 10,
        }

        executed, dropped = await loop.execute_actions([
            {"op": "hold_strength", "channel": "A", "value": 20},
            {"op": "pulse_hold", "channel": "A", "pattern": "潮汐"},
        ])
        await asyncio.sleep(0)

        self.assertEqual(dropped, [])
        self.assertEqual(len(executed), 2)
        self.assertEqual(
            [(frames[:3], len(frames), duration) for _, frames, duration in loop.ops.pulse_calls],
            [(["a", "b", "c"], 300, 30000), (["x", "y", "x"], 300, 30000)],
        )
        loop._cancel_loops(None)

    async def test_failed_safety_delta_retains_confirmed_strength_and_pending_target(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        loop.relay.fail_next_strength_delta("A")

        with self.assertRaises(DeviceOutputError):
            await loop.set_runtime_cap("A", 10)

        self.assertEqual(loop.ops.strength_deltas, [(0, -20)])
        self.assertEqual(loop.safety.current["A"], 30)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 30)
        self.assertEqual(
            loop.output_coordinator.pending("A").target_strength,
            10,
        )
        self.assertIsNone(loop.patterns["A"])
        self.assertNotIn("A", loop.loop_tasks)

    async def test_safety_delta_transport_exception_is_retryable_without_state_commit(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        loop.relay.fail_next_strength_delta(
            "A", OSError("private relay failure detail")
        )

        with self.assertRaises(DeviceOutputError):
            await loop.set_runtime_cap("A", 10)

        self.assertEqual(loop.safety.current["A"], 30)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 30)
        self.assertEqual(
            loop.output_coordinator.pending("A").target_strength,
            10,
        )

    async def test_explicit_runtime_safety_reconciliation_retries_pending_delta(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        loop.relay.fail_next_strength_delta("A")
        with self.assertRaises(DeviceOutputError):
            await loop.set_runtime_cap("A", 10)

        reconcile = getattr(loop, "reconcile_runtime_safety", None)
        self.assertIsNotNone(reconcile)
        result = await reconcile("A")

        self.assertEqual(result["A"]["dropped"], [])
        self.assertEqual(loop.ops.strength_deltas, [(0, -20), (0, -20)])
        self.assertEqual(loop.safety.current["A"], 10)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 10)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )

    async def test_identical_overheat_report_retries_failed_pending_reduction(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        loop.relay.fail_next_strength_delta("A")
        overheat = {
            "channelA": {"comfortLimit": {"overheat": True}},
        }

        first = await loop.update_device_state(None, overheat)
        second = await loop.update_device_state(None, overheat)

        self.assertEqual(loop.ops.strength_deltas, [(0, -10), (0, -10)])
        self.assertTrue(first["A"]["dropped"])
        self.assertEqual(second["A"]["dropped"], [])
        self.assertEqual(loop.safety.current["A"], 20)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )
        self.assertIsNone(loop.patterns["A"])

    async def test_overheat_recovery_does_not_reescalate_confirmed_strength(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)

        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"overheat": True}}},
        )
        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"overheat": False}}},
        )

        self.assertEqual(loop.ops.strength_deltas, [(0, -10)])
        self.assertFalse(loop.safety.overheat["A"])
        self.assertEqual(loop.safety.current["A"], 20)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)
        self.assertIsNone(loop.patterns["A"])

    async def test_disable_failure_retries_clear_before_committing_enabled_state(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 25)
        loop.relay.fail_next_clear("A")

        with self.assertRaises(DeviceOutputError):
            await loop.set_channel_enabled("A", False)

        self.assertTrue(loop.safety.enabled["A"])
        self.assertEqual(loop.safety.current["A"], 25)
        self.assertTrue(loop.output_coordinator.pending("A").clear_required)
        frames_after_failure = len(loop.relay.sent_frames)

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 5}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(loop.relay.sent_frames), frames_after_failure)
        self.assertEqual(loop.safety.current["A"], 25)
        loop.turn_count = 2
        loop.last_strength = {"A": 2, "B": 2}
        loop.last_wave = {"A": 2, "B": 2}
        loop.safety.current["B"] = 1
        loop.safety.pulse_until["B"] = float("inf")

        await loop._apply_channel_floor()

        self.assertEqual(len(loop.relay.sent_frames), frames_after_failure)
        self.assertEqual(loop.safety.current["A"], 25)

        result = await loop.set_channel_enabled("A", False)

        self.assertEqual(result["dropped"], [])
        self.assertFalse(loop.safety.enabled["A"])
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertFalse(loop.output_coordinator.pending("A").clear_required)
        self.assertEqual(loop.ops.clear_calls, [("slot-1", 0), ("slot-1", 0)])

    async def test_failed_a_reduction_does_not_block_b_reconciliation(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        await activate_strength(loop, "B", 30)
        loop.relay.fail_next_strength_delta("A")

        with self.assertRaises(DeviceOutputError):
            await loop.set_runtime_cap("A", 10)
        result_b = await loop.set_runtime_cap("B", 10)

        self.assertEqual(result_b["dropped"], [])
        self.assertEqual(loop.safety.current, {"A": 30, "B": 10})
        self.assertEqual(loop.output_coordinator.confirmed("B").strength, 10)
        self.assertEqual(
            loop.output_coordinator.pending("A").target_strength,
            10,
        )

    async def test_dry_run_safety_reduction_has_transport_parity_without_waveform_state(self):
        loop = make_game_loop_for_test()
        self.addCleanup(loop._cancel_loops, None)
        loop.safety.dry_run = True
        await activate_strength(loop, "A", 30)

        result = await loop.set_runtime_cap("A", 10)

        self.assertEqual(result["dropped"], [])
        self.assertEqual(loop.relay.sent_frames, [])
        self.assertEqual(loop.safety.current["A"], 10)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 10)
        self.assertIsNone(loop.patterns["A"])
        self.assertNotIn("A", loop.loop_tasks)

    async def test_cancelled_successful_reduction_publishes_before_reraising(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        relay.arm("strength")

        reduction = asyncio.create_task(loop.set_runtime_cap("A", 10))
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        reduction.cancel()
        relay.release_send.set()

        with self.assertRaises(asyncio.CancelledError):
            await reduction

        self.assertEqual(relay.physical_strength[0], 10)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 10)
        self.assertEqual(loop.safety.current["A"], 10)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )
        attempts = list(loop.ops.strength_deltas)

        await loop.reconcile_runtime_safety("A")

        self.assertEqual(loop.ops.strength_deltas, attempts)
        self.assertEqual(relay.physical_strength[0], 10)

    async def test_cancelled_successful_disable_publishes_before_reraising(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 25)
        relay.arm("reset")

        disabling = asyncio.create_task(loop.set_channel_enabled("A", False))
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        disabling.cancel()
        relay.release_send.set()

        with self.assertRaises(asyncio.CancelledError):
            await disabling

        self.assertEqual(relay.physical_strength[0], 0)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 0)
        self.assertFalse(loop.output_coordinator.confirmed("A").enabled)
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertFalse(loop.safety.enabled["A"])
        self.assertFalse(loop.output_coordinator.pending("A").clear_required)
        frames_after_disable = len(relay.sent_frames)
        loop.safety.pulse_until["A"] = float("inf")

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 5}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(relay.sent_frames), frames_after_disable)

    async def test_report_arriving_during_reduction_reconciles_newer_physical_state(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        relay.arm("strength")

        reduction = asyncio.create_task(loop.set_runtime_cap("A", 20))
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        report = asyncio.create_task(
            loop.update_device_state(relay.report_strength("A", 40), None)
        )
        await asyncio.sleep(0)
        relay.release_send.set()
        await asyncio.gather(reduction, report)

        self.assertEqual(loop.ops.strength_deltas, [(0, -10), (0, -20)])
        self.assertEqual(relay.physical_strength[0], 20)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)
        self.assertEqual(loop.safety.current["A"], 20)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )

    async def test_cancelled_report_retains_pending_reduction_until_identical_retry(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        relay.fail_next_strength_delta("A", OSError("delta exploded"))
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def block_channel(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective={"strength": int(state.strength or 0)},
            )

        initial_revision = loop.output_coordinator.revision("A")
        confirmation_started = asyncio.Event()
        confirm_reported_strength = (
            loop.output_coordinator.confirm_reported_strength
        )

        async def observed_confirmation(*args, **kwargs):
            confirmation_started.set()
            return await confirm_reported_strength(*args, **kwargs)

        loop.output_coordinator.confirm_reported_strength = (
            observed_confirmation
        )
        blocker = asyncio.create_task(
            loop.output_coordinator.run(
                "A", OutputIntentKind.MANUAL, block_channel
            )
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1)
        policy = {
            "channelA": {"comfortLimit": {"overheat": True}}
        }
        report = asyncio.create_task(
            loop.update_device_state(
                relay.report_strength("A", 30), policy
            )
        )
        await wait_for_condition(lambda: loop.safety.overheat["A"])
        await asyncio.wait_for(confirmation_started.wait(), timeout=1)

        report.cancel()
        release_blocker.set()
        await blocker
        with self.assertRaises(asyncio.CancelledError):
            await report
        await wait_for_condition(
            lambda: loop.output_coordinator.revision("A")
            >= initial_revision + 2
        )

        self.assertEqual(relay.physical_strength[0], 30)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 30)
        self.assertEqual(loop.safety.current["A"], 30)
        self.assertEqual(
            loop.output_coordinator.pending("A").target_strength, 20
        )
        self.assertEqual(loop.ops.strength_deltas, [(0, -10)])
        frames_after_cancellation = len(relay.sent_frames)

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 5}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(relay.sent_frames), frames_after_cancellation)

        retried = await loop.update_device_state(
            relay.report_strength("A", 30), policy
        )

        self.assertEqual(retried["A"]["dropped"], [])
        self.assertEqual(relay.physical_strength[0], 20)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)
        self.assertEqual(loop.safety.current["A"], 20)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )

    async def test_b_report_reconciles_while_a_transport_is_stalled(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        await activate_strength(loop, "B", 30)
        relay.arm("strength")

        stalled_a = asyncio.create_task(loop.set_runtime_cap("A", 20))
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        relay.strength_sent[1].clear()
        b_report = asyncio.create_task(
            loop.update_device_state(
                None,
                {"channelB": {"comfortLimit": {"overheat": True}}},
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(relay.strength_sent[1].wait()), timeout=0.2
            )
            b_result = await asyncio.wait_for(
                asyncio.shield(b_report), timeout=0.2
            )
            self.assertEqual(b_result["B"]["dropped"], [])
            self.assertEqual(relay.physical_strength[1], 20)
            self.assertEqual(
                loop.output_coordinator.confirmed("B").strength, 20
            )
            self.assertEqual(loop.safety.current["B"], 20)
        finally:
            relay.release_send.set()
            await asyncio.gather(stalled_a, b_report, return_exceptions=True)

    async def test_concurrent_channel_floors_share_confirmed_strength_snapshot(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        loop.turn_count = 2
        loop.last_strength = {"A": 0, "B": 2}
        loop.last_wave = {"A": 2, "B": 2}
        loop.safety.pulse_until = {"A": float("inf"), "B": float("inf")}
        loop.safety.current["B"] = 5
        relay.arm("strength")
        second_transaction_started = asyncio.Event()
        coordinator_run = loop.output_coordinator.run
        floor_transactions = 0

        async def observed_run(channel, kind, operation):
            nonlocal floor_transactions
            if channel == "A" and kind is OutputIntentKind.MANUAL:
                floor_transactions += 1
                if floor_transactions == 2:
                    second_transaction_started.set()
            return await coordinator_run(channel, kind, operation)

        loop.output_coordinator.run = observed_run

        first = asyncio.create_task(loop._apply_channel_floor())
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        second = asyncio.create_task(loop._apply_channel_floor())
        await asyncio.wait_for(second_transaction_started.wait(), timeout=1)
        relay.release_send.set()
        await asyncio.gather(first, second)

        self.assertEqual(loop.ops.strength_deltas, [(0, 15)])
        self.assertEqual(relay.physical_strength[0], 15)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 15)
        self.assertEqual(loop.safety.current["A"], 15)

    async def test_floor_loop_stops_at_invalidation_while_suspension_is_stalled(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        loop.cfg["playback"]["frame_ms"] = 10
        loop.cfg["playback"]["loop_batch_s"] = 0.1
        loop.cfg["playback"]["loop_overlap_s"] = 0
        loop.turn_count = 2
        loop.last_strength = {"A": 2, "B": 2}
        loop.last_wave = {"A": 0, "B": 2}
        loop.safety.current = {"A": 5, "B": 5}
        loop.safety.pulse_until["B"] = float("inf")
        loop.output_coordinator.seed_confirmed(
            "A", strength=5, enabled=True
        )
        controller = StalledSafetyController()
        loop.timeline_session = controller

        await loop._apply_channel_floor()
        self.assertEqual(relay.pulse_send_count, 1)
        worker = loop.loop_tasks["A"]
        disabling = asyncio.create_task(loop.set_channel_enabled("A", False))
        await asyncio.wait_for(controller.suspend_started.wait(), timeout=1)
        self.assertTrue(loop.output_coordinator.pending("A").clear_required)
        frames_at_invalidation = relay.pulse_send_count
        second_pulse = asyncio.create_task(relay.second_pulse_sent.wait())
        try:
            completed, _ = await asyncio.wait(
                {worker, second_pulse},
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            self.assertIn(worker, completed)
            self.assertNotIn(second_pulse, completed)
            self.assertEqual(relay.pulse_send_count, frames_at_invalidation)
        finally:
            second_pulse.cancel()
            controller.release_suspend.set()
            await asyncio.gather(
                disabling, second_pulse, return_exceptions=True
            )

    async def test_disable_serializes_against_inflight_channel_floor_start(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        loop.turn_count = 2
        loop.last_strength = {"A": 0, "B": 2}
        loop.last_wave = {"A": 0, "B": 2}
        loop.safety.current["B"] = 5
        loop.safety.pulse_until["B"] = float("inf")
        relay.arm("pulse")
        relay.fail_next_clear("A")

        floor = asyncio.create_task(loop._apply_channel_floor())
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        disabling = asyncio.create_task(loop.set_channel_enabled("A", False))
        for _ in range(10):
            await asyncio.sleep(0)
        disable_bypassed_floor = disabling.done()
        relay.release_send.set()
        floor_result, disable_result = await asyncio.gather(
            floor, disabling, return_exceptions=True
        )

        self.assertFalse(disable_bypassed_floor)
        self.assertIsNone(floor_result)
        self.assertIsInstance(disable_result, DeviceOutputError)
        self.assertEqual(loop.ops.strength_deltas, [])
        self.assertNotIn("A", loop.loop_tasks)
        self.assertEqual(
            relay.sent_frames[-2:], [{"clear": 0}, {"reset": 0}]
        )
        self.assertTrue(loop.output_coordinator.pending("A").clear_required)
        self.assertEqual(loop.patterns["A"], "呼吸")
        self.assertEqual(
            loop.output_coordinator.confirmed("A").waveform, "呼吸"
        )
        self.assertEqual(
            loop.output_coordinator.confirmed("A").waveform_mode, "finite"
        )

    async def test_disable_invalidates_queued_floor_strength_before_exceptional_clear(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        loop.turn_count = 2
        loop.last_strength = {"A": 0, "B": 2}
        loop.last_wave = {"A": 2, "B": 2}
        loop.safety.pulse_until = {"A": float("inf"), "B": float("inf")}
        loop.safety.current["B"] = 5
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def block_channel(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective={"strength": int(state.strength or 0)},
            )

        blocker = asyncio.create_task(
            loop.output_coordinator.run(
                "A", OutputIntentKind.MANUAL, block_channel
            )
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1)
        relay.arm("strength")
        relay.fail_next_clear("A", OSError("clear exploded"))

        floor = asyncio.create_task(loop._apply_channel_floor())
        await asyncio.sleep(0)
        disabling = asyncio.create_task(loop.set_channel_enabled("A", False))
        await wait_for_condition(
            lambda: loop.output_coordinator.pending("A").clear_required
        )
        release_blocker.set()
        relay.release_send.set()
        blocker_result, floor_result, disable_result = await asyncio.gather(
            blocker, floor, disabling, return_exceptions=True
        )

        self.assertTrue(blocker_result.sent)
        self.assertIsNone(floor_result)
        self.assertIsInstance(disable_result, DeviceOutputError)
        self.assertEqual(loop.ops.strength_deltas, [])
        self.assertEqual(relay.physical_strength[0], 0)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 0)
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertTrue(loop.output_coordinator.pending("A").clear_required)
        self.assertNotIn("A", loop.loop_tasks)

        retry = await loop.reconcile_runtime_safety("A")

        self.assertEqual(retry["A"]["dropped"], [])
        self.assertFalse(loop.output_coordinator.pending("A").clear_required)
        self.assertEqual(relay.physical_strength[0], 0)
        self.assertFalse(loop.safety.enabled["A"])

    async def test_stale_successful_floor_strength_is_retained_until_clear_retry(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        loop.turn_count = 2
        loop.last_strength = {"A": 0, "B": 2}
        loop.last_wave = {"A": 2, "B": 2}
        loop.safety.pulse_until = {"A": float("inf"), "B": float("inf")}
        loop.safety.current["B"] = 5
        relay.arm("strength")
        relay.fail_next_clear("A", OSError("clear exploded"))

        floor = asyncio.create_task(loop._apply_channel_floor())
        await asyncio.wait_for(relay.successful_send.wait(), timeout=1)
        disabling = asyncio.create_task(loop.set_channel_enabled("A", False))
        await wait_for_condition(
            lambda: loop.output_coordinator.pending("A").clear_required
        )
        relay.release_send.set()
        floor_result, disable_result = await asyncio.gather(
            floor, disabling, return_exceptions=True
        )

        self.assertIsNone(floor_result)
        self.assertIsInstance(disable_result, DeviceOutputError)
        self.assertEqual(relay.physical_strength[0], 15)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 15)
        self.assertEqual(loop.safety.current["A"], 15)
        self.assertTrue(loop.output_coordinator.pending("A").clear_required)
        self.assertNotIn("A", loop.loop_tasks)
        frames_after_failure = len(relay.sent_frames)

        executed, dropped = await loop.execute_actions(
            [{"op": "hold_strength", "channel": "A", "value": 5}]
        )

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(relay.sent_frames), frames_after_failure)

        retry = await loop.reconcile_runtime_safety("A")

        self.assertEqual(retry["A"]["dropped"], [])
        self.assertEqual(relay.physical_strength[0], 0)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 0)
        self.assertEqual(loop.safety.current["A"], 0)
        self.assertFalse(loop.output_coordinator.pending("A").clear_required)
        self.assertFalse(loop.safety.enabled["A"])

    async def test_comfort_cap_reduces_confirmed_physical_strength(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)

        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"comfortMax": 10}}},
        )

        self.assertEqual(loop.safety.cap_for("A"), 10)
        self.assertEqual(loop.ops.strength_deltas, [(0, -20)])
        self.assertEqual(relay.physical_strength[0], 10)
        self.assertEqual(loop.safety.current["A"], 10)

    async def test_absolute_cap_wins_when_lower_than_comfort_cap(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)

        await loop.update_device_state(
            None,
            {
                "channelA": {
                    "comfortLimit": {"comfortMax": 25, "absoluteMax": 10}
                }
            },
        )

        self.assertEqual(loop.safety.app_caps["A"], 10)
        self.assertEqual(loop.safety.cap_for("A"), 10)
        self.assertEqual(loop.ops.strength_deltas, [(0, -20)])
        self.assertEqual(relay.physical_strength[0], 10)

    async def test_app_cap_recovery_never_sends_positive_reescalation(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)

        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"comfortMax": 10}}},
        )
        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"comfortMax": 50}}},
        )

        self.assertEqual(loop.safety.cap_for("A"), 50)
        self.assertEqual(loop.ops.strength_deltas, [(0, -20)])
        self.assertEqual(relay.physical_strength[0], 10)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 10)
        self.assertEqual(loop.safety.current["A"], 10)

    async def test_malformed_or_missing_app_policy_retains_last_safe_cap(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        await loop.update_device_state(
            None,
            {"channelA": {"comfortLimit": {"comfortMax": 10}}},
        )

        await loop.update_device_state(
            None,
            {
                "channelA": {
                    "comfortLimit": {
                        "comfortMax": "invalid",
                        "absoluteMax": -1,
                    }
                }
            },
        )
        await loop.update_device_state(None, {"channelA": {}})
        await loop.update_device_state(None, None)

        self.assertEqual(loop.safety.app_caps["A"], 10)
        self.assertEqual(loop.safety.cap_for("A"), 10)
        self.assertEqual(loop.ops.strength_deltas, [(0, -20)])
        self.assertEqual(relay.physical_strength[0], 10)

    async def test_infinite_app_cap_does_not_abort_overheat_pending_reconciliation(self):
        relay = GatedPhysicalRelay()
        loop = make_game_loop_for_test(relay=relay)
        self.addCleanup(loop._cancel_loops, None)
        await activate_strength(loop, "A", 30)
        relay.fail_next_strength_delta("A", OSError("delta exploded"))
        report = {
            "channelA": {
                "comfortLimit": {
                    "overheat": True,
                    "comfortMax": float("inf"),
                }
            }
        }

        first = await loop.update_device_state(None, report)

        self.assertTrue(loop.safety.overheat["A"])
        self.assertIsNone(loop.safety.app_caps["A"])
        self.assertTrue(first["A"]["dropped"])
        self.assertEqual(relay.physical_strength[0], 30)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 30)
        self.assertEqual(loop.safety.current["A"], 30)
        self.assertEqual(
            loop.output_coordinator.pending("A").target_strength, 20
        )

        second = await loop.update_device_state(None, report)

        self.assertEqual(second["A"]["dropped"], [])
        self.assertEqual(relay.physical_strength[0], 20)
        self.assertEqual(loop.output_coordinator.confirmed("A").strength, 20)
        self.assertEqual(loop.safety.current["A"], 20)
        self.assertIsNone(
            loop.output_coordinator.pending("A").target_strength
        )

    async def test_clear_output_does_not_enter_estop(self):
        loop = make_game_loop_for_test()
        loop.execute_actions = AsyncMock(return_value=([], []))
        await loop.clear_output()
        loop.execute_actions.assert_awaited_once_with([{"op": "stop"}])
        self.assertFalse(loop.safety.estop_active)

    async def test_clear_output_for_one_channel_uses_physical_clear(self):
        loop = make_game_loop_for_test()
        loop.execute_actions = AsyncMock(return_value=([], []))

        await loop.clear_output("B")

        loop.execute_actions.assert_awaited_once_with([{"op": "clear", "channel": "B"}])
        self.assertFalse(loop.safety.estop_active)

    async def test_clear_output_sends_device_clear_and_resets_without_estop(self):
        loop = make_game_loop_for_test()
        loop.safety.current = {"A": 7, "B": 0}

        executed, dropped = await loop.clear_output()

        self.assertEqual(dropped, [])
        self.assertTrue(executed[0]["sent"])
        self.assertEqual(loop.ops.clear_calls, [("slot-1", None)])
        self.assertEqual(loop.ops.strength_deltas, [(0, -7)])
        self.assertEqual(loop.ops.reset_channels, [0, 1])
        self.assertEqual(loop.safety.current, {"A": 0, "B": 0})
        self.assertFalse(loop.safety.estop_active)

    async def test_hold_strength_reports_requested_and_effective_strength(self):
        loop = make_game_loop_for_test()
        loop.safety.dry_run = True
        loop.relay.first_client_id = lambda: None
        loop.relay.get_slot_id = lambda: None

        executed, dropped = await loop.execute_actions([
            {"op": "hold_strength", "channel": "A", "value": 99}
        ])

        self.assertEqual(dropped, [])
        self.assertEqual(
            executed[0]["effective"],
            {
                "op": "hold_strength",
                "channel": "A",
                "requested_strength": 99,
                "effective_strength": 40,
            },
        )

    async def test_pulse_cycle_requires_connected_device_outside_dry_run(self):
        loop = make_game_loop_for_test()
        loop.relay.first_client_id = lambda: None
        loop.relay.get_slot_id = lambda: None

        executed, dropped = await loop.execute_actions([
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        ])

        self.assertEqual(executed, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("clientId/slotId", dropped[0]["reason"])

    def test_pulse_cycle_rejects_disabled_channel(self):
        loop = make_game_loop_for_test()
        loop.safety.set_channel_enabled("A", False)

        ok, reason, cmd = loop.safety.validate(
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        )

        self.assertFalse(ok)
        self.assertIn("已手动关闭", reason)
        self.assertIsNone(cmd)

    def test_pulse_cycle_rejects_unknown_pattern(self):
        loop = make_game_loop_for_test()

        ok, reason, cmd = loop.safety.validate(
            {"op": "pulse_cycle", "channel": "A", "pattern": "不存在"}
        )

        self.assertFalse(ok)
        self.assertIn("未知波形", reason)
        self.assertIsNone(cmd)

    def test_pulse_cycle_rejects_empty_frame_sequence(self):
        loop = make_game_loop_for_test()
        loop.safety.presets["呼吸"]["frames"] = []

        ok, reason, cmd = loop.safety.validate(
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        )

        self.assertFalse(ok)
        self.assertIn("没有可播放帧", reason)
        self.assertIsNone(cmd)

    def test_pulse_cycle_is_rejected_during_estop(self):
        loop = make_game_loop_for_test()
        loop.safety.estop_active = True

        ok, reason, cmd = loop.safety.validate(
            {"op": "pulse_cycle", "channel": "A", "pattern": "呼吸"}
        )

        self.assertFalse(ok)
        self.assertIn("急停中", reason)
        self.assertIsNone(cmd)


if __name__ == "__main__":
    unittest.main()
