"""A real relay event decoder with an in-memory phone must preserve video ownership."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.relay_client import RelayClient
from backend.output_coordinator import OutputOwnership, TransportOutcome
from backend.video.csv_timeline import parse_video_csv
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from backend.video.waveforms import resolve_video_plan
from tests.test_game_loop_timeline import make_game_loop_for_test
from tests.test_relay_acknowledgement import TickPhone


class VideoFeedbackOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = make_game_loop_for_test(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.now = 0.0
        self.sequence = 0
        timeline = parse_video_csv(
            b'start_time,end_time,A_target,B_target\n'
            b'0:00:00,0:00:01,20,20\n0:00:01,0:00:10,0,30\n',
            duration_ms=10000,
        )
        plan = resolve_video_plan(timeline, allowed=('呼吸',),
                                  library_sha256='a' * 64, seed=1)
        self.output = GameLoopVideoOutput(self.h.loop, clock=lambda: self.now)
        self.session = VideoSession(
            plan, self.output, source_id='synthetic', duration_ms=10000,
            timeline=timeline, clock=lambda: self.now, watch=False,
        )
        self.addAsyncCleanup(self.session.close)
        await self.session.start()
        await self.observe(0)
        await self.session.flush()
        self.now = .9
        await self.observe(900)
        await self.session.flush()

    async def observe(self, position):
        self.sequence += 1
        await self.session.observe(dict(
            session_id=self.session.session_id, epoch=self.session.epoch,
            sequence=self.sequence, position_ms=position,
            state='playing', rate=1,
        ))

    async def test_slots_patch_ack_during_video_clear_keeps_playing(self):
        report_done = asyncio.Event()
        async def on_event(event, _payload):
            if event == 'slots_patch':
                client = relay.clients['phone']
                await self.h.loop.update_device_state(
                    client['props'], client['slotState'])
                report_done.set()

        relay = RelayClient('ws://offline.invalid', on_event=on_event)
        relay.clients['phone'] = {
            'devices': [{'slotId': 'slot'}], 'props': {}, 'slotState': {},
        }
        phone = TickPhone(relay)
        phone.strength = 20
        relay.ws = phone
        old_relay = self.h.loop.relay
        self.h.loop.relay = relay
        self.h.safety.dry_run = False
        held = False
        try:
            await self.h.loop._action_lock.acquire()
            held = True
            self.now = 1.05
            await self.observe(1050)
            for _ in range(100):
                await asyncio.sleep(0)
                if self.output.clearing:
                    break
            self.assertTrue(self.output.clearing)
            coordinator = self.h.loop.output_coordinator
            generation = coordinator.generation('A')
            relay._handle_frame({
                'type': 'message', 'clientId': 'phone', 'data': {
                    't': 'ev', 'ev': 'slots.patch', 'slots': [{
                        'slotId': 'slot',
                        'props': {'intensityA': 20, 'intensityB': 20},
                        'slotState': {},
                    }],
                },
            })
            for _ in range(200):
                phone.tick()
                await asyncio.sleep(0)
                if report_done.is_set():
                    break
            self.assertTrue(report_done.is_set())
            self.assertEqual(coordinator.generation('A'), generation)
            self.assertTrue(self.output.owns_control())
            await self.session.tick()
            self.h.loop._action_lock.release()
            held = False
            for _ in range(300):
                phone.tick()
                await asyncio.sleep(0)
                if self.session._work.done():
                    break
            await asyncio.wait_for(self.session.flush(), 2)
            self.assertEqual(self.session.status, 'playing')
            self.assertEqual(self.session.epoch, 1)
            self.assertFalse(self.session.state()['clear_pending'])
        finally:
            if held:
                self.h.loop._action_lock.release()
            self.h.safety.dry_run = True
            self.h.loop.relay = old_relay

    async def test_disable_while_feedback_clear_waits_retires_old_owner(self):
        loop = self.h.loop
        coordinator = loop.output_coordinator
        coordinator.seed_confirmed('A', strength=20)
        coordinator.require_clear('A')
        owner = OutputOwnership(coordinator)
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_clear(_channel, _snapshot, enabled):
            entered.set()
            await release.wait()
            return TransportOutcome(True, {
                'strength': 0, 'waveform': None,
                'waveform_mode': None, 'enabled': enabled,
            })

        old_clear = loop._transport_safety_clear
        loop._transport_safety_clear = delayed_clear
        held = False
        try:
            await loop._action_lock.acquire()
            held = True
            retry = asyncio.create_task(loop._reconcile_channel_safety_locked(
                'A', retry_existing_clear=True))
            await asyncio.wait_for(entered.wait(), 1)
            disable = asyncio.create_task(loop.set_channel_enabled('A', False))
            await asyncio.sleep(0)
            retired_during_disable = not owner.is_current()
            release.set()
            result = await asyncio.wait_for(retry, 1)
            pending_after_retry = coordinator.pending('A').clear_required
            loop._action_lock.release()
            held = False
            await asyncio.wait_for(disable, 1)
            self.assertTrue(retired_during_disable)
            self.assertTrue(pending_after_retry)
            self.assertEqual(result, {'executed': [], 'dropped': []})
            self.assertFalse(loop.safety.enabled['A'])
        finally:
            release.set()
            if held:
                loop._action_lock.release()
            loop._transport_safety_clear = old_clear
