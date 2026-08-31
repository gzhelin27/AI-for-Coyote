import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from backend.config import DEFAULTS
from backend.game_loop import GameLoop
from backend.safety import SafetyManager


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

    def first_client_id(self):
        return "client-1"

    def get_slot_id(self):
        return "slot-1"

    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        return True


class YieldingConnectedRelay(ConnectedRelay):
    async def send_frame(self, frame):
        await asyncio.sleep(0)
        return await super().send_frame(frame)


def make_game_loop_for_test(*, pattern="呼吸", frames=None):
    cfg = deepcopy(DEFAULTS)
    cfg["app"]["dry_run"] = False
    cfg["presets"][pattern] = {
        "waveform": "wave_test",
        "frames": list(frames or ["f"]),
        "default_duration_s": 5,
        "max_duration_s": 10,
    }
    safety = SafetyManager(cfg)
    loop = GameLoop(cfg, None, safety, ConnectedRelay())
    loop.ops = RecordingOps()
    return loop


class GameLoopCycleTests(unittest.IsolatedAsyncioTestCase):
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
