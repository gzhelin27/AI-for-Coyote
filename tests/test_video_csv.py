import unittest
from backend.video.csv_timeline import parse_video_csv, find_interval

HEADER = b'start_time,end_time,A_target,B_target\n'


class VideoCsvTests(unittest.TestCase):
    def test_absolute_time_and_gaps(self):
        timeline = parse_video_csv(HEADER + b'0:10:20,0:11:30,20,15\n', duration_ms=720000)
        row = timeline.intervals[0]
        self.assertEqual((row.start_ms, row.end_ms), (620000, 690000))
        self.assertEqual(find_interval(timeline, 620000), row)
        for position in (-1, 0, 619999, 690000, 720000):
            self.assertIsNone(find_interval(timeline, position))

    def test_sorted_normalization_bom_and_adjacent_boundaries(self):
        first = parse_video_csv(HEADER + b'0:00:01,0:00:02,20,0\n0:00:00,0:00:01,0,200\n', duration_ms=3000)
        second = parse_video_csv(b'\xef\xbb\xbf' + HEADER + b' 00:00:00 ,0:00:01, 0 ,200\r\n0:00:01,0:00:02,20,0\n', duration_ms=3000)
        self.assertEqual(first, second)
        self.assertEqual(find_interval(first, 1000).a_target, 20)
        self.assertIsNone(find_interval(first, 2000))

    def test_large_hours(self):
        row = parse_video_csv(HEADER + b'25:00:00,25:00:01,0,0\n', duration_ms=90001000).intervals[0]
        self.assertEqual(row.start_ms, 90000000)

    def test_invalid_inputs(self):
        for row in (b'0:60:00,1:01:00,1,1', b'0:0:00,0:01:00,1,1',
                    b'0:00:00.1,0:01:00,1,1', b'-1:00:00,0:01:00,1,1',
                    b'0:00:00,0:00:00,1,1', b'0:00:02,0:00:01,1,1',
                    b'0:00:00,0:00:01,-1,1', b'0:00:00,0:00:01,1.0,1',
                    b'0:00:00,0:00:01,201,1', b'0:00:00,0:00:01,1',
                    b'0:00:00,0:00:01,1,1,extra', b'0:00:00,0:00:04,1,1',
                    b'0:00:00,0:00:02,1,1\n0:00:01,0:00:03,1,1',
                    b'0:00:00,0:00:01,1,1\n0:00:00,0:00:01,1,1'):
            with self.subTest(row=row), self.assertRaises(ValueError):
                parse_video_csv(HEADER + row, duration_ms=3000)
        for payload in (b'', b'bad', HEADER + b'\xff', HEADER + b'"unterminated'):
            with self.assertRaises(ValueError):
                parse_video_csv(payload, duration_ms=3000)
        for duration in (0, -1, True, 1.5, float('inf')):
            with self.assertRaises(ValueError):
                parse_video_csv(HEADER, duration_ms=duration)

    def test_resource_bounds(self):
        with self.assertRaises(ValueError):
            parse_video_csv(b' ' * (16 * 1024 * 1024 + 1), duration_ms=3000)
        with self.assertRaisesRegex(ValueError, 'rows'):
            parse_video_csv(HEADER + b'0:00:00,0:00:01,0,0\n' * 100001, duration_ms=3000)
