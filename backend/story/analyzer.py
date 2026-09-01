"""One-time story analysis with validated context-limit fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import threading
from typing import Any
import weakref

from backend.llm import ContextLimitError, StoryAnalysisError

from .models import AnalysisKey, ImportedStory, StoryChapter, StoryMap, StoryScene


_WHOLE_BOOK_PROMPT = """\
Analyze the supplied normalized novel into a faithful source-ordered scene map.
Return one JSON object with a nonempty `chapters` array. Every chapter has integer
`start`/`end` offsets, string `title`, nonempty `summary`, and a nonempty `scenes`
array. Every scene has integer `start`/`end`, nonempty `summary`, and finite positive
numeric `pace`. Offsets are zero-based Python string offsets into the supplied text.
Chapters must exactly partition [0, text length), and scenes must exactly partition
their chapter. Do not omit headings, whitespace, or separators from the partitions.
"""

_CHUNK_PROMPT = """\
Analyze the supplied normalized novel chunk into faithful source-ordered scenes.
Return one JSON object with a nonempty `scenes` array. Every scene has integer
`start`/`end` offsets, nonempty `summary`, and finite positive numeric `pace`.
Offsets are local, zero-based Python string offsets into this exact chunk. Scenes
must exactly partition [0, chunk length), including headings, whitespace, and
separators. Do not return global offsets.
"""

_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?(?P<title>(?:"
    r"第[ \t]*[〇零一二三四五六七八九十百千万两0-9]+[ \t]*(?:章|回|节|卷|篇)"
    r"|chapter[ \t]+(?:[0-9]+|[ivxlcdm]+)"
    r")[^\n]*)$"
)
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")
_SINGLE_FLIGHT_GUARD = threading.Lock()
_SINGLE_FLIGHTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, AnalysisKey], asyncio.Task[StoryMap]],
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _Chunk:
    start: int
    end: int
    title: str


@dataclass(frozen=True, slots=True)
class _GeneratedScene:
    start: int
    end: int
    summary: str
    pace: float


@dataclass(frozen=True, slots=True)
class _GeneratedChapter:
    start: int
    end: int
    title: str
    summary: str
    scenes: tuple[_GeneratedScene, ...]


class StoryAnalyzer:
    """Analyze an imported story once, then reuse only a validated complete map."""

    def __init__(
        self,
        client: Any,
        store: Any,
        prompt_version: str,
        dlc_version: str,
        max_chunk_chars: int,
    ) -> None:
        model = _analysis_identity(getattr(client, "model", None), "analysis model")
        prompt_version = _analysis_identity(prompt_version, "analysis prompt version")
        dlc_version = _analysis_identity(dlc_version, "DLC version")
        if (
            isinstance(max_chunk_chars, bool)
            or not isinstance(max_chunk_chars, int)
            or max_chunk_chars <= 0
        ):
            raise ValueError("max_chunk_chars must be a positive integer")
        self.client = client
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.dlc_version = dlc_version
        self.max_chunk_chars = max_chunk_chars

    async def analyze(self, story: ImportedStory) -> StoryMap:
        if not isinstance(story, ImportedStory):
            raise TypeError("story must be an ImportedStory")
        if not story.text.strip():
            raise StoryAnalysisError("story text is empty")

        key = AnalysisKey(
            source_hash=story.source_sha256,
            model=self.model,
            prompt_version=self.prompt_version,
            dlc_version=self.dlc_version,
        )

        cached = self.store.load(key)
        if cached is not None and cached.text_length == len(story.text):
            return cached

        loop = asyncio.get_running_loop()
        flight_key = (_resolved_store_entry(self.store, key), key)
        with _SINGLE_FLIGHT_GUARD:
            flights = _SINGLE_FLIGHTS.setdefault(loop, {})
            owner = flights.get(flight_key)
            if owner is None or owner.done():
                owner = loop.create_task(
                    self._analyze_owner(story, key),
                    name=f"story-analysis-{key.digest()}",
                )
                flights[flight_key] = owner
                owner.add_done_callback(
                    lambda task, owner_loop=loop, owner_key=flight_key: _finish_flight(
                        owner_loop,
                        owner_key,
                        task,
                    )
                )
        return await asyncio.shield(owner)

    async def _analyze_owner(
        self,
        story: ImportedStory,
        key: AnalysisKey,
    ) -> StoryMap:
        cached = self.store.load(key)
        if cached is not None and cached.text_length == len(story.text):
            return cached

        try:
            response = await self.client.complete_json(
                _versioned_prompt(_WHOLE_BOOK_PROMPT, self.prompt_version),
                story.text,
                "story_map",
            )
        except ContextLimitError:
            generated = await self._analyze_fallback(story.text)
        except StoryAnalysisError:
            raise
        except Exception as exc:
            raise StoryAnalysisError("structured story analysis failed") from exc
        else:
            generated = _parse_generated_chapters(response, len(story.text))

        story_map = _build_story_map(
            source_hash=story.source_sha256,
            text_length=len(story.text),
            generated=generated,
        )
        self.store.save(key, story_map)
        return story_map

    async def _analyze_fallback(self, text: str) -> tuple[_GeneratedChapter, ...]:
        chunks = _heading_chunks(text)
        if not chunks:
            chunks = _bounded_paragraph_chunks(text, self.max_chunk_chars)

        merged: list[_GeneratedChapter] = []
        for chunk in chunks:
            content = text[chunk.start : chunk.end]
            try:
                response = await self.client.complete_json(
                    _versioned_prompt(_CHUNK_PROMPT, self.prompt_version),
                    content,
                    "story_chunk",
                )
            except StoryAnalysisError:
                raise
            except Exception as exc:
                raise StoryAnalysisError("structured story chunk analysis failed") from exc
            local_chapters = _parse_generated_chapters(
                response,
                len(content),
                title_hint=chunk.title,
            )
            for chapter in local_chapters:
                merged.append(_shift_chapter(chapter, chunk.start))
        return tuple(merged)


def _analysis_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not (identity := value.strip()):
        raise ValueError(f"{name} must be a nonempty string")
    return identity


def _versioned_prompt(prompt: str, prompt_version: str) -> str:
    return f"{prompt}\nAnalysis prompt version: {prompt_version}\n"


def _resolved_store_entry(store: Any, key: AnalysisKey) -> str:
    try:
        cache_path = Path(store.cache_path(key)).resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        raise StoryAnalysisError("analysis cache identity is unavailable") from exc
    return os.path.normcase(str(cache_path))


def _finish_flight(
    loop: asyncio.AbstractEventLoop,
    flight_key: tuple[str, AnalysisKey],
    task: asyncio.Task[StoryMap],
) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    with _SINGLE_FLIGHT_GUARD:
        flights = _SINGLE_FLIGHTS.get(loop)
        if flights is None or flights.get(flight_key) is not task:
            return
        del flights[flight_key]
        if not flights:
            del _SINGLE_FLIGHTS[loop]


def _heading_chunks(text: str) -> tuple[_Chunk, ...]:
    matches = tuple(_HEADING_RE.finditer(text))
    if not matches:
        return ()
    chunks: list[_Chunk] = []
    for index, match in enumerate(matches):
        start = 0 if index == 0 else match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunks.append(_Chunk(start=start, end=end, title=match.group("title").strip()))
    return tuple(chunks)


def _bounded_paragraph_chunks(text: str, max_chars: int) -> tuple[_Chunk, ...]:
    raw_chunks: list[_Chunk] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            boundaries = tuple(_PARAGRAPH_BREAK_RE.finditer(text, start, hard_end))
            if boundaries:
                candidate = boundaries[-1].end()
                if candidate > start and text[start:candidate].strip():
                    end = candidate
        raw_chunks.append(_Chunk(start=start, end=end, title=""))
        start = end
    return _attach_whitespace_chunks(text, raw_chunks)


def _attach_whitespace_chunks(text: str, raw_chunks: list[_Chunk]) -> tuple[_Chunk, ...]:
    chunks: list[_Chunk] = []
    pending_start: int | None = None
    for chunk in raw_chunks:
        if text[chunk.start : chunk.end].strip():
            start = pending_start if pending_start is not None else chunk.start
            chunks.append(_Chunk(start=start, end=chunk.end, title=""))
            pending_start = None
        elif chunks:
            previous = chunks[-1]
            chunks[-1] = _Chunk(
                start=previous.start,
                end=chunk.end,
                title=previous.title,
            )
        elif pending_start is None:
            pending_start = chunk.start
    return tuple(chunks)


def _parse_generated_chapters(
    response: object,
    text_length: int,
    *,
    title_hint: str = "",
) -> tuple[_GeneratedChapter, ...]:
    if not isinstance(response, dict):
        raise StoryAnalysisError("story analysis response must be an object")
    raw_chapters = response.get("chapters")
    if raw_chapters is None:
        raw_scenes = response.get("scenes")
        scenes = _parse_scenes(raw_scenes, 0, text_length)
        summary = _optional_summary(response.get("summary")) or _scene_summary(scenes)
        title = _optional_title(response.get("title")) or title_hint
        return (
            _GeneratedChapter(
                start=0,
                end=text_length,
                title=title,
                summary=summary,
                scenes=scenes,
            ),
        )
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise StoryAnalysisError("story chapters must be a nonempty array")

    chapters: list[_GeneratedChapter] = []
    previous_end = 0
    for raw_chapter in raw_chapters:
        if not isinstance(raw_chapter, dict):
            raise StoryAnalysisError("story chapter must be an object")
        start = _offset(raw_chapter.get("start"), "chapter start")
        end = _offset(raw_chapter.get("end"), "chapter end")
        if start != previous_end or start >= end or end > text_length:
            raise StoryAnalysisError("story chapters must exactly partition their text")
        scenes = _parse_scenes(raw_chapter.get("scenes"), start, end)
        title = _optional_title(raw_chapter.get("title"))
        if not title and len(raw_chapters) == 1:
            title = title_hint
        summary = _required_summary(raw_chapter.get("summary"), "chapter summary")
        chapters.append(
            _GeneratedChapter(
                start=start,
                end=end,
                title=title,
                summary=summary,
                scenes=scenes,
            )
        )
        previous_end = end
    if previous_end != text_length:
        raise StoryAnalysisError("story chapters must exactly partition their text")
    return tuple(chapters)


def _parse_scenes(raw_scenes: object, chapter_start: int, chapter_end: int) -> tuple[_GeneratedScene, ...]:
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise StoryAnalysisError("story scenes must be a nonempty array")
    scenes: list[_GeneratedScene] = []
    previous_end = chapter_start
    for raw_scene in raw_scenes:
        if not isinstance(raw_scene, dict):
            raise StoryAnalysisError("story scene must be an object")
        start = _offset(raw_scene.get("start"), "scene start")
        end = _offset(raw_scene.get("end"), "scene end")
        if start != previous_end or start >= end or end > chapter_end:
            raise StoryAnalysisError("story scenes must exactly partition their chapter")
        summary = _required_summary(raw_scene.get("summary"), "scene summary")
        pace = raw_scene.get("pace")
        if isinstance(pace, bool) or not isinstance(pace, (int, float)):
            raise StoryAnalysisError("scene pace must be a finite positive number")
        try:
            normalized_pace = float(pace)
        except (OverflowError, TypeError, ValueError) as exc:
            raise StoryAnalysisError("scene pace must be a finite positive number") from exc
        if not math.isfinite(normalized_pace) or normalized_pace <= 0:
            raise StoryAnalysisError("scene pace must be a finite positive number")
        scenes.append(
            _GeneratedScene(
                start=start,
                end=end,
                summary=summary,
                pace=normalized_pace,
            )
        )
        previous_end = end
    if previous_end != chapter_end:
        raise StoryAnalysisError("story scenes must exactly partition their chapter")
    return tuple(scenes)


def _offset(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StoryAnalysisError(f"{name} must be a nonnegative integer")
    return value


def _required_summary(value: object, name: str) -> str:
    if not isinstance(value, str) or not (summary := value.strip()):
        raise StoryAnalysisError(f"{name} must be nonempty")
    return summary


def _optional_summary(value: object) -> str:
    if value is None:
        return ""
    return _required_summary(value, "chapter summary")


def _optional_title(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StoryAnalysisError("chapter title must be a string")
    return value.strip()


def _scene_summary(scenes: tuple[_GeneratedScene, ...]) -> str:
    return "；".join(scene.summary for scene in scenes)


def _shift_chapter(chapter: _GeneratedChapter, offset: int) -> _GeneratedChapter:
    return _GeneratedChapter(
        start=chapter.start + offset,
        end=chapter.end + offset,
        title=chapter.title,
        summary=chapter.summary,
        scenes=tuple(
            _GeneratedScene(
                start=scene.start + offset,
                end=scene.end + offset,
                summary=scene.summary,
                pace=scene.pace,
            )
            for scene in chapter.scenes
        ),
    )


def _build_story_map(
    source_hash: str,
    text_length: int,
    generated: tuple[_GeneratedChapter, ...],
) -> StoryMap:
    chapters: list[StoryChapter] = []
    for chapter_index, chapter in enumerate(generated):
        scenes = tuple(
            StoryScene(
                id=StoryScene.stable_id(source_hash, chapter_index, scene_index),
                index=scene_index,
                start_offset=scene.start,
                end_offset=scene.end,
                summary=scene.summary,
                pace=scene.pace,
            )
            for scene_index, scene in enumerate(chapter.scenes)
        )
        chapters.append(
            StoryChapter(
                id=StoryChapter.stable_id(source_hash, chapter_index),
                index=chapter_index,
                start_offset=chapter.start,
                end_offset=chapter.end,
                title=chapter.title,
                summary=chapter.summary,
                scenes=scenes,
            )
        )
    try:
        return StoryMap(
            source_hash=source_hash,
            text_length=text_length,
            chapters=tuple(chapters),
        )
    except ValueError as exc:
        raise StoryAnalysisError("story analysis did not exactly partition the source") from exc
