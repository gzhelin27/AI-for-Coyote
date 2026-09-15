"""Independent review reproductions using synthetic inputs and fake transport."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.test_game_loop_timeline import make_game_loop_for_test
from backend.video.csv_timeline import parse_video_csv
from backend.video.waveforms import resolve_video_plan
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession


class VideoReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.h = make_game_loop_for_test(Path(root))
        self.h.safety.max_step = 10
        self.h.safety.user_caps.update(A=40, B=40)
        for pattern in ('a', 'b'):
            self.h.safety.presets[pattern] = {
                **deepcopy(self.h.safety.presets['呼吸']), 'frames': ['01', '02', '03']}
        timeline = parse_video_csv(
            b'start_time,end_time,A_target,B_target\n0:00:00,0:01:00,30,0\n', duration_ms=70000)
        plan = resolve_video_plan(timeline, allowed=('a', 'b'), library_sha256='a'*64, seed=1)
        self.now = 0.
        self.output = GameLoopVideoOutput(self.h.loop)
        self.session = VideoSession(plan, self.output, source_id='review', duration_ms=70000,
                                    timeline=timeline, clock=lambda: self.now, watch=False)
        await self.session.start()
        self.seq = 0
        self.addAsyncCleanup(self.session.close)

    async def observe(self, position, status='playing', flush=True):
        self.seq += 1
        await self.session.observe(dict(session_id=self.session.session_id, epoch=self.session.epoch,
                                       sequence=self.seq, position_ms=position, state=status))
        if flush:
            await self.session.flush()

    async def test_normal_boundary_does_not_reset_confirmed_strength_with_inflight_wave(self):
        await self.observe(29400)
        self.assertEqual(self.output.snapshot('A').strength, 30)
        # A continuation's network write is in flight when the next media block arrives.
        self.h.safety.dry_run = False
        original_send = self.h.relay.send_frame
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(frame):
            if frame['data']['m'] == 'device.op' and frame['data']['data']['t'] == 0:
                entered.set()
                await release.wait()
            return await original_send(frame)

        self.h.relay.send_frame = delayed_send
        self.now = .45
        await self.session.tick()
        await asyncio.wait_for(entered.wait(), 1)
        self.now = .6
        await self.observe(30000, flush=False)
        release.set()
        await self.session.flush()
        resets = [f for f in self.h.relay.sent_frames if f['data']['m'] == 'device.op'
                  and f['data']['data']['t'] == 7]
        self.assertEqual(resets, [], 'normal waveform boundary must preserve confirmed intensity')
        self.assertEqual(self.output.snapshot('A').strength, 30)

    async def test_paused_new_node_and_watchdog_do_not_resume(self):
        await self.observe(0)
        await self.observe(0, 'paused')
        await self.observe(35000, 'paused')
        self.now = 10
        await self.session.tick()
        await self.session.flush()
        self.assertEqual(self.session.status, 'paused')
        self.assertEqual(self.output.snapshot('A').strength, 0)
        self.assertEqual(self.output.snapshot('B').strength, 0)

    async def test_late_wave_fragment_at_normal_boundary_is_not_playback_error(self):
        await self.observe(29400)
        self.assertEqual(self.output.snapshot('A').strength, 30)
        self.now = .45
        await self.h.loop._action_lock.acquire()
        try:
            await self.session.tick()
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(self.session._work.done())
            self.now = .7
        finally:
            self.h.loop._action_lock.release()
        await self.session.flush()
        self.assertEqual(self.session.status, 'playing', self.session.state())
        await self.observe(30000)
        self.assertEqual(self.output.snapshot('A').strength, 30)


    async def test_delayed_queue_clear_cannot_send_wave_past_block_end(self):
        self.h.safety.dry_run = False
        original_send = self.h.relay.send_frame

        async def delayed_queue_clear(frame):
            result = await original_send(frame)
            if frame['data']['m'] == 'device.op.clear':
                self.now = .7  # Queue clear ACK arrives after the 30s boundary.
            return result

        self.h.relay.send_frame = delayed_queue_clear
        await self.observe(29400)
        pulses = [f for f in self.h.relay.sent_frames if f['data']['m'] == 'device.op'
                  and f['data']['data']['t'] == 0]
        self.assertEqual(pulses, [], 'queue-clear ACK cannot authorize expired block frames')

    async def test_cap_reduction_preempts_pending_increase_before_its_ack(self):
        from backend.relay_client import RelayClient
        from tests.test_relay_acknowledgement import TickPhone
        self.h.safety.dry_run = False
        relay = RelayClient('ws://offline.invalid')
        relay.clients['phone'] = {'devices': [{'slotId': 'slot'}]}
        phone = TickPhone(relay)
        relay.ws = phone
        self.h.loop.relay = relay
        increase = asyncio.create_task(self.output.set_strength('A', 10))
        for _ in range(100):
            await asyncio.sleep(0)
            if phone.queued:
                break
        reduction = asyncio.create_task(self.h.loop.set_runtime_cap('A', 5))
        for _ in range(100):
            await asyncio.sleep(0)
        prior_to_ack = list(phone.sent)
        # Settle all synthetic transport before assertion/cleanup.
        for _ in range(100):
            phone.tick()
            await asyncio.sleep(0)
        increase_result = await increase
        reduction_result = await reduction
        self.h.safety.dry_run = True
        self.assertFalse(increase_result.success)
        self.assertEqual(reduction_result['effective_cap'], 5)
        self.assertEqual(phone.strength, 0)
        self.assertEqual(self.output.snapshot('A').strength, 0)
        self.assertFalse(self.h.loop.loop_tasks)
        clear_frames = [f for f in prior_to_ack if f['data']['m'] == 'device.op.clear']
        self.assertTrue(clear_frames, 'lower cap must cancel a queued above-cap increase before its ACK')
