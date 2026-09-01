"""Atomic, schema-versioned local storage for faithful story analysis."""

import json
import os
from pathlib import Path
import secrets
from typing import Any

from .models import AnalysisKey, StoryChapter, StoryMap, StoryScene


_SCHEMA_VERSION = 1
_DOCUMENT_KEYS = frozenset(("schema_version", "analysis_key", "story_map"))
_KEY_KEYS = frozenset(("source_hash", "model", "prompt_version", "dlc_version"))
_MAP_KEYS = frozenset(("source_hash", "chapters"))
_CHAPTER_KEYS = frozenset(
    ("id", "index", "start_offset", "end_offset", "title", "summary", "scenes")
)
_SCENE_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "summary"))


class _CacheValidationError(ValueError):
    """A local cache document cannot be trusted as a StoryMap."""


class AnalysisStore:
    """Store source-hash analysis entries beneath one local cache directory.

    Version one is deliberately fail-closed: a document must have exactly the
    documented fields and schema version. Future readers must add an explicit
    migration before accepting a newer schema or preserving unknown fields.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def cache_path(self, key: AnalysisKey) -> Path:
        """Return the only cache path associated with ``key``."""

        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        return self._safe_child(f"{key.digest()}.json")

    def load(self, key: AnalysisKey) -> StoryMap | None:
        """Return a validated cache hit, or quarantine corrupt local JSON."""

        cache_path = self.cache_path(key)
        try:
            if not cache_path.is_file():
                return None
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            return self._decode_document(document, key)
        except (json.JSONDecodeError, UnicodeDecodeError, _CacheValidationError):
            self._quarantine(cache_path)
            return None
        except OSError:
            return None

    def save(self, key: AnalysisKey, story_map: StoryMap) -> None:
        """Durably replace the complete cache entry after validating its source."""

        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        if not isinstance(story_map, StoryMap):
            raise TypeError("story_map must be a StoryMap")
        if story_map.source_hash != key.source_hash:
            raise ValueError("story map source hash does not match the analysis key")

        self._directory.mkdir(parents=True, exist_ok=True)
        target = self.cache_path(key)
        document = self._encode_document(key, story_map)
        temporary_path = self._safe_child(f".{target.name}.{secrets.token_hex(16)}.tmp")
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(target)
            self._fsync_directory()
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _safe_child(self, filename: str) -> Path:
        """Build a generated, containment-checked filename under this store."""

        if Path(filename).name != filename:
            raise ValueError("cache filename must not contain path components")
        directory = self._directory.resolve(strict=False)
        candidate = (directory / filename).resolve(strict=False)
        if candidate.parent != directory:
            raise ValueError("cache path escapes the analysis directory")
        return candidate

    def _quarantine(self, cache_path: Path) -> None:
        """Move only this generated cache file to a unique sibling invalid file."""

        if cache_path.parent != self._directory.resolve(strict=False):
            return
        if cache_path.suffix != ".json":
            return
        for _ in range(32):
            invalid_path = self._safe_child(
                f"{cache_path.name}.{secrets.token_hex(16)}.invalid"
            )
            if invalid_path.exists():
                continue
            try:
                cache_path.rename(invalid_path)
                return
            except FileNotFoundError:
                return
            except FileExistsError:
                continue
            except OSError:
                return

    def _fsync_directory(self) -> None:
        """Persist the replacement metadata where the platform permits it."""

        if os.name == "nt":
            return
        try:
            descriptor = os.open(self._directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _encode_document(key: AnalysisKey, story_map: StoryMap) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "analysis_key": {
                "source_hash": key.source_hash,
                "model": key.model,
                "prompt_version": key.prompt_version,
                "dlc_version": key.dlc_version,
            },
            "story_map": {
                "source_hash": story_map.source_hash,
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
                            }
                            for scene in chapter.scenes
                        ],
                    }
                    for chapter in story_map.chapters
                ],
            },
        }

    def _decode_document(self, document: object, expected_key: AnalysisKey) -> StoryMap:
        raw_document = _exact_object(document, _DOCUMENT_KEYS, "cache document")
        schema_version = raw_document["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != _SCHEMA_VERSION
        ):
            raise _CacheValidationError("cache schema version is unsupported")
        raw_key = _exact_object(raw_document["analysis_key"], _KEY_KEYS, "analysis key")
        key = AnalysisKey(
            source_hash=_required_string(raw_key["source_hash"], "key source hash"),
            model=_required_string(raw_key["model"], "key model"),
            prompt_version=_required_string(
                raw_key["prompt_version"], "key prompt version"
            ),
            dlc_version=_required_string(raw_key["dlc_version"], "key DLC version"),
        )
        if key != expected_key:
            raise _CacheValidationError("cache key does not match request")

        raw_map = _exact_object(raw_document["story_map"], _MAP_KEYS, "story map")
        source_hash = _required_string(raw_map["source_hash"], "story map source hash")
        if source_hash != expected_key.source_hash:
            raise _CacheValidationError("story map source hash does not match request")
        raw_chapters = _required_array(raw_map["chapters"], "story map chapters")
        chapters = tuple(self._decode_chapter(raw_chapter) for raw_chapter in raw_chapters)
        try:
            return StoryMap(source_hash=source_hash, chapters=chapters)
        except ValueError as exc:
            raise _CacheValidationError("story map is invalid") from exc

    @staticmethod
    def _decode_chapter(raw_chapter: object) -> StoryChapter:
        chapter = _exact_object(raw_chapter, _CHAPTER_KEYS, "chapter")
        raw_scenes = _required_array(chapter["scenes"], "chapter scenes")
        scenes = tuple(AnalysisStore._decode_scene(raw_scene) for raw_scene in raw_scenes)
        try:
            return StoryChapter(
                id=_required_string(chapter["id"], "chapter id"),
                index=_required_integer(chapter["index"], "chapter index"),
                start_offset=_required_integer(
                    chapter["start_offset"], "chapter start offset"
                ),
                end_offset=_required_integer(chapter["end_offset"], "chapter end offset"),
                title=_required_string_or_empty(chapter["title"], "chapter title"),
                summary=_required_string_or_empty(
                    chapter["summary"], "chapter summary"
                ),
                scenes=scenes,
            )
        except ValueError as exc:
            raise _CacheValidationError("chapter is invalid") from exc

    @staticmethod
    def _decode_scene(raw_scene: object) -> StoryScene:
        scene = _exact_object(raw_scene, _SCENE_KEYS, "scene")
        try:
            return StoryScene(
                id=_required_string(scene["id"], "scene id"),
                index=_required_integer(scene["index"], "scene index"),
                start_offset=_required_integer(scene["start_offset"], "scene start offset"),
                end_offset=_required_integer(scene["end_offset"], "scene end offset"),
                summary=_required_string_or_empty(scene["summary"], "scene summary"),
            )
        except ValueError as exc:
            raise _CacheValidationError("scene is invalid") from exc


def _exact_object(value: object, expected_keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise _CacheValidationError(f"{name} fields are invalid")
    return value


def _required_array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise _CacheValidationError(f"{name} must be an array")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _CacheValidationError(f"{name} must be a non-empty string")
    return value


def _required_string_or_empty(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _CacheValidationError(f"{name} must be a string")
    return value


def _required_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _CacheValidationError(f"{name} must be an integer")
    return value
