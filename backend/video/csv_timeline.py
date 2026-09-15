"""Bounded, atomic CSV validation and half-open interval queries."""
from bisect import bisect_right
import csv
import hashlib
import io
import json
import re

from .models import VideoCsv, VideoInterval

MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_CSV_ROWS = 100000
HEADER = ['start_time', 'end_time', 'A_target', 'B_target']


def _time_ms(value: str) -> int:
    if not re.fullmatch(r'[0-9]+:[0-5][0-9]:[0-5][0-9]', value):
        raise ValueError('Time must use H:MM:SS with minutes and seconds below 60')
    hours, minutes, seconds = map(int, value.split(':'))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def _target(value: str) -> int:
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('Targets must be integers from 0 to 200')
    target = int(value)
    if target > 200:
        raise ValueError('Targets must be integers from 0 to 200')
    return target


def parse_video_csv(payload: bytes, *, duration_ms: int) -> VideoCsv:
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError('Video duration must be a positive integer in milliseconds')
    if not isinstance(payload, bytes) or len(payload) > MAX_CSV_BYTES:
        raise ValueError('CSV must be bytes and at most 16 MiB')
    try:
        reader = csv.reader(io.StringIO(payload.decode('utf-8-sig'), newline=''), strict=True)
        if next(reader, None) != HEADER:
            raise ValueError('CSV header must be start_time,end_time,A_target,B_target')
        rows = []
        for values in reader:
            if len(rows) >= MAX_CSV_ROWS:
                raise ValueError('CSV exceeds 100000 rows')
            if len(values) != 4:
                raise ValueError('Each CSV row must contain exactly four columns')
            start, end, a, b = (value.strip() for value in values)
            row = (_time_ms(start), _time_ms(end), _target(a), _target(b))
            if not 0 <= row[0] < row[1] <= duration_ms:
                raise ValueError('Interval must have start < end within video duration')
            rows.append(row)
    except (UnicodeError, csv.Error) as error:
        raise ValueError('CSV must be valid UTF-8 and well-formed CSV') from error
    rows.sort()
    if any(left[1] > right[0] for left, right in zip(rows, rows[1:])):
        raise ValueError('CSV intervals must not overlap or repeat')
    canonical = json.dumps(rows, separators=(',', ':')).encode('ascii')
    return VideoCsv(tuple(VideoInterval(str(index), *row) for index, row in enumerate(rows)),
                    hashlib.sha256(canonical).hexdigest(), duration_ms)


def find_interval(timeline: VideoCsv, position_ms: int) -> VideoInterval | None:
    if type(position_ms) is not int or not 0 <= position_ms < timeline.duration_ms:
        return None
    index = bisect_right(timeline.intervals, position_ms, key=lambda row: row.start_ms) - 1
    if index >= 0 and position_ms < timeline.intervals[index].end_ms:
        return timeline.intervals[index]
    return None
