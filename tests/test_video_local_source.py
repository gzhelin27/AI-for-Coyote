"""Browser-local registrations persist metadata, never media bytes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.video.source import VideoSourceStore
from backend.video.waveforms import resolve_video_plan


CSV = b'start_time,end_time,A_target,B_target\n0:00:00,0:00:01,20,0\n'


class VideoLocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'registrations'
        self.store = VideoSourceStore(self.root, max_bytes=4)
        self.addCleanup(self.store.close)

    def test_ten_gib_registration_is_small_and_survives_restart_without_media(self):
        source = self.store.register_local('C:\\never-read\\big.mp4', 10 * 1024**3, 2000, 1234)
        self.assertEqual(source.filename, 'big.mp4')
        self.assertEqual(source.size, 10 * 1024**3)
        self.assertEqual(source.metadata, {'source_kind': 'browser_local',
            'identity_origin': 'registration_metadata', 'duration_origin': 'browser', 'last_modified': 1234})
        files = list(self.root.iterdir())
        self.assertEqual([path.name for path in files], [source.source_id + '.json'])
        self.assertLess(files[0].stat().st_size, 4096)
        self.assertEqual(json.loads(files[0].read_text())['version'], 2)
        other = VideoSourceStore(self.root, max_bytes=1)
        self.addCleanup(other.close)
        self.assertEqual(other.get(source.source_id), source)
        with self.assertRaisesRegex(ValueError, 'browser.local'):
            other.media_path(source.source_id)

    def test_same_file_registration_has_independent_identity_and_csv(self):
        first = self.store.register_local('x.mp4', 100, 2000, 0)
        second = self.store.register_local('x.mp4', 100, 2000, 0)
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertNotEqual(first.sha256, second.sha256)
        timeline = self.store.bind_csv(first.source_id, CSV)
        self.assertEqual(self.store.bound_csv(first.source_id), timeline)
        self.assertIsNone(self.store.bound_csv(second.source_id))
        plan = resolve_video_plan(timeline, allowed=('wave',), library_sha256='a' * 64, seed=42)
        plan_id = self.store.save_plan(first.source_id, plan)
        record = json.loads((self.root / (plan_id + '.plan.json')).read_text())
        self.assertEqual(record['source']['metadata']['identity_origin'], 'registration_metadata')
        self.assertEqual(record['source']['sha256'], first.sha256)
        self.assertEqual(list(self.root.glob('*.media')), [])
        path = self.root / (second.source_id + '.json')
        second_record = json.loads(path.read_text())
        second_record['csv'] = json.loads((self.root / (first.source_id + '.json')).read_text())['csv']
        path.write_text(json.dumps(second_record))
        with self.assertRaises(ValueError):
            self.store.bound_csv(second.source_id)

    def test_registration_rejects_invalid_numeric_metadata_without_files(self):
        for size in (0, -1, True, 1.5, '100', 2**53):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.store.register_local('x', size, 2000, 0)
        for modified in (-1, True, 1.5, '0', 2**53):
            with self.subTest(modified=modified), self.assertRaises(ValueError):
                self.store.register_local('x', 100, 2000, modified)
        for duration in (0, -1, True, 1.5, float('inf')):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.store.register_local('x', 100, duration, 0)
        self.assertEqual(list(self.root.iterdir()), [])
        source = self.store.register_local('x', 2**53 - 1, 2000, 2**53 - 1)
        self.assertEqual(self.store.get(source.source_id), source)

    def test_tampered_registration_fields_are_rejected(self):
        source = self.store.register_local('x.mp4', 100, 2000, 0)
        path = self.root / (source.source_id + '.json')
        original = json.loads(path.read_text())
        mutations = [
            lambda d: d['source'].update(filename='changed.mp4'),
            lambda d: d['source'].update(size=101),
            lambda d: d['source'].update(duration_ms=2001),
            lambda d: d['source'].update(sha256='0' * 64),
            lambda d: d['source']['metadata'].update(last_modified=1),
            lambda d: d['source']['metadata'].update(last_modified=True),
            lambda d: d['source']['metadata'].update(identity_origin='content'),
            lambda d: d['source']['metadata'].update(extra='unexpected'),
            lambda d: d.update(version=True),
            lambda d: d.update(extra='unexpected'),
        ]
        for mutate in mutations:
            document = copy.deepcopy(original)
            mutate(document)
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                self.store.get(source.source_id)
        path.write_text(json.dumps(original))
        self.assertEqual(self.store.get(source.source_id), source)
