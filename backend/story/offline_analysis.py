"""Fail-closed import of a Codex-produced faithful story-map candidate."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat

from .analysis_store import AnalysisStore
from .models import AnalysisKey, ImportedStory, StoryChapter, StoryMap, StoryScene
from .source import StorySourceError, StorySourceLoader


OFFLINE_PRODUCER = "codex-offline"
OFFLINE_ANALYSIS_VERSION = "faithful-offline-v1"
_MAX_CANDIDATE_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_MEMBERS = 10_000
_MAP_KEYS = frozenset(("source_hash", "text_length", "chapters"))
_CHAPTER_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "title", "summary", "scenes"))
_SCENE_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "summary", "pace"))


class OfflineAnalysisError(ValueError):
    """An offline candidate is malformed, unsafe, or does not match its source."""


@dataclass(frozen=True, slots=True)
class ValidatedOfflineAnalysis:
    key: AnalysisKey
    story_map: StoryMap
    chapter_count: int
    scene_count: int


def offline_analysis_key(story: ImportedStory, dlc_version: str) -> AnalysisKey:
    if not isinstance(story, ImportedStory):
        raise TypeError("story must be an ImportedStory")
    return AnalysisKey(
        source_hash=story.source_sha256,
        model=OFFLINE_PRODUCER,
        prompt_version=OFFLINE_ANALYSIS_VERSION,
        dlc_version=_required_identity(dlc_version, "DLC version"),
    )


class OfflineAnalysisImporter:
    """Load a real source, strictly validate a candidate, then atomically save it."""

    def __init__(
        self,
        source_loader: StorySourceLoader,
        store: AnalysisStore,
        *,
        candidate_directory: Path,
    ) -> None:
        if not isinstance(source_loader, StorySourceLoader):
            raise TypeError("source_loader must be a StorySourceLoader")
        if not isinstance(store, AnalysisStore):
            raise TypeError("store must be an AnalysisStore")
        self._source_loader = source_loader
        self._store = store
        self._candidate_directory = Path(candidate_directory)

    def validate(
        self,
        source_path: Path,
        candidate_path: Path,
        *,
        encoding: str = "auto",
        dlc_version: str,
    ) -> ValidatedOfflineAnalysis:
        story = self._load_source(Path(source_path), encoding)
        key = offline_analysis_key(story, dlc_version)
        candidate = self._read_candidate(Path(candidate_path))
        story_map = self._decode_candidate(candidate, story)
        return ValidatedOfflineAnalysis(
            key=key,
            story_map=story_map,
            chapter_count=len(story_map.chapters),
            scene_count=sum(len(chapter.scenes) for chapter in story_map.chapters),
        )

    def import_candidate(
        self,
        source_path: Path,
        candidate_path: Path,
        *,
        encoding: str = "auto",
        dlc_version: str,
    ) -> ValidatedOfflineAnalysis:
        result = self.validate(
            source_path, candidate_path, encoding=encoding, dlc_version=dlc_version
        )
        self._store.save(result.key, result.story_map)
        return result

    def _load_source(self, source_path: Path, encoding: str) -> ImportedStory:
        try:
            return self._source_loader.load(
                source_path.name, source_path.read_bytes(), encoding=encoding
            )
        except (OSError, StorySourceError, TypeError, ValueError) as exc:
            raise OfflineAnalysisError("story source could not be loaded") from exc

    def _read_candidate(self, candidate_path: Path) -> dict[str, object]:
        identity = self._candidate_identity(candidate_path)
        try:
            payload = candidate_path.read_bytes()
        except (OSError, UnicodeError, RecursionError) as exc:
            raise OfflineAnalysisError("candidate file could not be read") from exc
        if _regular_file_identity(candidate_path) != identity:
            raise OfflineAnalysisError("candidate file changed while reading")
        if len(payload) > _MAX_CANDIDATE_BYTES:
            raise OfflineAnalysisError("candidate file exceeds the size limit")
        try:
            decoded = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError, ValueError) as exc:
            raise OfflineAnalysisError("candidate JSON is invalid") from exc
        try:
            _validate_json_shape(decoded)
        except (UnicodeError, RecursionError, OverflowError, ValueError) as exc:
            raise OfflineAnalysisError("candidate JSON is invalid") from exc
        return _exact_object(decoded, _MAP_KEYS, "candidate")

    def _candidate_identity(self, candidate_path: Path) -> tuple[int, int]:
        try:
            candidate_path.relative_to(self._candidate_directory)
        except ValueError as exc:
            raise OfflineAnalysisError("candidate file is outside the configured directory") from exc
        if _contains_redirect(self._candidate_directory) or _contains_redirect(candidate_path):
            raise OfflineAnalysisError("candidate path contains a filesystem redirect")
        identity = _regular_file_identity(candidate_path)
        if identity is None:
            raise OfflineAnalysisError("candidate file is not a regular file")
        try:
            candidate_path.resolve(strict=True).relative_to(
                self._candidate_directory.resolve(strict=True)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise OfflineAnalysisError("candidate file is outside the configured directory") from exc
        return identity

    @staticmethod
    def _decode_candidate(candidate: dict[str, object], story: ImportedStory) -> StoryMap:
        source_hash = _required_string(candidate["source_hash"], "candidate source_hash")
        if source_hash != story.source_sha256:
            raise OfflineAnalysisError("candidate source_hash does not match source")
        text_length = _required_integer(candidate["text_length"], "candidate text_length")
        if text_length != len(story.text):
            raise OfflineAnalysisError("candidate text_length does not match source")
        chapters = _required_array(candidate["chapters"], "candidate chapters")
        try:
            decoded_chapters = tuple(
                OfflineAnalysisImporter._decode_chapter(raw_chapter, source_hash, chapter_index)
                for chapter_index, raw_chapter in enumerate(chapters)
            )
            return StoryMap(
                source_hash=source_hash,
                text_length=text_length,
                chapters=decoded_chapters,
            )
        except (TypeError, UnicodeError, RecursionError, OverflowError, ValueError, OfflineAnalysisError) as exc:
            if isinstance(exc, OfflineAnalysisError):
                raise
            raise OfflineAnalysisError("candidate story map is invalid") from exc

    @staticmethod
    def _decode_chapter(raw_chapter: object, source_hash: str, chapter_index: int) -> StoryChapter:
        chapter = _exact_object(raw_chapter, _CHAPTER_KEYS, f"chapter {chapter_index}")
        chapter_id = _required_string(chapter["id"], f"chapter {chapter_index} id")
        expected_id = StoryChapter.stable_id(source_hash, chapter_index)
        if chapter_id != expected_id:
            raise OfflineAnalysisError(f"chapter {chapter_index} id does not match source")
        scenes = _required_array(chapter["scenes"], f"chapter {chapter_index} scenes")
        return StoryChapter(
            id=chapter_id,
            index=_required_integer(chapter["index"], f"chapter {chapter_index} index"),
            start_offset=_required_integer(chapter["start_offset"], f"chapter {chapter_index} start_offset"),
            end_offset=_required_integer(chapter["end_offset"], f"chapter {chapter_index} end_offset"),
            title=_required_string_or_empty(chapter["title"], f"chapter {chapter_index} title"),
            summary=_required_string(chapter["summary"], f"chapter {chapter_index} summary"),
            scenes=tuple(
                OfflineAnalysisImporter._decode_scene(raw_scene, source_hash, chapter_index, scene_index)
                for scene_index, raw_scene in enumerate(scenes)
            ),
        )

    @staticmethod
    def _decode_scene(raw_scene: object, source_hash: str, chapter_index: int, scene_index: int) -> StoryScene:
        scene = _exact_object(raw_scene, _SCENE_KEYS, f"chapter {chapter_index} scene {scene_index}")
        scene_id = _required_string(scene["id"], f"chapter {chapter_index} scene {scene_index} id")
        expected_id = StoryScene.stable_id(source_hash, chapter_index, scene_index)
        if scene_id != expected_id:
            raise OfflineAnalysisError(f"chapter {chapter_index} scene {scene_index} id does not match source")
        return StoryScene(
            id=scene_id,
            index=_required_integer(scene["index"], f"chapter {chapter_index} scene {scene_index} index"),
            start_offset=_required_integer(scene["start_offset"], f"chapter {chapter_index} scene {scene_index} start_offset"),
            end_offset=_required_integer(scene["end_offset"], f"chapter {chapter_index} scene {scene_index} end_offset"),
            summary=_required_string(scene["summary"], f"chapter {chapter_index} scene {scene_index} summary"),
            pace=_required_pace(scene["pace"], f"chapter {chapter_index} scene {scene_index} pace"),
        )


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("candidate JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"candidate JSON constant {value} is not permitted")


def _validate_json_shape(root: object) -> None:
    pending: list[tuple[object, int]] = [(root, 1)]
    members = 0
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise OfflineAnalysisError("candidate JSON exceeds the nesting limit")
        if isinstance(current, dict):
            members += len(current)
            for key, value in current.items():
                key.encode("utf-8", "strict")
                pending.append((value, depth + 1))
        elif isinstance(current, list):
            members += len(current)
            pending.extend((value, depth + 1) for value in current)
        elif isinstance(current, str):
            current.encode("utf-8", "strict")
        if members > _MAX_JSON_MEMBERS:
            raise OfflineAnalysisError("candidate JSON exceeds the member limit")


def _exact_object(value: object, expected_keys: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise OfflineAnalysisError(f"{name} fields are invalid")
    return value


def _required_array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise OfflineAnalysisError(f"{name} must be an array")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineAnalysisError(f"{name} must be a non-empty string")
    return value


def _required_string_or_empty(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise OfflineAnalysisError(f"{name} must be a string")
    return value


def _required_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineAnalysisError(f"{name} must be a non-negative integer")
    return value


def _required_pace(value: object, name: str) -> float | int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise OfflineAnalysisError(f"{name} must be a finite number from 0.25 to 4.0")
    if isinstance(value, float) and not math.isfinite(value):
        raise OfflineAnalysisError(f"{name} must be a finite number from 0.25 to 4.0")
    if value < 0.25 or value > 4.0:
        raise OfflineAnalysisError(f"{name} must be from 0.25 to 4.0")
    return value


def _required_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not (identity := value.strip()):
        raise OfflineAnalysisError(f"{name} must be a non-empty string")
    return identity


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    if _is_redirect(path):
        return None
    try:
        details = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(details.st_mode):
        return None
    return details.st_dev, details.st_ino


def _contains_redirect(path: Path) -> bool:
    return any(_is_redirect(candidate) for candidate in (path.absolute(), *path.absolute().parents))


def _is_redirect(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        details = os.lstat(path)
    except OSError:
        return False
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_point)
