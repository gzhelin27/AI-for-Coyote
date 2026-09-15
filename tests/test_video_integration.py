"""Synthetic end-to-end video dry-runs; no service, private source or device."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from backend.video.csv_timeline import parse_video_csv
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from backend.video.waveforms import resolve_video_plan
from tests.test_game_loop_timeline import make_game_loop_for_test


class VideoDryRunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.h = make_game_loop_for_test(Path(root))
        self.h.safety.dry_run = True
        self.h.safety.max_step = 10
        self.h.safety.user_caps.update(A=40, B=40)
        for name in ('synthetic-a', 'synthetic-b'):
            self.h.safety.presets[name] = {
                **deepcopy(self.h.safety.presets['呼吸']),
                'frames': ['fixture-frame-0', 'fixture-frame-1', 'fixture-frame-2']}
        self.now = 0.
        self.position_ms = 0
        self.sequence = 0
        self.session = None
        self.output = GameLoopVideoOutput(self.h.loop)

    async def asyncTearDown(self):
        if self.session is not None:
            await self.session.close('integration_cleanup')
        self.assertEqual(self.h.relay.sent_frames, [], 'dry-run must never submit relay frames')
        self.assertFalse(self.h.loop.loop_tasks, 'video strength must not start a default helper')

    async def load(self, rows):
        timeline = parse_video_csv(
            ('start_time,end_time,A_target,B_target\n' + rows).encode('utf-8'),
            duration_ms=80000)
        plan = resolve_video_plan(timeline, allowed=('synthetic-a', 'synthetic-b'),
                                  library_sha256='a' * 64, seed=47)
        self.session = VideoSession(plan, self.output, timeline=timeline, source_id='synthetic',
                                    duration_ms=80000, clock=lambda: self.now,
                                    dry_run=True, watch=False)
        await self.session.start()
        return plan

    async def observe(self, position_ms=None, state='playing', epoch=None):
        if position_ms is not None:
            self.position_ms = position_ms
        self.sequence += 1
        await self.session.observe({
            'session_id': self.session.session_id,
            'epoch': self.session.epoch if epoch is None else epoch,
            'sequence': self.sequence, 'position_ms': self.position_ms,
            'state': state, 'rate': 1})
        await self.session.flush()

    async def advance(self, milliseconds):
        # Fresh 10Hz observations keep the lease valid while real and media
        # clocks advance together. Final partial ticks test exact boundaries.
        left = milliseconds
        while left:
            step = min(100, left)
            self.now = round(self.now + step / 1000, 6)
            self.position_ms += step
            await self.observe()
            await self.session.tick()
            await self.session.flush()
            left -= step

    def strength(self, channel='A'):
        return self.output.snapshot(channel).strength

    async def test_immediate_targets_survive_blocks_without_repeated_strength_commands(self):
        plan = await self.load('0:00:00,0:01:10,30,15\n')
        self.assertEqual([(b.start_ms, b.end_ms) for b in plan.blocks],
                         [(0, 30000), (30000, 60000), (60000, 70000)])
        await self.observe(0)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        await self.advance(29900)
        old_pattern = self.session.state()['channels']['A']['pattern']
        await self.advance(100)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        self.assertNotEqual(self.session.state()['channels']['A']['pattern'], old_pattern)
        await self.advance(29900)
        old_pattern = self.session.state()['channels']['A']['pattern']
        await self.advance(100)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        self.assertNotEqual(self.session.state()['channels']['A']['pattern'], old_pattern)
        await self.advance(9999)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        self.assertEqual([(event['channel'], event['confirmed_at'], event['strength'])
                          for event in self.session.audit], [('A', 0., 30), ('B', 0., 15)])
        await self.advance(1)
        self.assertEqual((self.strength(), self.strength('B')), (0, 0))
        self.assertIsNone(self.session.state()['block'])

    async def test_changed_targets_are_immediate_and_large_targets_remain_capped(self):
        await self.load('0:00:00,0:00:01,20,0\n'
                        '0:00:01,0:00:02,30,0\n'
                        '0:00:02,0:00:03,20,0\n'
                        '0:00:03,0:00:04,31,0\n'
                        '0:00:04,0:00:10,60,15\n')
        await self.observe(0)
        self.assertEqual(self.strength(), 20)
        for expected in (30, 20, 31, 40):
            await self.advance(1000)
            self.assertEqual(self.strength(), expected)
        state = self.session.state()['channels']
        self.assertEqual((state['A']['target'], state['A']['capped_target'], state['A']['strength']),
                         (60, 40, 40))
        self.assertEqual(self.strength('B'), 15)
        await self.advance(2000)
        self.assertEqual([(event['confirmed_at'], event['strength']) for event in self.session.audit
                          if event['channel'] == 'A'],
                         [(0., 20), (1., 30), (2., 20), (3., 31), (4., 40)])

    async def test_seek_pause_clears_and_resume_applies_current_targets(self):
        await self.load('0:00:00,0:01:10,30,15\n')
        await self.observe(0)
        await self.advance(4000)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        await self.observe(65000, 'seeking', epoch=self.session.epoch + 1)
        self.assertEqual((self.strength(), self.strength('B')), (0, 0))
        await self.observe(65000, 'paused')
        self.now += 10
        await self.session.tick()
        await self.session.flush()
        self.assertEqual((self.strength(), self.strength('B')), (0, 0))
        await self.observe(65000)
        self.assertEqual(self.session.state()['block']['index'], 2)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        await self.advance(2000)
        self.assertEqual((self.strength(), self.strength('B')), (30, 15))
        await self.observe(state='paused')
        self.assertEqual((self.strength(), self.strength('B')), (0, 0))

    async def test_sub_frame_tail_never_queues_a_waveform(self):
        await self.load('0:00:00,0:01:10,30,0\n')
        await self.observe(69950)
        self.assertEqual(self.output.offsets['A'], 0)
        self.assertIsNone(self.h.loop.output_coordinator.confirmed('A').waveform)
        await self.advance(50)
        self.assertEqual((self.strength(), self.strength('B')), (0, 0))
        self.assertIsNone(self.session.state()['block'])


if __name__ == '__main__':
    unittest.main()
