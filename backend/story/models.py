"""Immutable records shared by story import, analysis, and replay code."""

from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class ImportedStory:
    """A validated local source and its normalized text representation."""

    filename: str
    extension: str
    original_bytes: bytes
    text: str
    source_sha256: str


def _require_non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_index(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a zero-based index")
    return value


def _require_offset(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class StoryScene:
    """A source-ordered scene whose identifier is independent of analysis text."""

    id: str
    index: int
    start_offset: int
    end_offset: int
    summary: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.id, "scene id")
        _require_index(self.index, "scene index")
        start_offset = _require_offset(self.start_offset, "scene start offset")
        end_offset = _require_offset(self.end_offset, "scene end offset")
        if start_offset >= end_offset:
            raise ValueError("scene offsets must be increasing")
        if not isinstance(self.summary, str):
            raise ValueError("scene summary must be a string")

    @staticmethod
    def stable_id(source_hash: str, chapter_index: int, scene_index: int) -> str:
        """Return the source-indexed ID without consulting generated content."""

        _require_non_empty_text(source_hash, "source hash")
        _require_index(chapter_index, "chapter index")
        _require_index(scene_index, "scene index")
        return f"ch-{chapter_index + 1:04d}-sc-{scene_index + 1:04d}"


@dataclass(frozen=True, slots=True)
class StoryChapter:
    """A source-ordered chapter and its complete scene partition."""

    id: str
    index: int
    start_offset: int
    end_offset: int
    title: str
    summary: str
    scenes: tuple[StoryScene, ...]

    def __post_init__(self) -> None:
        _require_non_empty_text(self.id, "chapter id")
        _require_index(self.index, "chapter index")
        start_offset = _require_offset(self.start_offset, "chapter start offset")
        end_offset = _require_offset(self.end_offset, "chapter end offset")
        if start_offset >= end_offset:
            raise ValueError("chapter offsets must be increasing")
        if not isinstance(self.title, str):
            raise ValueError("chapter title must be a string")
        if not isinstance(self.summary, str):
            raise ValueError("chapter summary must be a string")
        if not isinstance(self.scenes, tuple) or not self.scenes:
            raise ValueError("chapter scenes must be a non-empty tuple")
        if not all(isinstance(scene, StoryScene) for scene in self.scenes):
            raise ValueError("chapter scenes must contain StoryScene values")

    @staticmethod
    def stable_id(source_hash: str, chapter_index: int) -> str:
        """Return the source-indexed ID without consulting generated content."""

        _require_non_empty_text(source_hash, "source hash")
        _require_index(chapter_index, "chapter index")
        return f"ch-{chapter_index + 1:04d}"


@dataclass(frozen=True, slots=True)
class StoryMap:
    """The complete, ordered scene map for one imported source hash."""

    source_hash: str
    chapters: tuple[StoryChapter, ...]

    def __post_init__(self) -> None:
        source_hash = _require_non_empty_text(self.source_hash, "source hash")
        if not isinstance(self.chapters, tuple) or not self.chapters:
            raise ValueError("story map chapters must be a non-empty tuple")
        if not all(isinstance(chapter, StoryChapter) for chapter in self.chapters):
            raise ValueError("story map chapters must contain StoryChapter values")

        previous_chapter_end = 0
        for chapter_index, chapter in enumerate(self.chapters):
            if chapter.index != chapter_index:
                raise ValueError("chapter indexes must be contiguous and zero-based")
            if chapter.id != StoryChapter.stable_id(source_hash, chapter_index):
                raise ValueError("chapter id does not match its source index")
            if chapter.start_offset < previous_chapter_end:
                raise ValueError("chapter offsets must be source-ordered")
            previous_scene_end = chapter.start_offset
            for scene_index, scene in enumerate(chapter.scenes):
                if scene.index != scene_index:
                    raise ValueError("scene indexes must be contiguous and zero-based")
                if scene.id != StoryScene.stable_id(
                    source_hash, chapter_index, scene_index
                ):
                    raise ValueError("scene id does not match its source index")
                if not (
                    chapter.start_offset <= scene.start_offset
                    and scene.end_offset <= chapter.end_offset
                ):
                    raise ValueError("scene offsets must remain within their chapter")
                if scene.start_offset < previous_scene_end:
                    raise ValueError("scene offsets must be source-ordered")
                previous_scene_end = scene.end_offset
            previous_chapter_end = chapter.end_offset


@dataclass(frozen=True, slots=True)
class AnalysisKey:
    """All inputs that make one faithful analysis cache entry reusable."""

    source_hash: str
    model: str
    prompt_version: str
    dlc_version: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.source_hash, "source hash")
        _require_non_empty_text(self.model, "analysis model")
        _require_non_empty_text(self.prompt_version, "analysis prompt version")
        _require_non_empty_text(self.dlc_version, "DLC version")

    def digest(self) -> str:
        """Return a canonical digest that changes for every analysis input."""

        payload = {
            "dlc_version": self.dlc_version,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "source_hash": self.source_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()
