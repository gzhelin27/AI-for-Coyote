"""Immutable, resolved video timeline values."""
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoInterval:
    row_id: str
    start_ms: int
    end_ms: int
    a_target: int
    b_target: int


@dataclass(frozen=True)
class VideoCsv:
    intervals: tuple[VideoInterval, ...]
    sha256: str
    duration_ms: int


@dataclass(frozen=True)
class VideoBlock:
    row_id: str
    index: int
    start_ms: int
    end_ms: int
    a_pattern: str | None
    b_pattern: str | None
    a_target: int
    b_target: int


@dataclass(frozen=True)
class VideoPlan:
    timeline_sha256: str
    seed: int
    library_sha256: str
    blocks: tuple[VideoBlock, ...]
