import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.video.api import install_video_routes
from backend.video.source import VideoSourceStore
from backend.video.output import GameLoopVideoOutput, OutputReceipt
from tests.test_game_loop_timeline import make_game_loop_for_test

CSV = b'start_time,end_time,A_target,B_target\n0:00:00,0:00:01,20,0\n'


class RoutingSession:
    def __init__(self, plan, output, **kwargs):
        self.closed = False
        self.output = output
        self.session_id = 'routing-session'
        self.source_id = kwargs['source_id']
        self.status = 'paused'
    async def start(self):
        return self.state()
    def state(self):
        return {'session_id': self.session_id, 'source_id': self.source_id, 'status': self.status}
    async def observe(self, value):
        if value.get('state') not in ('playing', 'paused'):
            raise ValueError('invalid observation')
        self.status = value['state']
        return self.state()
    async def close(self, reason):
        self.closed = True
        self.status = 'closed'
        return self.state()
    def interrupt(self, reason):
        self.status = 'paused'
        self.output.preempt()


class VideoEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'videos'
        harness = make_game_loop_for_test(Path(self.tmp.name) / 'replay')
        self.state = SimpleNamespace(loop=harness.loop, safety=harness.safety,
            timeline_session=harness.controller, timeline_transition_lock=asyncio.Lock(), cfg=harness.cfg)
        self.app = FastAPI()
        install_video_routes(self.app, self.state, self.root, session_factory=RoutingSession)
        @self.app.post('/api/manual')
        async def manual():
            return {'video_closed': self.state.video_session.closed}
        @self.app.post('/api/estop')
        async def estop():
            return {'emergency': True}
        self.client = self.enterContext(TestClient(self.app))

    def import_source(self):
        response = self.client.post('/api/video/sources', data={'duration_ms': '2000'},
            files={'file': ('clip.mp4', b'synthetic-media', 'video/mp4')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['source_id']

    def start_session(self):
        source_id = self.import_source()
        response = self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/api/video/sessions', json={'source_id': source_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['session_id']

    def test_lazy_store_and_missing_source_validation(self):
        self.assertFalse(self.root.exists())
        self.assertEqual(self.client.get('/api/video/state').json(), {'session': None})
        self.assertFalse(self.root.exists())
        self.assertEqual(self.client.post('/api/video/sessions', json={'source_id': 'bad'}).status_code, 400)
        self.assertEqual(self.client.post('/api/video/sessions', json={'source_id': 'a' * 32}).status_code, 404)

    def assert_legacy_handlers_overlap(self):
        entered = [asyncio.Event(), asyncio.Event()]
        @self.app.post('/api/story/concurrency/{side}')
        async def concurrent_handler(side: int):
            entered[side].set()
            try:
                await asyncio.wait_for(entered[1 - side].wait(), 0.3)
            except asyncio.TimeoutError:
                return {'overlapped': False}
            return {'overlapped': True}
        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.client.post, '/api/story/concurrency/0')
            second = pool.submit(self.client.post, '/api/story/concurrency/1')
            self.assertTrue(first.result().json()['overlapped'])
            self.assertTrue(second.result().json()['overlapped'])

    def test_no_video_does_not_serialize_cooperating_story_handlers(self):
        self.assert_legacy_handlers_overlap()

    def test_completed_video_does_not_serialize_cooperating_story_handlers(self):
        session_id = self.start_session()
        self.client.post(f'/api/video/sessions/{session_id}/stop')
        self.assert_legacy_handlers_overlap()

    def test_active_video_handoff_does_not_serialize_cooperating_story_handlers(self):
        self.start_session()
        self.assert_legacy_handlers_overlap()

    def test_conflicting_request_during_video_start_closes_new_video_before_handler(self):
        source_id = self.import_source()
        self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        entered, release = threading.Event(), threading.Event()
        original = self.state.loop.stop_timeline_session
        async def slow_handoff():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original()
        with patch.object(self.state.loop, 'stop_timeline_session', slow_handoff), ThreadPoolExecutor() as pool:
            starting = pool.submit(self.client.post, '/api/video/sessions', json={'source_id': source_id})
            try:
                self.assertTrue(entered.wait(1))
                manual = pool.submit(self.client.post, '/api/manual')
                self.assertFalse(manual.done())
            finally:
                release.set()
            self.assertEqual(starting.result().status_code, 200)
            self.assertTrue(manual.result().json()['video_closed'])

    def test_new_video_rejects_conflicting_handler_already_in_flight(self):
        source_id = self.import_source()
        self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        entered, release = threading.Event(), threading.Event()
        @self.app.post('/api/story/slow-owner')
        async def slow_owner():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {'done': True}
        with ThreadPoolExecutor() as pool:
            legacy = pool.submit(self.client.post, '/api/story/slow-owner')
            try:
                self.assertTrue(entered.wait(1))
                result = self.client.post('/api/video/sessions', json={'source_id': source_id})
                self.assertEqual(result.status_code, 409)
                self.assertIsNone(self.state.video_session)
            finally:
                release.set()
            self.assertEqual(legacy.result().status_code, 200)

    def test_waiting_video_start_cannot_block_legacy_cancellation(self):
        source_id = self.import_source()
        self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
        @self.app.post('/api/story/locked-owner')
        async def locked_owner():
            async with self.state.timeline_transition_lock:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return {'done': True}
        @self.app.post('/api/story/cancel-owner')
        async def cancel_owner():
            cancelled.set()
            release.set()
            return {'cancelled': True}
        with ThreadPoolExecutor() as pool:
            legacy = pool.submit(self.client.post, '/api/story/locked-owner')
            try:
                self.assertTrue(entered.wait(1))
                starting = pool.submit(self.client.post, '/api/video/sessions', json={'source_id': source_id})
                time.sleep(0.1)
                cancelling = pool.submit(self.client.post, '/api/story/cancel-owner')
                self.assertEqual(starting.result(timeout=0.5).status_code, 409)
                self.assertEqual(cancelling.result(timeout=0.5).status_code, 200)
                self.assertTrue(cancelled.is_set())
            finally:
                release.set()
            self.assertEqual(legacy.result().status_code, 200)

    def test_upload_duration_and_csv_validation(self):
        response = self.client.post('/api/video/sources', data={'duration_ms': '1.5'}, files={'file': ('x', b'abc')})
        self.assertEqual(response.status_code, 422)
        source_id = self.import_source()
        self.assertEqual(self.client.post('/api/video/sessions', json={'source_id': source_id}).status_code, 409)
        response = self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x', b'bad')})
        self.assertEqual(response.status_code, 400)

    def test_manual_handoff_closes_video_before_handler(self):
        session_id = self.start_session()
        self.assertEqual(self.client.get('/api/video/state').json()['status'], 'paused')
        self.assertTrue(self.client.post('/api/manual').json()['video_closed'])
        self.assertEqual(self.client.post(f'/api/video/sessions/{session_id}/stop').status_code, 200)

    def test_clock_single_owner_invalid_message_and_disconnect(self):
        session_id = self.start_session()
        path = f'/api/video/sessions/{session_id}/clock'
        with self.client.websocket_connect(path) as socket:
            socket.send_json({'state': 'playing'})
            for _ in range(5):
                if socket.receive_json().get('status') == 'playing':
                    break
            else:
                self.fail('playing observation was not delivered')
            with self.assertRaises(Exception):
                with self.client.websocket_connect(path):
                    pass
            socket.send_json({'state': 'bad'})
            for _ in range(5):
                if 'error' in socket.receive_json():
                    break
            else:
                self.fail('invalid observation did not return an error')
        self.assertTrue(self.state.video_session.closed)

    def test_slow_csv_disk_io_does_not_delay_emergency_stop(self):
        self.start_session()
        source_id = self.state.video_session.source_id
        entered, release = threading.Event(), threading.Event()
        original = VideoSourceStore.bind_csv
        def slow_bind(store, identity, payload):
            entered.set()
            release.wait(3)
            return original(store, identity, payload)
        with patch.object(VideoSourceStore, 'bind_csv', slow_bind), ThreadPoolExecutor() as pool:
            upload = pool.submit(self.client.post, f'/api/video/sources/{source_id}/csv',
                                 files={'file': ('x.csv', CSV)})
            try:
                self.assertTrue(entered.wait(1))
                stop = pool.submit(self.client.post, '/api/estop')
                self.assertEqual(stop.result(timeout=0.5).status_code, 200)
                self.assertFalse(upload.done())
            finally:
                release.set()
            self.assertEqual(upload.result().status_code, 200)

    def test_emergency_stop_bypasses_pending_mode_handoff(self):
        self.start_session()
        entered, release = threading.Event(), threading.Event()
        original = self.state.video_session.close
        async def blocked_close(reason):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original(reason)
        with patch.object(self.state.video_session, 'close', blocked_close), ThreadPoolExecutor() as pool:
            handoff = pool.submit(self.client.post, '/api/manual')
            try:
                self.assertTrue(entered.wait(1))
                stop = pool.submit(self.client.post, '/api/estop')
                self.assertEqual(stop.result(timeout=0.5).status_code, 200)
                self.assertFalse(handoff.done())
            finally:
                release.set()
            self.assertEqual(handoff.result().status_code, 200)

    def test_replacement_waits_for_clear_even_after_old_session_marks_closed(self):
        self.start_session()
        prior = self.state.video_session
        prior.closed = True
        entered, release = threading.Event(), threading.Event()
        async def pending_clear(reason):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return prior.state()
        with patch.object(prior, 'close', pending_clear), ThreadPoolExecutor() as pool:
            replacement = pool.submit(self.client.post, '/api/video/sessions', json={'source_id': prior.source_id})
            try:
                self.assertTrue(entered.wait(1), 'replacement skipped outstanding old clear')
                self.assertFalse(replacement.done())
                self.assertIs(self.state.video_session, prior)
            finally:
                release.set()
            self.assertEqual(replacement.result().status_code, 200)

    def test_plan_persistence_failure_prevents_session_activation(self):
        source_id = self.import_source()
        self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        with patch.object(VideoSourceStore, 'save_plan', side_effect=OSError('disk unavailable')):
            response = self.client.post('/api/video/sessions', json={'source_id': source_id})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.state.video_session)

    def test_slow_plan_commit_allows_estop_and_binding_change_prevents_activation(self):
        source_id = self.import_source()
        self.client.post(f'/api/video/sources/{source_id}/csv', files={'file': ('x.csv', CSV)})
        entered, release = threading.Event(), threading.Event()
        original = VideoSourceStore.save_plan
        def slow_save(store, identity, plan):
            saved = original(store, identity, plan)
            entered.set()
            release.wait(3)
            return saved
        with patch.object(VideoSourceStore, 'save_plan', slow_save), ThreadPoolExecutor() as pool:
            start = pool.submit(self.client.post, '/api/video/sessions', json={'source_id': source_id})
            try:
                self.assertTrue(entered.wait(1))
                stop = pool.submit(self.client.post, '/api/estop')
                self.assertEqual(stop.result(timeout=0.5).status_code, 200)
                self.assertIsNone(self.state.video_session)
                changed = self.client.post(f'/api/video/sources/{source_id}/csv',
                    files={'file': ('new.csv', CSV.replace(b',20,0', b',30,0'))})
                self.assertEqual(changed.status_code, 200)
            finally:
                release.set()
            self.assertEqual(start.result().status_code, 409)
            self.assertIsNone(self.state.video_session)

    def test_session_start_rejects_csv_replaced_during_source_scan(self):
        self.start_session()
        identity = self.state.video_session.source_id
        entered, release = threading.Event(), threading.Event()
        original = VideoSourceStore.get
        def slow_get(store, source_id):
            entered.set()
            release.wait(3)
            return original(store, source_id)
        with patch.object(VideoSourceStore, 'get', slow_get), ThreadPoolExecutor() as pool:
            start = pool.submit(self.client.post, '/api/video/sessions', json={'source_id': identity})
            try:
                self.assertTrue(entered.wait(1))
                changed = self.client.post(f'/api/video/sources/{identity}/csv',
                    files={'file': ('new.csv', CSV.replace(b',20,0', b',30,0'))})
                self.assertEqual(changed.status_code, 200)
            finally:
                release.set()
            self.assertEqual(start.result().status_code, 409)

    def test_real_session_socket_pause_preempts_pending_output(self):
        app = FastAPI()
        install_video_routes(app, self.state, self.root)
        entered, cancelled = threading.Event(), threading.Event()
        async def blocked_strength(output, channel, target, **kwargs):
            entered.set()
            try:
                while output.is_current():
                    await asyncio.sleep(0.01)
                return OutputReceipt(False, 0, error='retired output')
            finally:
                cancelled.set()
        with TestClient(app) as client, patch.object(GameLoopVideoOutput, 'set_strength', blocked_strength):
            source = client.post('/api/video/sources', data={'duration_ms': '2000'},
                files={'file': ('clip.mp4', b'synthetic', 'video/mp4')}).json()
            identity = source['source_id']
            client.post(f'/api/video/sources/{identity}/csv', files={'file': ('x.csv', CSV)})
            started = client.post('/api/video/sessions', json={'source_id': identity})
            self.assertEqual(started.status_code, 200, started.text)
            session = started.json()
            with client.websocket_connect(f"/api/video/sessions/{session['session_id']}/clock") as socket:
                observation = {'session_id': session['session_id'], 'epoch': session['epoch'],
                               'sequence': 1, 'position_ms': 0, 'state': 'playing', 'rate': 1}
                socket.send_json(observation)
                self.assertTrue(entered.wait(1), 'real session did not attempt output')
                socket.send_json(dict(observation, sequence=2, state='paused'))
                for _ in range(10):
                    if socket.receive_json().get('status') == 'paused':
                        break
                else:
                    self.fail('socket pause waited for pending strength')
                self.assertEqual(self.state.video_session.status, 'paused')
            self.assertTrue(cancelled.wait(0.5), 'disconnect did not cancel pending output')
            stopped = client.post(f"/api/video/sessions/{session['session_id']}/stop")
            self.assertEqual(stopped.status_code, 200, stopped.text)
            replacement = client.post('/api/video/sessions', json={'source_id': identity}).json()
            self.assertNotEqual(replacement['session_id'], session['session_id'])
            self.assertEqual(client.post(f"/api/video/sessions/{session['session_id']}/stop").status_code, 200)
            self.assertFalse(self.state.video_session.closed, 'old stop closed the new owner')
