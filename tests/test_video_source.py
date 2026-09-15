import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import unittest

from backend.video.source import VideoSourceStore
from backend.video.waveforms import resolve_video_plan


CSV = b'start_time,end_time,A_target,B_target\n0:00:00,0:00:01,20,0\n'


async def chunks(*parts):
    for part in parts:
        yield part


class VideoSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'video'
        self.store = VideoSourceStore(self.root, max_bytes=2 * 1024 * 1024)
        self.addCleanup(self.store.close)

    async def test_stream_identity_display_name_and_restart(self):
        source = await self.store.import_stream('../folder\\clip.mp4', chunks(b'abc', b'def'), 2000)
        self.assertEqual(source.filename, 'clip.mp4')
        self.assertEqual(source.sha256, hashlib.sha256(b'abcdef').hexdigest())
        self.assertEqual(source.size, 6)
        self.assertEqual(self.store.media_path(source.source_id).read_bytes(), b'abcdef')
        other = VideoSourceStore(self.root)
        self.addCleanup(other.close)
        self.assertEqual(other.get(source.source_id), source)

    async def test_failed_and_cancelled_imports_leave_no_files(self):
        async def cancelled():
            yield b'abc'
            raise asyncio.CancelledError()
        for stream, error in [(chunks(b'x' * (2 * 1024 * 1024 + 1)), ValueError),
                              (chunks(), ValueError), (cancelled(), asyncio.CancelledError)]:
            with self.assertRaises(error):
                await self.store.import_stream('x.mp4', stream, 2000)
            self.assertEqual(list(self.root.iterdir()), [])

    async def test_large_chunks_are_split_and_duration_is_validated(self):
        data = b'x' * (1024 * 1024 + 17)
        source = await self.store.import_stream('x.mp4', chunks(data), 2000)
        self.assertEqual(source.sha256, hashlib.sha256(data).hexdigest())
        for duration in (True, 0, -1, 1.2, float('inf'), 10**20):
            with self.assertRaises(ValueError):
                await self.store.import_stream('x', chunks(b'x'), duration)

    async def test_slow_disk_write_does_not_block_event_loop_and_cancel_waits_for_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        original_write = os.write
        def slow_write(descriptor, payload):
            if not entered.is_set():
                entered.set()
                release.wait(0.3)
            return original_write(descriptor, payload)
        with patch('backend.video.source.os.write', slow_write):
            task = asyncio.create_task(self.store.import_stream('x', chunks(b'abc'), 2000))
            await asyncio.sleep(0.05)
            try:
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done(), 'disk write blocked the event loop')
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done(), 'cancel must wait before closing active disk descriptor')
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_binding_is_atomic_persistent_and_source_specific(self):
        source = await self.store.import_stream('same.mp4', chunks(b'one'), 2000)
        timeline = self.store.bind_csv(source.source_id, CSV)
        with self.assertRaises(ValueError):
            self.store.bind_csv(source.source_id, b'invalid')
        self.assertEqual(self.store.bound_csv(source.source_id), timeline)
        other = VideoSourceStore(self.root)
        self.addCleanup(other.close)
        self.assertEqual(other.bound_csv(source.source_id), timeline)
        second = await self.store.import_stream('same.mp4', chunks(b'two'), 2000)
        self.assertIsNone(self.store.bound_csv(second.source_id))
        self.store.media_path(source.source_id).write_bytes(b'two')
        with self.assertRaises(ValueError):
            self.store.bound_csv(source.source_id)

    async def test_invalid_ids_and_hardlinks_are_rejected(self):
        for identity in ('../outside', 'a/b', 'C:\\foo', '', 'a' * 100):
            with self.assertRaises(ValueError):
                self.store.get(identity)
        source = await self.store.import_stream('video', chunks(b'abc'), 2000)
        media = self.store.media_path(source.source_id)
        os.link(media, Path(self.tmp.name) / 'external')
        with self.assertRaises(OSError):
            self.store.media_path(source.source_id)

    async def test_saved_binding_cannot_be_grafted_to_different_content(self):
        first = await self.store.import_stream('x', chunks(b'one'), 2000)
        second = await self.store.import_stream('x', chunks(b'two'), 2000)
        self.store.bind_csv(first.source_id, CSV)
        first_record = json.loads((self.root / (first.source_id + '.json')).read_text())
        destination = self.root / (second.source_id + '.json')
        second_record = json.loads(destination.read_text())
        second_record['csv'] = first_record['csv']
        destination.write_text(json.dumps(second_record))
        with self.assertRaises(ValueError):
            self.store.bound_csv(second.source_id)

    async def test_tampered_display_metadata_is_rejected(self):
        source = await self.store.import_stream('x', chunks(b'one'), 2000)
        destination = self.root / (source.source_id + '.json')
        record = json.loads(destination.read_text())
        record['source']['filename'] = '../secret'
        destination.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.store.get(source.source_id)

    async def test_resolved_plan_is_persisted_atomically_with_content_identities(self):
        source = await self.store.import_stream('video.mp4', chunks(b'one'), 2000)
        timeline = self.store.bind_csv(source.source_id, CSV)
        plan = resolve_video_plan(timeline, allowed=('wave-a',), library_sha256='a' * 64, seed=42)
        plan_id = self.store.save_plan(source.source_id, plan)
        path = self.root / (plan_id + '.plan.json')
        saved = json.loads(path.read_text())
        self.assertEqual(saved['source']['sha256'], hashlib.sha256(b'one').hexdigest())
        self.assertEqual(saved['source']['source_id'], source.source_id)
        self.assertEqual(saved['csv_sha256'], timeline.sha256)
        self.assertEqual((saved['seed'], saved['library_sha256']), (42, 'a' * 64))
        self.assertEqual([(b['start_ms'], b['end_ms'], b['a_pattern'], b['a_target'], b['b_pattern'])
                         for b in saved['blocks']], [(0, 1000, 'wave-a', 20, None)])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save_plan(source.source_id, replace(plan, timeline_sha256='b' * 64))
        with self.assertRaises(ValueError):
            self.store.save_plan(source.source_id, replace(plan, blocks=()))
        with patch.object(self.store._files, '_write_atomically', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.store.save_plan(source.source_id, plan)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(self.root.glob('*.plan.json'))), 1)

    async def test_redirected_root_is_rejected(self):
        target = Path(self.tmp.name) / 'target'
        target.mkdir()
        link = Path(self.tmp.name) / 'link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(OSError):
            VideoSourceStore(link)
