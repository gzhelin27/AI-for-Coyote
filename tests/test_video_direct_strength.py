"""Only a registered, live video session may omit the upward step limit."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from backend.output_coordinator import OutputIntentKind
from backend.video.csv_timeline import parse_video_csv
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from backend.video.waveforms import resolve_video_plan
from tests.test_game_loop_timeline import make_game_loop_for_test


class VideoDirectStrengthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = make_game_loop_for_test(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.h.safety.max_step = 10
        self.h.safety.user_caps.update(A=40, B=40)
        self.now = 0.
        self.session = None

    async def play(self, a=30, b=0, *, observe=True):
        timeline = parse_video_csv(f'start_time,end_time,A_target,B_target\n0:00:00,0:01:00,{a},{b}\n'.encode(), duration_ms=70000)
        plan = resolve_video_plan(timeline, allowed=('呼吸',), library_sha256='a'*64, seed=1)
        self.output = GameLoopVideoOutput(self.h.loop, clock=lambda:self.now)
        self.session = VideoSession(plan,self.output,source_id='synthetic',duration_ms=70000,
                                    timeline=timeline,clock=lambda:self.now,watch=False)
        await self.session.start()
        self.addAsyncCleanup(self.session.close)
        if observe:
            await self.session.observe(dict(session_id=self.session.session_id,epoch=1,sequence=1,position_ms=0,state='playing',rate=1))
            await self.session.flush()

    async def test_video_reaches_target_in_one_confirmed_command(self):
        await self.play(30,15)
        self.assertEqual((self.output.snapshot('A').strength,self.output.snapshot('B').strength),(30,15))
        self.assertEqual([(r['channel'],r['strength']) for r in self.session.audit],[('A',30),('B',15)])
        self.assertEqual(self.h.safety.max_step,10)

    async def test_csv_above_cap_is_clipped_once(self):
        await self.play(60)
        self.assertEqual(self.output.snapshot('A').strength,40)
        self.assertEqual(len(self.session.audit),1)
        self.assertEqual(self.session.audit[0]['target'],60)
        self.assertEqual(self.session.audit[0]['capped_target'],40)

    async def test_json_flags_and_ordinary_timeline_cannot_bypass_step(self):
        generations = self.h.loop.begin_timeline_output(('A','B'))
        action = dict(op='hold_strength',channel='A',value=30,video=True,
                      skip_max_step=True,_video_owner=True,_video_hold_authority=True)
        executed,dropped=await self.h.loop.execute_actions([action],intent=OutputIntentKind.TIMELINE_OR_REPLAY,
            owner_generations=generations,waveform_managed_channels=('A',),video_remaining_ms=lambda:1000)
        self.assertFalse(dropped)
        self.assertEqual(self.h.safety.current['A'],10)
        self.assertEqual(self.h.safety.validate(dict(action,value=60))[2]['value'],20)

    async def test_forged_internal_owner_argument_is_rejected(self):
        generations=self.h.loop.begin_timeline_output(('A','B'))
        with self.assertRaises(ValueError):
            await self.h.loop.execute_actions([dict(op='hold_strength',channel='A',value=30)],
                intent=OutputIntentKind.TIMELINE_OR_REPLAY,owner_generations=generations,
                waveform_managed_channels=('A',),video_remaining_ms=lambda:1000,_video_owner=object())
        self.assertEqual(self.h.safety.current['A'],0)

    async def test_app_cap_still_limits_video(self):
        self.h.safety.app_caps['A']=23
        await self.play(60)
        self.assertEqual(self.output.snapshot('A').strength,23)

    async def test_paused_owner_cannot_authorize_direct_strength(self):
        await self.play()
        await self.session.observe(dict(session_id=self.session.session_id,epoch=1,sequence=2,position_ms=0,state='paused',rate=1))
        await self.session.flush()
        receipt=await self.output.set_strength('A',30,remaining=lambda:10000)
        self.assertFalse(receipt.success)
        self.assertEqual(self.output.snapshot('A').strength,0)

    async def test_single_phone_target_waits_for_ack_before_confirmation(self):
        from backend.relay_client import RelayClient
        from tests.test_relay_acknowledgement import TickPhone
        await self.play(observe=False)
        relay=RelayClient('ws://offline.invalid')
        relay.clients['phone']={'devices':[{'slotId':'slot'}]}
        phone=TickPhone(relay)
        relay.ws=phone
        original=self.h.loop.relay
        self.h.loop.relay=relay
        self.h.safety.dry_run=False
        def increments():
            return [f['data']['data']['v'] for f in phone.sent
                    if f['data']['m']=='device.op' and f['data']['data']['t']==3]
        try:
            await self.session.observe(dict(session_id=self.session.session_id,epoch=1,sequence=1,position_ms=0,state='playing',rate=1))
            for _ in range(150):
                await asyncio.sleep(0)
                if increments(): break
                phone.tick()
            self.assertEqual(increments(),[30], 'one target command, not three +10 commands')
            self.assertEqual(self.output.snapshot('A').strength,0,'send is not a confirmed ACK')
            self.assertEqual(self.session.audit,[])
            for _ in range(150):
                phone.tick()
                await asyncio.sleep(0)
                if self.session._work.done(): break
            await self.session.flush()
            self.assertEqual(self.output.snapshot('A').strength,30)
            self.assertEqual(phone.strength,30)
            self.assertEqual(increments(),[30])
            self.assertEqual(len(self.session.audit),1)
        finally:
            self.h.safety.dry_run=True
            self.h.loop.relay=original

    async def test_cap_reduction_preempts_direct_video_target_waiting_for_ack(self):
        from backend.relay_client import RelayClient
        from tests.test_relay_acknowledgement import TickPhone
        await self.play(observe=False)
        relay = RelayClient('ws://offline.invalid')
        relay.clients['phone'] = {'devices': [{'slotId': 'slot'}]}
        phone = TickPhone(relay)
        relay.ws = phone
        original = self.h.loop.relay
        self.h.loop.relay = relay
        self.h.safety.dry_run = False
        try:
            await self.session.observe(dict(session_id=self.session.session_id, epoch=1,
                sequence=1, position_ms=0, state='playing', rate=1))
            for _ in range(150):
                await asyncio.sleep(0)
                increases = [f for f in phone.sent
                    if f['data']['m'] == 'device.op' and f['data']['data']['t'] == 3]
                if increases:
                    break
                phone.tick()
            self.assertEqual([f['data']['data']['v'] for f in increases], [30])
            self.assertEqual(self.output.snapshot('A').strength, 0)
            self.assertEqual(self.session.audit, [])
            before = len(phone.sent)
            reduction = asyncio.create_task(self.h.loop.set_runtime_cap('A', 5))
            for _ in range(150):
                await asyncio.sleep(0)
            self.assertTrue(any(f['data']['m'] == 'device.op.clear'
                for f in phone.sent[before:]), 'cap reduction must preempt before the old ACK')
            for _ in range(250):
                phone.tick()
                await asyncio.sleep(0)
                if reduction.done() and self.session._work.done():
                    break
            result = await asyncio.wait_for(reduction, 2)
            await asyncio.wait_for(self.session.flush(), 2)
            self.assertEqual(result['effective_cap'], 5)
            self.assertEqual(self.output.snapshot('A').strength, 0)
            self.assertEqual(phone.strength, 0)
            self.assertEqual(self.session.audit, [], 'retired 30 ACK cannot become a confirmed video event')
            self.assertNotEqual(self.session.status, 'playing')
            self.assertFalse(self.h.loop.loop_tasks)
        finally:
            self.h.safety.dry_run = True
            self.h.loop.relay = original
