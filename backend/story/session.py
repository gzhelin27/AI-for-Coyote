"""Story/reader state adapter over the authoritative timeline session owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from backend.timeline.models import SCHEMA_VERSION, SessionStatus
from backend.timeline.replay_store import ReplaySummary
from backend.timeline.session import PlannedSessionArchive, SessionController

from .models import ImportedStory, StoryChapter, StoryMap
from .planner import ValidatedChapterPlan


_SOURCE_ENCODINGS = ("auto", "utf-8", "gb18030")
_SOURCE_EXTENSIONS = ("txt", "md", "docx")
_RESUME_POSITIONS = ("current", "chapter_start", "beginning")


class NovelSessionError(RuntimeError):
    """A novel session transition was rejected before safe playback."""


class NovelSessionStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    VALIDATED = "validated"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHING = "finishing"


@dataclass(frozen=True, slots=True)
class NovelSessionState:
    """Immutable reader-facing state with no physical output ownership."""

    status: NovelSessionStatus
    source_hash: str | None = None
    filename: str | None = None
    chapter_id: str | None = None
    speed: str | None = None
    cursor: int = 0
    current_scene_id: str | None = None
    reader_start_offset: int | None = None
    reader_end_offset: int | None = None
    progress: float = 0.0


class NovelSessionController:
    """Own story/reader/plan state and delegate all output lifecycle work."""

    def __init__(
        self,
        session_controller: SessionController,
        *,
        source_encoding: str,
        analysis_version: str,
        dlc_version: str,
    ) -> None:
        if not isinstance(session_controller, SessionController):
            raise TypeError("session_controller must be a SessionController")
        if source_encoding not in _SOURCE_ENCODINGS:
            raise ValueError("source_encoding must be auto, utf-8, or gb18030")
        for value, name in (
            (analysis_version, "analysis_version"),
            (dlc_version, "dlc_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        self._session = session_controller
        self._source_encoding = source_encoding
        self._analysis_version = analysis_version.strip()
        self._dlc_version = dlc_version.strip()
        self._lock = asyncio.Lock()
        self._phase = NovelSessionStatus.IDLE
        self._story: ImportedStory | None = None
        self._story_map: StoryMap | None = None
        self._chapter: StoryChapter | None = None
        self._plan: ValidatedChapterPlan | None = None

    @property
    def session_controller(self) -> SessionController:
        return self._session

    @property
    def plan(self) -> ValidatedChapterPlan | None:
        return self._plan

    async def start(
        self,
        plan: ValidatedChapterPlan,
        story: ImportedStory,
        story_map: StoryMap,
    ) -> NovelSessionState:
        async with self._lock:
            self._synchronize_with_session()
            if self._phase is not NovelSessionStatus.IDLE:
                raise NovelSessionError("a novel session is already active")
            if self._session.to_state().status is not SessionStatus.IDLE:
                raise NovelSessionError("the timeline session owner is already active")
            self._phase = NovelSessionStatus.PLANNING
            try:
                chapter = self._validate_start(plan, story, story_map)
                archive = PlannedSessionArchive(
                    scenes=self._story_map_document(story_map),
                    source=story.original_bytes,
                    source_extension=story.extension.removeprefix(".").lower(),
                    metadata={
                        "analysis_version": self._analysis_version,
                        "chapter_id": plan.chapter_id,
                        "content_type": "novel",
                        "dlc_version": self._dlc_version,
                        "source_encoding": self._source_encoding,
                        "source_text_hash": story.source_sha256,
                        "speed": plan.speed,
                    },
                )
            except Exception as exc:
                self._reset()
                if isinstance(exc, NovelSessionError):
                    raise
                raise NovelSessionError("novel chapter validation failed") from exc

            self._story = story
            self._story_map = story_map
            self._chapter = chapter
            self._plan = plan
            self._phase = NovelSessionStatus.VALIDATED
            try:
                await self._session.start_planned(
                    plot_events=plan.plot_events,
                    chapter_duration_ms=plan.chapter_duration_ms,
                    seed=plan.seed,
                    cycle_gap_policy=plan.timeline_request.cycle_gap_policy,
                    archive=archive,
                )
            except BaseException:
                if self._session.to_state().status is not SessionStatus.IDLE:
                    await self._session.stop()
                self._reset()
                raise
            self._phase = NovelSessionStatus.RUNNING
            return self.to_state()

    async def pause(self) -> NovelSessionState:
        async with self._lock:
            self._synchronize_with_session()
            if self._phase not in (
                NovelSessionStatus.RUNNING,
                NovelSessionStatus.FINISHING,
            ):
                if self._phase is NovelSessionStatus.PAUSED:
                    return self.to_state()
                raise NovelSessionError("no running novel session to pause")
            self._phase = NovelSessionStatus.FINISHING
            await self._session.pause()
            self._phase = NovelSessionStatus.PAUSED
            return self.to_state()

    async def resume(
        self,
        from_: Literal["current", "chapter_start", "beginning"],
    ) -> NovelSessionState:
        if from_ not in _RESUME_POSITIONS:
            raise NovelSessionError(
                "resume position must be current, chapter_start, or beginning"
            )
        async with self._lock:
            self._synchronize_with_session()
            if self._phase is not NovelSessionStatus.PAUSED or self._plan is None:
                raise NovelSessionError("no paused novel session to resume")
            cursor = self._session.to_state().cursor if from_ == "current" else 0
            self._phase = NovelSessionStatus.FINISHING
            await self._session.resume_planned(cursor)
            self._phase = NovelSessionStatus.RUNNING
            return self.to_state()

    async def finish(self) -> ReplaySummary:
        async with self._lock:
            self._synchronize_with_session()
            if self._phase not in (
                NovelSessionStatus.RUNNING,
                NovelSessionStatus.PAUSED,
                NovelSessionStatus.FINISHING,
            ):
                raise NovelSessionError("no novel session to finish")
            self._phase = NovelSessionStatus.FINISHING
            try:
                summary = await self._session.finish()
            except BaseException:
                if self._session.to_state().status is SessionStatus.PAUSED:
                    self._phase = NovelSessionStatus.PAUSED
                raise
            self._reset()
            return summary

    async def abort(self) -> NovelSessionState:
        async with self._lock:
            self._synchronize_with_session()
            if self._phase is NovelSessionStatus.IDLE:
                return self.to_state()
            self._phase = NovelSessionStatus.FINISHING
            await self._session.stop()
            self._reset()
            return self.to_state()

    async def on_disconnect(self) -> NovelSessionState:
        """Disconnect is an abnormal abort and never creates history."""

        return await self.abort()

    def to_state(self) -> NovelSessionState:
        self._synchronize_with_session()
        story = self._story
        chapter = self._chapter
        plan = self._plan
        if story is None or chapter is None or plan is None:
            return NovelSessionState(status=self._phase)

        physical = self._session.to_state()
        cursor = min(max(physical.cursor, 0), len(plan.plot_events) - 1)
        current_scene_id = physical.current_event_id
        event = next(
            (
                item
                for item in plan.plot_events
                if item.event_id == current_scene_id
            ),
            None,
        )
        if event is not None:
            current_scene_id = event.scene_id
        scene = next(
            (
                item
                for item in chapter.scenes
                if item.id == current_scene_id
            ),
            None,
        )
        progress = 0.0
        if event is not None:
            progress = (cursor + 1) / len(plan.plot_events)
        return NovelSessionState(
            status=self._phase,
            source_hash=story.source_sha256,
            filename=story.filename,
            chapter_id=chapter.id,
            speed=plan.speed,
            cursor=cursor,
            current_scene_id=current_scene_id,
            reader_start_offset=(scene.start_offset if scene is not None else None),
            reader_end_offset=(scene.end_offset if scene is not None else None),
            progress=progress,
        )

    @staticmethod
    def _validate_start(
        plan: object,
        story: object,
        story_map: object,
    ) -> StoryChapter:
        if not isinstance(plan, ValidatedChapterPlan):
            raise NovelSessionError("plan must be a ValidatedChapterPlan")
        if not isinstance(story, ImportedStory):
            raise NovelSessionError("story must be an ImportedStory")
        if not isinstance(story_map, StoryMap):
            raise NovelSessionError("story_map must be a StoryMap")
        extension = story.extension.removeprefix(".").lower()
        if extension not in _SOURCE_EXTENSIONS:
            raise NovelSessionError("story source extension is not supported")
        if not isinstance(story.original_bytes, bytes):
            raise NovelSessionError("story original source must be bytes")
        if story.source_sha256 != story_map.source_hash:
            raise NovelSessionError("story does not match the story map")
        if len(story.text) != story_map.text_length:
            raise NovelSessionError("story length does not match the story map")
        if plan.source_hash != story.source_sha256:
            raise NovelSessionError("chapter plan does not match the story")
        chapter = next(
            (item for item in story_map.chapters if item.id == plan.chapter_id),
            None,
        )
        if chapter is None:
            raise NovelSessionError("chapter plan does not name a mapped chapter")
        if tuple(event.scene_id for event in plan.plot_events) != tuple(
            scene.id for scene in chapter.scenes
        ):
            raise NovelSessionError("chapter plan does not cover mapped scenes exactly")
        offsets = tuple(event.offset_ms for event in plan.plot_events)
        if (
            not offsets
            or offsets[0] != 0
            or any(current <= previous for previous, current in zip(offsets, offsets[1:]))
            or offsets[-1] >= plan.chapter_duration_ms
        ):
            raise NovelSessionError("chapter plan offsets are invalid")
        return chapter

    @staticmethod
    def _story_map_document(story_map: StoryMap) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
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

    def _reset(self) -> None:
        self._phase = NovelSessionStatus.IDLE
        self._story = None
        self._story_map = None
        self._chapter = None
        self._plan = None

    def _synchronize_with_session(self) -> None:
        if self._phase not in (
            NovelSessionStatus.RUNNING,
            NovelSessionStatus.PAUSED,
        ):
            return
        status = self._session.to_state().status
        if status is SessionStatus.IDLE:
            self._reset()
        elif status is SessionStatus.PAUSED:
            self._phase = NovelSessionStatus.PAUSED
        elif status is SessionStatus.FINISHING:
            self._phase = NovelSessionStatus.FINISHING
        elif status is SessionStatus.RUNNING:
            self._phase = NovelSessionStatus.RUNNING
