"""Resolve independent channel randomness once, before playback."""
from bisect import bisect_right
import hashlib
import json
import random
import re

from .models import VideoBlock, VideoCsv, VideoPlan

BLOCK_MS = 30000
MAX_BLOCKS = 100000


def resolve_video_plan(timeline: VideoCsv, *, allowed: tuple[str, ...],
                       library_sha256: str, seed: int) -> VideoPlan:
    if not isinstance(library_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', library_sha256):
        raise ValueError('Waveform library identity must be a SHA-256 digest')
    if type(seed) is not int:
        raise ValueError('Seed must be an integer')
    if not isinstance(allowed, tuple) or any(not isinstance(name, str) or not name for name in allowed):
        raise ValueError('Allowed presets must be a tuple of nonempty names')
    names = tuple(sorted(set(allowed)))
    if not names and any(row.a_target or row.b_target for row in timeline.intervals):
        raise ValueError('Positive output requires at least one allowed waveform')
    count = sum((row.end_ms - row.start_ms + BLOCK_MS - 1) // BLOCK_MS for row in timeline.intervals)
    if count > MAX_BLOCKS:
        raise ValueError('Video plan exceeds 100000 blocks')
    rngs = [random.Random(int.from_bytes(hashlib.sha256(
        json.dumps([seed, channel], separators=(',', ':')).encode('ascii')).digest(), 'big'))
        for channel in ('A', 'B')]
    previous = [None, None]
    blocks = []
    for row in timeline.intervals:
        for start in range(row.start_ms, row.end_ms, BLOCK_MS):
            patterns = []
            for channel, target in enumerate((row.a_target, row.b_target)):
                pattern = None
                if target:
                    candidates = tuple(name for name in names if name != previous[channel]) if len(names) > 1 else names
                    pattern = rngs[channel].choice(candidates)
                    previous[channel] = pattern
                patterns.append(pattern)
            blocks.append(VideoBlock(row.row_id, len(blocks), start, min(start + BLOCK_MS, row.end_ms),
                                     *patterns, row.a_target, row.b_target))
    return VideoPlan(timeline.sha256, seed, library_sha256.lower(), tuple(blocks))


def find_block(plan: VideoPlan, position_ms: int) -> VideoBlock | None:
    if type(position_ms) is not int or position_ms < 0:
        return None
    index = bisect_right(plan.blocks, position_ms, key=lambda block: block.start_ms) - 1
    if index >= 0 and position_ms < plan.blocks[index].end_ms:
        return plan.blocks[index]
    return None
