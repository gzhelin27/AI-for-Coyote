"""Strict JSON codec for the shared immutable StoryMap domain model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import StoryChapter, StoryMap, StoryScene


_ROOT_KEYS = frozenset(("schema_version", "source_hash", "text_length", "chapters"))
_CHAPTER_KEYS = frozenset(
    ("id", "index", "start_offset", "end_offset", "title", "summary", "scenes")
)
_SCENE_KEYS = frozenset(
    ("id", "index", "start_offset", "end_offset", "summary", "pace")
)


class StoryMapCodecError(ValueError):
    """A scenes document cannot be proven to be an exact StoryMap."""


def encode_story_map(
    story_map: StoryMap, *, schema_version: int
) -> dict[str, object]:
    """Encode a validated StoryMap using the one exact archive schema."""

    if not isinstance(story_map, StoryMap):
        raise TypeError("story_map must be a StoryMap")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("schema_version must be an integer")
    return {
        "schema_version": schema_version,
        "source_hash": story_map.source_hash,
        "text_length": story_map.text_length,
        "chapters": [
            {
                "id": chapter.id,
                "index": chapter.index,
                "start_offset": chapter.start_offset,
                "end_offset": chapter.end_offset,
                "title": chapter.title,
                "summary": chapter.summary,
                "scenes": [
                    {
                        "id": scene.id,
                        "index": scene.index,
                        "start_offset": scene.start_offset,
                        "end_offset": scene.end_offset,
                        "summary": scene.summary,
                        "pace": scene.pace,
                    }
                    for scene in chapter.scenes
                ],
            }
            for chapter in story_map.chapters
        ],
    }


def decode_story_map(
    document: object, *, schema_version: int
) -> StoryMap:
    """Decode exact-key JSON and re-run every shared StoryMap invariant."""

    try:
        root = _exact_mapping(document, _ROOT_KEYS, "story map")
        if (
            isinstance(root["schema_version"], bool)
            or root["schema_version"] != schema_version
        ):
            raise StoryMapCodecError("story map schema version is invalid")
        chapter_values = _non_empty_list(root["chapters"], "story map chapters")
        chapters: list[StoryChapter] = []
        for chapter_value in chapter_values:
            chapter = _exact_mapping(
                chapter_value, _CHAPTER_KEYS, "story map chapter"
            )
            scene_values = _non_empty_list(
                chapter["scenes"], "story map chapter scenes"
            )
            scenes = tuple(
                _decode_scene(scene_value) for scene_value in scene_values
            )
            chapters.append(
                StoryChapter(
                    id=chapter["id"],
                    index=chapter["index"],
                    start_offset=chapter["start_offset"],
                    end_offset=chapter["end_offset"],
                    title=chapter["title"],
                    summary=chapter["summary"],
                    scenes=scenes,
                )
            )
        return StoryMap(
            source_hash=root["source_hash"],
            text_length=root["text_length"],
            chapters=tuple(chapters),
        )
    except StoryMapCodecError:
        raise
    except (KeyError, OverflowError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise StoryMapCodecError("story map does not satisfy model invariants") from exc


def _decode_scene(value: object) -> StoryScene:
    scene = _exact_mapping(value, _SCENE_KEYS, "story map scene")
    return StoryScene(
        id=scene["id"],
        index=scene["index"],
        start_offset=scene["start_offset"],
        end_offset=scene["end_offset"],
        summary=scene["summary"],
        pace=scene["pace"],
    )


def _exact_mapping(
    value: object, expected_keys: frozenset[str], name: str
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise StoryMapCodecError(f"{name} fields are invalid")
    return value


def _non_empty_list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise StoryMapCodecError(f"{name} must be a non-empty array")
    return value
