import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend.video.csv_timeline import parse_video_csv
from backend.video.waveforms import resolve_video_plan
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from tests.test_game_loop_timeline import make_game_loop_for_test


class VideoSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.h = make_game_loop_for_test(Path(root))
        self.h.safety.max_step = 10
        self.h.safety.user_caps.update(A=40, B=40)
        for name in ('wave-a', 'wave-b'):
            self.h.safety.presets[name] = {**deepcopy(self.h.safety.presets['呼吸']), 'frames': ['01', '02', '03']}
        self.timeline = parse_video_csv(
            b'start_time,end_time,A_target,B_target\n0:00:00,0:00:30,20,0\n0:00:30,0:01:10,30,15\n',
            duration_ms=80000)
        self.plan = resolve_video_plan(self.timeline, allowed=('wave-a', 'wave-b'), library_sha256='a' * 64, seed=42)
        self.now = 0.
        self.output = GameLoopVideoOutput(self.h.loop)
        self.session = VideoSession(self.plan, self.output, source_id='test', duration_ms=80000,
                                    timeline=self.timeline, clock=lambda: self.now, watch=False)
        await self.session.start()
        self.seq = 0
        self.addAsyncCleanup(self.session.close, 'test_cleanup')

    async def observe(self, position, state='playing', epoch=None):
        self.seq += 1
        await self.session.observe({'session_id': self.session.session_id, 'epoch': epoch or self.session.epoch,
                                    'sequence': self.seq, 'position_ms': position, 'state': state, 'rate': 1})
        await self.session.flush()

    async def advance(self, seconds):
        for _ in range(round(seconds * 10)):
            self.now = round(self.now + .1, 3)
            await self.observe(round(self.now * 1000))
            await self.session.tick()
            await self.session.flush()

    async def test_video_targets_apply_immediately_and_independently(self):
        await self.observe(0)
        self.assertEqual(self.output.snapshot('A').strength, 20)
        await self.advance(2)
        self.assertEqual(self.output.snapshot('A').strength, 20)
        await self.advance(28)
        self.assertEqual(self.output.snapshot('A').strength, 30)
        self.assertEqual(self.output.snapshot('B').strength, 15)
        self.assertFalse(self.h.relay.sent_frames)  # Entire run is simulated.

    async def test_seek_selects_current_block_and_paused_seek_stays_zero(self):
        await self.observe(0)
        await self.observe(65000, 'seeking', epoch=2)
        await self.observe(65000, 'paused', epoch=2)
        self.assertEqual(self.output.snapshot('A').strength, 0)
        await self.observe(65000, epoch=2)
        self.assertEqual(self.session.state()['block']['index'], 2)
        self.assertEqual(self.output.snapshot('A').strength, 30)
        await self.observe(75000, 'seeking', epoch=3)
        await self.observe(75000, epoch=3)
        self.assertEqual(self.output.snapshot('A').strength, 0)
        self.assertIsNone(self.session.state()['block'])

    async def test_lease_loss_clears_and_stale_heartbeat_cannot_resume(self):
        await self.observe(0)
        old_epoch = self.session.epoch
        self.now = 10
        await self.session.tick()
        await self.session.flush()
        self.assertEqual(self.output.snapshot('A').strength, 0)
        await self.observe(10000, epoch=old_epoch)
        self.assertNotEqual(self.session.state()['status'], 'playing')
        self.assertEqual(self.output.snapshot('A').strength, 0)

    async def test_repeated_clock_does_not_reset_wave_or_resend_strength(self):
        await self.observe(0)
        for _ in range(10):
            await self.observe(0)
        self.assertEqual(self.output.snapshot('A').strength, 20)
        self.assertEqual(self.output.offsets['A'], 5)
        self.assertEqual([(item['channel'], item['strength']) for item in self.session.audit], [('A', 20)])

    async def test_state_reports_processed_sequence_and_safety_epoch(self):
        await self.observe(0)
        self.assertEqual(self.session.state()['sequence'], self.seq)
        previous = self.session.epoch
        self.now = 2
        await self.session.tick()
        await self.session.flush()
        self.assertGreater(self.session.state()['epoch'], previous)
        self.assertEqual(self.session.state()['sequence'], self.seq)

    async def test_failed_close_can_retry_but_successful_close_never_clears_again(self):
        await self.observe(0)
        original = self.output.clear
        with patch.object(self.output, 'clear', AsyncMock(side_effect=RuntimeError('offline'))):
            with self.assertRaises(RuntimeError):
                await self.session.close()
        with patch.object(self.output, 'clear', AsyncMock(wraps=original)) as clearing:
            await self.session.close()
            self.assertEqual(self.output.snapshot('A').strength, 0)
            await self.session.close()
            self.assertEqual(clearing.await_count, 1)

    async def test_revisited_row_end_clears_without_waiting_for_next_clock_sample(self):
        for epoch in (2, 3):
            await self.observe(69000, 'seeking', epoch=epoch)
            await self.observe(69000, epoch=epoch)
            self.assertGreater(self.output.snapshot('A').strength, 0)
            self.now += .9
            await self.observe(69900, epoch=epoch)
            self.now += .11
            await self.session.tick()
            await self.session.flush()
            self.assertEqual(self.output.snapshot('A').strength, 0)

    async def test_strength_waiting_for_action_lock_cannot_outlive_lease(self):
        await self.h.loop._action_lock.acquire()
        self.seq += 1
        await self.session.observe(dict(session_id=self.session.session_id, epoch=self.session.epoch,
                                       sequence=self.seq, position_ms=0, state='playing', rate=1))
        for _ in range(20):
            await asyncio.sleep(0)
            if self.output.inflight_strength:
                break
        self.now = 2
        self.h.loop._action_lock.release()
        await self.session.flush()
        self.assertEqual(self.output.snapshot('A').strength, 0)
        self.assertFalse(self.session.audit, 'expired strength must never be confirmed')

    async def test_pause_preempts_ack_wait_without_waiting_for_operation_lock(self):
        from backend.relay_client import RelayClient
        from tests.test_relay_acknowledgement import TickPhone
        self.h.safety.dry_run = False
        relay = RelayClient('ws://offline.invalid')
        relay.clients['phone'] = {'devices': [{'slotId': 'slot'}]}
        phone = TickPhone(relay)
        relay.ws = phone
        self.h.loop.relay = relay
        self.seq += 1
        await self.session.observe({'session_id': self.session.session_id, 'epoch': 1, 'sequence': self.seq,
                                    'position_ms': 0, 'state': 'playing', 'rate': 1})
        for _ in range(100):
            await asyncio.sleep(0)
            if any(f['data']['m'] == 'device.op' and f['data']['data']['t'] == 3 for f in phone.sent):
                break
        self.now = 2
        await self.session.tick()  # Must invalidate blocked ACK without acquiring the work lock.
        for _ in range(150):
            await asyncio.sleep(0)
        resets = [f['data']['data']['c'] for f in phone.sent
                  if f['data']['m'] == 'device.op' and f['data']['data']['t'] == 7]
        self.assertEqual(set(resets), {0, 1})
        for _ in range(100):
            phone.tick()
            await asyncio.sleep(0)
        await self.session.flush()
        self.assertEqual(self.output.snapshot('A').strength, 0)
        self.assertFalse(phone.waveform_strengths)
        self.h.safety.dry_run = True  # Cleanup has no phone tick pump.
