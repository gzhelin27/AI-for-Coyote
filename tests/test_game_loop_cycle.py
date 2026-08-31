import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock

from backend.config import DEFAULTS
from backend.game_loop import GameLoop
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
            elif "clear" in frame and frame["clear"] in (0, 1):
                self.physical_strength[frame["clear"]] = 0
            elif "reset" in frame and frame["reset"] in (0, 1):
                self.physical_strength[frame["reset"]] = 0
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
