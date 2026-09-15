import unittest
from backend.video.csv_timeline import parse_video_csv
from backend.video.waveforms import resolve_video_plan, find_block
from tests.test_video_csv import HEADER


class VideoWaveformTests(unittest.TestCase):
    def plan(self, rows=b'0:10:20,0:11:30,20,15\n', allowed=('b', 'a', 'c'), seed=42, identity='a' * 64):
        return resolve_video_plan(parse_video_csv(HEADER + rows, duration_ms=4000000000),
                                  allowed=allowed, library_sha256=identity, seed=seed)

    def test_anchor_partition_lookup_and_repeat_exclusion(self):
        plan = self.plan()
        self.assertEqual([(b.start_ms, b.end_ms) for b in plan.blocks],
                         [(620000, 650000), (650000, 680000), (680000, 690000)])
        self.assertEqual(find_block(plan, 685000), plan.blocks[2])
        self.assertEqual(find_block(plan, 650000), plan.blocks[1])
        for position in (-1, 619999, 690000):
            self.assertIsNone(find_block(plan, position))
        for left, right in zip(plan.blocks, plan.blocks[1:]):
            self.assertNotEqual(left.a_pattern, right.a_pattern)
            self.assertNotEqual(left.b_pattern, right.b_pattern)

    def test_stability_channel_independence_library_identity(self):
        plan = self.plan()
        self.assertEqual(plan, self.plan(allowed=('c', 'a', 'b', 'a')))
        zero_a = self.plan(rows=b'0:10:20,0:11:30,0,15\n')
        self.assertEqual([b.b_pattern for b in plan.blocks], [b.b_pattern for b in zero_a.blocks])
        self.assertTrue(all(b.a_pattern is None for b in zero_a.blocks))
        self.assertNotEqual(plan.library_sha256, self.plan(identity='b' * 64).library_sha256)

    def test_zero_single_empty_and_row_anchors(self):
        plan = self.plan(rows=b'0:00:01,0:00:02,1,0\n0:00:03,0:00:35,1,0\n', allowed=('only',))
        self.assertEqual([(b.start_ms, b.end_ms) for b in plan.blocks], [(1000,2000),(3000,33000),(33000,35000)])
        self.assertTrue(all(b.a_pattern == 'only' and b.b_pattern is None for b in plan.blocks))
        self.assertIsNone(find_block(plan, 2000))
        self.assertEqual(self.plan(rows=b'0:00:00,0:00:01,0,0\n', allowed=()).blocks[0].a_pattern, None)
        with self.assertRaises(ValueError):
            self.plan(allowed=())

    def test_invalid_metadata_and_block_limit(self):
        for identity in ('bad', 'g' * 64):
            with self.assertRaises(ValueError):
                self.plan(identity=identity)
        with self.assertRaises(ValueError):
            self.plan(seed=True)
        with self.assertRaisesRegex(ValueError, 'blocks'):
            self.plan(rows=b'0:00:00,1000:00:00,0,0\n')
