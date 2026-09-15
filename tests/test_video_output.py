import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.test_game_loop_timeline import make_game_loop_for_test
from backend.video.output import GameLoopVideoOutput


class VideoOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.h = make_game_loop_for_test(Path(root))
        self.h.safety.max_step = 10
        self.h.safety.user_caps.update(A=40, B=40)
        self.h.safety.presets['video-wave'] = {
            **deepcopy(self.h.safety.presets['呼吸']), 'frames': ['FRAME_0', 'FRAME_1', 'FRAME_2']}
        self.output = GameLoopVideoOutput(self.h.loop, clock=lambda: 0.)
        self.output.claim()
        self.addAsyncCleanup(self.output.clear)

    async def test_strength_has_no_default_wave_helper_and_preserves_limits(self):
        receipt = await self.output.set_strength('A', 30)
        self.assertTrue(receipt.success)
        self.assertEqual(receipt.strength, 10)
        self.assertFalse(self.h.loop.loop_tasks)
        self.assertIsNone(self.h.loop.output_coordinator.confirmed('A').waveform)

    async def test_replace_waveform_keeps_strength_and_bounds_frames(self):
        self.h.safety.dry_run = False  # Fake relay only.
        await self.output.set_strength('A', 10)
        receipt = await self.output.replace_block('A', 'video-wave', 550)
        self.assertTrue(receipt.success)
        self.assertEqual(receipt.duration_ms, 500)
        self.assertEqual(self.output.snapshot('A').strength, 10)
        pulses = [f['data']['data'] for f in self.h.relay.sent_frames
                  if f['data']['m'] == 'device.op' and f['data']['data']['t'] == 0]
        self.assertEqual(pulses[-1]['v'], ['FRAME_0', 'FRAME_1', 'FRAME_2', 'FRAME_0', 'FRAME_1'])
        self.assertEqual(pulses[-1]['d'], 500)
        next_receipt = await self.output.continue_block('A', 'video-wave', 200)
        self.assertTrue(next_receipt.success)
        pulse = self.h.relay.sent_frames[-1]['data']['data']
        self.assertEqual(pulse['v'], ['FRAME_2', 'FRAME_0'])

    async def test_less_than_one_frame_sends_none(self):
        self.h.safety.dry_run = False
        await self.output.set_strength('A', 10)
        self.h.relay.sent_frames.clear()
        receipt = await self.output.continue_block('A', 'video-wave', 99)
        self.assertEqual(receipt.duration_ms, 0)
        self.assertEqual(self.h.relay.sent_frames, [])

    async def test_preempt_invalidates_strength_and_clear_zeroes(self):
        await self.output.set_strength('A', 10)
        self.output.preempt()
        receipt = await self.output.set_strength('A', 20)
        self.assertFalse(receipt.success)
        await self.output.clear()
        self.assertEqual(self.output.snapshot('A').strength, 0)

    async def test_clear_failure_is_not_success(self):
        self.h.safety.dry_run = False
        await self.output.set_strength('A', 10)
        self.h.relay.fail_next_clear('A')
        with self.assertRaises(RuntimeError):
            await self.output.clear(('A',))
