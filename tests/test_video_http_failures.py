"""Real video sessions with a fake relay exercise HTTP failure and recovery."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.video.api import install_video_routes
from tests.test_game_loop_timeline import make_game_loop_for_test

CSV = b'start_time,end_time,A_target,B_target\n0:00:00,0:00:01,20,0\n'


class VideoHttpFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.h = make_game_loop_for_test(Path(temporary) / 'replay')
        self.state = SimpleNamespace(loop=self.h.loop, safety=self.h.safety,
            timeline_transition_lock=asyncio.Lock(), cfg=self.h.cfg)
        app = FastAPI()
        install_video_routes(app, self.state, Path(temporary) / 'video')
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        self.addCleanup(setattr, self.h.safety, 'dry_run', True)
        source = self.local_source()
        self.assertEqual(source.status_code, 200)
        self.source_id = source.json()['source_id']
        self.assertEqual(self.csv().status_code, 200)

    def local_source(self):
        return self.client.post('/api/video/local-sources', json={
            'filename': 'synthetic.mp4', 'size': 100, 'duration_ms': 2000, 'last_modified': 1})

    def csv(self):
        return self.client.post(f'/api/video/sources/{self.source_id}/csv', files={'file': ('test.csv', CSV)})

    def start(self):
        return self.client.post('/api/video/sessions', json={'source_id': self.source_id})

    def assert_retryable(self, response):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('重试', response.json()['detail'])
        self.assertIn('清零', response.json()['detail'])

    def test_real_mode_without_device_rejects_before_creating_session_or_clear(self):
        self.h.safety.dry_run = False  # cfg remains True: runtime is authoritative.
        with patch.object(self.h.relay, 'get_slot_id', return_value=None):
            response = self.start()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn('连接', response.json()['detail'])
            self.assertIsNone(self.state.video_session)
            self.assertFalse(self.h.relay.sent_frames)
            self.assertFalse(self.h.loop.output_coordinator.pending('A').clear_required)
            self.assertEqual(self.start().status_code, 409)
            self.assertEqual(self.csv().status_code, 200, 'failed preflight must not poison later CSV imports')
            self.assertIsNone(self.state.video_session)

    def test_dry_run_without_device_still_starts_using_runtime_mode(self):
        self.h.cfg['app']['dry_run'] = False
        with patch.object(self.h.relay, 'first_client_id', return_value=None), patch.object(self.h.relay, 'get_slot_id', return_value=None):
            response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['dry_run'])
        self.assertFalse(self.h.relay.sent_frames)

    def test_failed_start_and_failed_cleanup_remain_retryable_and_block_replacement(self):
        self.h.safety.dry_run = False
        with patch.object(self.h.relay, 'send_frame', AsyncMock(return_value=False)):
            self.assert_retryable(self.start())
            prior = self.state.video_session
            self.assertTrue(prior.closed)
            self.assertTrue(prior.state()['clear_pending'])
            self.assert_retryable(self.start())
            self.assertIs(self.state.video_session, prior)
            self.assertTrue(self.h.loop.output_coordinator.pending('A').clear_required)
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNot(self.state.video_session, prior)
        self.assertFalse(response.json()['clear_pending'])

    def test_source_csv_and_stop_report_failed_clear_without_losing_pending_session(self):
        self.assertEqual(self.start().status_code, 200)
        prior = self.state.video_session
        stop_url = f'/api/video/sessions/{prior.session_id}/stop'
        self.h.safety.dry_run = False
        with patch.object(self.h.relay, 'send_frame', AsyncMock(return_value=False)):
            for request in (self.csv, self.local_source,
                            lambda: self.client.post('/api/video/sources', data={'duration_ms': '2000'}, files={'file': ('synthetic.mp4', b'media')}),
                            lambda: self.client.post(stop_url)):
                with self.subTest(request=request):
                    self.assert_retryable(request())
                    self.assertIs(self.state.video_session, prior)
                    self.assertTrue(prior.state()['clear_pending'])
        response = self.client.post(stop_url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['clear_pending'])
        self.assertEqual(self.csv().status_code, 200)

    def test_missing_device_check_does_not_skip_cleanup_of_existing_session(self):
        self.assertEqual(self.start().status_code, 200)
        prior = self.state.video_session
        self.h.safety.dry_run = False
        with patch.object(self.h.relay, 'get_slot_id', return_value=None):
            self.assert_retryable(self.start())
        self.assertIs(self.state.video_session, prior)
        self.assertTrue(prior.closed)
        self.assertTrue(prior.state()['clear_pending'])
