"""Immutable records shared by story import, analysis, and replay code."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportedStory:
    """A validated local source and its normalized text representation."""

    filename: str
    extension: str
    original_bytes: bytes
    text: str
    source_sha256: str
