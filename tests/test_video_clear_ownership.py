"""A video's own clear may yield, but cannot hide an external owner change."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.output_coordinator import OutputIntentKind
from backend.video.csv_timeline import parse_video_csv
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from backend.video.waveforms import resolve_video_plan
from tests.test_game_loop_timeline import make_game_loop_for_test


class VideoClearOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = make_game_loop_for_test(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.now = 0.
        self.sequence = 0

    async def prepare(self, a=0, b=30, gap=False):
        suffix = '' if gap else f'0:00:01,0:00:10,{a},{b}\n'
        timeline = parse_video_csv(('start_time,end_time,A_target,B_target\n0:00:00,0:00:01,20,20\n'+suffix).encode(), duration_ms=10000)
        plan = resolve_video_plan(timeline, allowed=('呼吸',), library_sha256='a'*64, seed=1)
        self.output = GameLoopVideoOutput(self.h.loop, clock=lambda:self.now)
        self.session = VideoSession(plan,self.output,source_id='synthetic',duration_ms=10000,
            timeline=timeline,clock=lambda:self.now,watch=False)
        self.addAsyncCleanup(self.session.close)
        await self.session.start()
        await self.observe(0)
        await self.session.flush()
        self.now = .9
        await self.observe(900)
        await self.session.flush()

    async def observe(self, position, state='playing'):
        self.sequence += 1
        await self.session.observe(dict(session_id=self.session.session_id,epoch=self.session.epoch,
            sequence=self.sequence,position_ms=position,state=state,rate=1))

    async def transition(self, *, target=(0,30), external=None, after=False, gap=False, global_clear=False):
        await self.prepare(*target, gap=gap)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.h.loop.clear_output
        delayed_once = False
        async def delayed(channel=None, **kwargs):
            nonlocal delayed_once
            if delayed_once:
                return await original(channel, **kwargs)
            delayed_once = True
            if after:
                result = await original(channel, **kwargs)
            entered.set()
            await release.wait()
            return result if after else await original(channel, **kwargs)
        try:
            with patch.object(self.h.loop, 'clear_output', delayed):
                self.now = 1.05
                if global_clear:
                    await self.session.tick()
                else:
                    await self.observe(1050)
                await asyncio.wait_for(entered.wait(),1)
                if global_clear:
                    await self.observe(1050)
                if external == 'clear':
                    self.h.loop.require_output_clear(('A','B'))
                elif external == 'policy':
                    self.h.loop.output_coordinator.invalidate_queued_normal('B')
                elif external == 'takeover':
                    self.h.loop.begin_timeline_output(('A','B'))
                elif external == 'stop':
                    self.session.interrupt('operator_stop')
                elif external == 'lease':
                    self.now += 1.1
                await self.session.tick()
                release.set()
                await asyncio.wait_for(self.session.flush(),2)
                await self.session.tick()
                await asyncio.wait_for(self.session.flush(),2)
                state = self.session.state()
                if external:
                    self.assertNotEqual(state['status'],'playing',state)
                    self.assertEqual(tuple(state['channels'][c]['strength'] for c in ('A','B')),(0,0))
                    self.assertFalse((await self.output.set_strength('B',30,remaining=lambda:500)).success)
                else:
                    self.assertEqual(state['status'],'playing',state)
                    self.assertEqual(state['epoch'],1)
                    self.assertEqual(tuple(state['channels'][c]['strength'] for c in ('A','B')),(0,0) if gap else target)
                self.assertFalse(self.h.relay.sent_frames)
                self.assertFalse(state['clear_pending'], 'confirmed clear must not masquerade as pending ownership')
        finally:
            release.set()

    async def test_a_zero_clear_does_not_pause_b(self):
        await self.transition()

    async def test_b_zero_clear_does_not_pause_a(self):
        await self.transition(target=(30,0))

    async def test_both_zero_clear_does_not_pause_media(self):
        await self.transition(target=(0,0))

    async def test_gap_clear_does_not_pause_media(self):
        await self.transition(gap=True)

    async def test_external_clear_during_owned_clear_wins(self):
        await self.transition(external='clear')

    async def test_policy_change_during_owned_clear_wins(self):
        await self.transition(external='policy')

    async def test_takeover_after_clear_before_claim_wins(self):
        await self.transition(external='takeover',after=True)

    async def test_global_clear_cannot_reclaim_after_external_clear(self):
        await self.transition(external='clear',after=True,gap=True,global_clear=True)

    async def test_projected_gap_global_clear_accepts_new_observation(self):
        await self.transition(gap=True,global_clear=True)

    async def test_retired_owner_can_close_and_new_session_can_start(self):
        await self.transition(external='clear')
        old = self.session
        state = await old.close()
        self.assertFalse(state['clear_pending'])
        await self.prepare()
        self.assertIsNot(self.session,old)
        self.assertEqual(self.session.status,'playing')

    async def test_stop_during_owned_clear_wins(self):
        await self.transition(external='stop')

    async def test_lease_loss_during_owned_clear_wins(self):
        await self.transition(external='lease')
