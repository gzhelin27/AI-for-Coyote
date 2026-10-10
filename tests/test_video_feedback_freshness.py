"""Exercise phone deltas through the real relay decoder and application callback."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.main import AppState
from backend.relay_client import RelayClient
from backend.video.csv_timeline import parse_video_csv
from backend.video.output import GameLoopVideoOutput
from backend.video.session import VideoSession
from backend.video.waveforms import resolve_video_plan
from tests.test_game_loop_timeline import make_game_loop_for_test


class VideoFeedbackFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = make_game_loop_for_test(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.h.safety.caps.update(A=40, B=40)
        self.h.safety.user_caps.update(A=40, B=40)
        self.h.safety.max_step = 40
        self.reports = asyncio.Queue()

        async def on_event(event, payload):
            try:
                await AppState.on_relay_event(self.app, event, payload)
            except Exception as exc:
                self.reports.put_nowait(exc)
            else:
                self.reports.put_nowait(None)

        self.relay = RelayClient('ws://offline.invalid', on_event=on_event)
        self.relay.clients['phone'] = {
            'devices': [{'slotId': 'slot'}], 'props': {}, 'slotState': {},
        }
        self.app = SimpleNamespace(relay=self.relay, loop=self.h.loop, broadcast=AsyncMock())
        timeline = parse_video_csv(
            b'start_time,end_time,A_target,B_target\n0:00:00,0:00:10,20,20\n',
            duration_ms=10000)
        self.output = GameLoopVideoOutput(self.h.loop, clock=lambda: 0.)
        self.session = VideoSession(
            resolve_video_plan(timeline, allowed=('呼吸',), library_sha256='a' * 64, seed=1),
            self.output, source_id='synthetic', duration_ms=10000,
            timeline=timeline, clock=lambda: 0., watch=False)
        self.addAsyncCleanup(self.session.close)
        await self.session.start()
        await self.session.observe(dict(session_id=self.session.session_id, epoch=1,
                                        sequence=1, position_ms=0, state='playing', rate=1))
        await self.session.flush()

    async def report(self, props=None, policy=None, *, client='phone', slot='slot'):
        self.relay._handle_frame({
            'type': 'message', 'clientId': client,
            'data': {'t': 'ev', 'ev': 'slots.patch', 'slots': [{
                'slotId': slot, 'props': props or {}, 'slotState': policy or {},
            }]},
        })
        error = await asyncio.wait_for(self.reports.get(), 1)
        if error:
            raise error

    async def test_b_only_report_after_wave_end_does_not_replay_old_a_amplitude(self):
        # A's frame amplitude was ignored while its finite waveform was active.
        self.h.safety.pulse_until['A'] = float('inf')
        await self.report({'intensityA': 80})
        self.assertEqual(self.output.snapshot('A').strength, 20)
        generation = dict(self.output.generations)
        self.h.safety.pulse_until['A'] = 0.
        await self.report({'intensityB': 20})
        self.assertEqual(self.output.snapshot('A').strength, 20)
        self.assertTrue(self.output.owns_control())
        await self.session.tick()
        await self.session.flush()
        self.assertEqual(self.session.status, 'playing')
        self.assertEqual(self.output.generations, generation)
        self.assertEqual(self.h.relay.sent_frames, [])

    async def test_policy_only_patch_does_not_replay_old_strength(self):
        self.h.safety.pulse_until['A'] = float('inf')
        await self.report({'intensityA': 80})
        self.h.safety.pulse_until['A'] = 0.
        await self.report(policy={'channelA': {'comfortLimit': {'overheatPercent': 0}}})
        self.assertEqual(self.output.snapshot('A').strength, 20)
        self.assertTrue(self.output.owns_control())

    async def test_other_client_or_slot_cannot_change_active_output(self):
        self.relay.clients['other'] = {
            'devices': [{'slotId': 'elsewhere'}], 'props': {}, 'slotState': {},
        }
        self.h.safety.pulse_until['A'] = 0.
        await self.report({'intensityA': 80}, slot='elsewhere')
        self.assertEqual(self.output.snapshot('A').strength, 20)
        await self.report({'intensityA': 80}, client='other', slot='elsewhere')
        self.assertTrue(self.output.owns_control())

    async def test_fresh_over_cap_strength_still_reduces_and_pauses_video(self):
        self.h.safety.pulse_until['A'] = 0.
        with self.assertLogs('ai-for-coyote.game', level='WARNING') as captured:
            await self.report({'intensityA': 80})
        self.assertIn('channel=A reported=80 cap=40', '\n'.join(captured.output))
        self.assertNotIn('synthetic', '\n'.join(captured.output))
        self.assertEqual(self.output.snapshot('A').strength, 40)
        await self.session.tick()
        await self.session.flush()
        self.assertEqual(self.session.status, 'paused')
        self.assertEqual(self.output.snapshot('A').strength, 0)

    async def test_fresh_overheat_without_strength_still_pauses_video(self):
        await self.report(policy={'channelA': {'comfortLimit': {'overheat': True}}})
        self.assertTrue(self.h.safety.overheat['A'])
        await self.session.tick()
        await self.session.flush()
        self.assertEqual(self.session.status, 'paused')
        self.assertEqual(self.output.snapshot('A').strength, 0)

    async def test_later_patch_cannot_rewrite_queued_overheat_report(self):
        queued = asyncio.Queue()
        releases = [asyncio.Event(), asyncio.Event()]
        gates = iter(releases)

        async def delayed_event(event, payload):
            await next(gates).wait()
            await AppState.on_relay_event(self.app, event, payload)
            queued.put_nowait(self.h.safety.overheat['A'])

        self.relay.on_event = delayed_event
        for overheated in (True, False):
            self.relay._handle_frame({
                'type': 'message', 'clientId': 'phone', 'data': {
                    't': 'ev', 'ev': 'slots.patch', 'slots': [{
                        'slotId': 'slot', 'slotState': {
                            'channelA': {'comfortLimit': {'overheat': overheated}},
                        },
                    }],
                },
            })
        releases[0].set()
        self.assertTrue(await asyncio.wait_for(queued.get(), 1))
        releases[1].set()
        self.assertFalse(await asyncio.wait_for(queued.get(), 1))

    async def test_empty_event_does_not_refresh_cached_strength(self):
        self.relay.clients['phone']['props']['intensityA'] = 80
        self.h.safety.pulse_until['A'] = 0.
        await AppState.on_relay_event(self.app, 'slots_patch', {})
        self.assertEqual(self.output.snapshot('A').strength, 20)
        self.assertTrue(self.output.owns_control())
