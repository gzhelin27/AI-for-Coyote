"""Story/reader state adapter over the authoritative timeline session owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from backend.timeline.models import SCHEMA_VERSION, SessionStatus
from backend.timeline.replay_store import ReplaySummary
from backend.timeline.session import (
    PlannedSessionArchive,
    PreparedSessionFinish,
    SessionController,
)

from .models import ImportedStory, StoryChapter, StoryMap
from .planner import ValidatedChapterPlan
from .story_map_codec import encode_story_map


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
        self._session_id: str | None = None

    @property
    def session_controller(self) -> SessionController:
        return self._session

    @property
    def plan(self) -> ValidatedChapterPlan | None:
        self._reconcile_with_session()
        return self._plan

    async def start(
        self,
        plan: ValidatedChapterPlan,
        story: ImportedStory,
        story_map: StoryMap,
        *,
        source_encoding: str | None = None,
        dlc_version: str | None = None,
    ) -> NovelSessionState:
        async with self._lock:
            try:
                self._reconcile_with_session()
                if self._phase is not NovelSessionStatus.IDLE:
                    raise NovelSessionError("a novel session is already active")
                if self._session.to_state().status is not SessionStatus.IDLE:
                    raise NovelSessionError("the timeline session owner is already active")
                self._phase = NovelSessionStatus.PLANNING
                try:
                    chapter = self._validate_start(plan, story, story_map)
                    archive_encoding = (
                        self._source_encoding
                        if source_encoding is None
                        else source_encoding
                    )
                    if archive_encoding not in _SOURCE_ENCODINGS:
                        raise NovelSessionError(
                            "source encoding must be auto, utf-8, or gb18030"
                        )
                    archive_dlc_version = (
                        self._dlc_version if dlc_version is None else dlc_version
                    )
                    if (
                        not isinstance(archive_dlc_version, str)
                        or not archive_dlc_version.strip()
                    ):
                        raise NovelSessionError(
                            "DLC version must be a non-empty string"
                        )
                    archive = PlannedSessionArchive(
                        scenes=encode_story_map(
                            story_map, schema_version=SCHEMA_VERSION
                        ),
                        source=story.original_bytes,
                        source_extension=story.extension.removeprefix(".").lower(),
                        metadata={
                            "analysis_version": self._analysis_version,
                            "chapter_id": plan.chapter_id,
                            "content_type": "novel",
                            "dlc_version": archive_dlc_version.strip(),
                            "source_encoding": archive_encoding,
                            "source_text_hash": story.source_sha256,
                            "speed": plan.speed,
                        },
                    )
                except Exception as exc:
                    if isinstance(exc, NovelSessionError):
                        raise
                    raise NovelSessionError("novel chapter validation failed") from exc

                self._story = story
                self._story_map = story_map
                self._chapter = chapter
                self._plan = plan
                self._phase = NovelSessionStatus.VALIDATED
                await self._session.start_planned(
                    plot_events=plan.plot_events,
                    chapter_duration_ms=plan.chapter_duration_ms,
                    seed=plan.seed,
                    cycle_gap_policy=plan.timeline_request.cycle_gap_policy,
                    archive=archive,
                )
            finally:
                self._reconcile_with_session()
            return self.to_state()

    async def pause(self) -> NovelSessionState:
        async with self._lock:
            try:
                self._reconcile_with_session()
                if self._phase not in (
                    NovelSessionStatus.RUNNING,
                    NovelSessionStatus.FINISHING,
                ):
                    if self._phase is NovelSessionStatus.PAUSED:
                        return self.to_state()
                    raise NovelSessionError("no running novel session to pause")
                self._phase = NovelSessionStatus.FINISHING
                await self._session.pause()
            finally:
                self._reconcile_with_session()
            return self.to_state()

    async def resume(
        self,
        from_: Literal["current", "chapter_start", "beginning"],
    ) -> NovelSessionState:
        async with self._lock:
            try:
                self._reconcile_with_session()
                if from_ not in _RESUME_POSITIONS:
                    raise NovelSessionError(
                        "resume position must be current, chapter_start, or beginning"
                    )
                if self._phase is not NovelSessionStatus.PAUSED or self._plan is None:
                    raise NovelSessionError("no paused novel session to resume")
                cursor = self._session.to_state().cursor if from_ == "current" else 0
                self._phase = NovelSessionStatus.FINISHING
                await self._session.resume_planned(cursor)
            finally:
                self._reconcile_with_session()
            return self.to_state()

    async def prepare_finish(self) -> PreparedSessionFinish:
        async with self._lock:
            try:
                self._reconcile_with_session()
                if self._phase not in (
                    NovelSessionStatus.RUNNING,
                    NovelSessionStatus.PAUSED,
                    NovelSessionStatus.FINISHING,
                ):
                    raise NovelSessionError("no novel session to finish")
                self._phase = NovelSessionStatus.FINISHING
                prepared = await self._session.prepare_finish()
            finally:
                self._reconcile_with_session()
            return prepared

    async def persist_finish(
        self, prepared: PreparedSessionFinish
    ) -> ReplaySummary:
        try:
            return await self._session.persist_finish(prepared)
        finally:
            self._reconcile_with_session()

    async def finalize_finish(
        self, prepared: PreparedSessionFinish
    ) -> ReplaySummary:
        async with self._lock:
            try:
                return await self._session.finalize_finish(prepared)
            finally:
                self._reconcile_with_session()

    async def finish(self) -> ReplaySummary:
        prepared = await self.prepare_finish()

        async def persist_and_finalize() -> ReplaySummary:
            await self.persist_finish(prepared)
            return await self.finalize_finish(prepared)

        return await self._session._await_lifecycle_completion(
            persist_and_finalize(), name="novel-finish-lifecycle"
        )

    async def abort(self) -> NovelSessionState:
        async with self._lock:
            try:
                self._reconcile_with_session()
                if self._phase is NovelSessionStatus.IDLE:
                    return self.to_state()
                self._phase = NovelSessionStatus.FINISHING
                await self._session.stop()
            finally:
                self._reconcile_with_session()
            return self.to_state()

    async def on_disconnect(self) -> NovelSessionState:
        """Disconnect is an abnormal abort and never creates history."""

        async with self._lock:
            try:
                self._reconcile_with_session()
                if self._phase is NovelSessionStatus.IDLE:
                    return self.to_state()
                self._phase = NovelSessionStatus.FINISHING
                await self._session.on_disconnect()
            finally:
                self._reconcile_with_session()
            return self.to_state()

    def to_state(self) -> NovelSessionState:
        self._reconcile_with_session()
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

    def _reset(self) -> None:
        self._phase = NovelSessionStatus.IDLE
        self._story = None
        self._story_map = None
        self._chapter = None
        self._plan = None
        self._session_id = None

    def _reconcile_with_session(self) -> None:
        physical = self._session.to_state()
        if physical.status is SessionStatus.IDLE:
            self._reset()
            return
        if (
            physical.mode != "novel"
            or physical.session_id is None
            or self._plan is None
        ):
            self._reset()
            return
        if self._session_id is None:
            self._session_id = physical.session_id
        elif self._session_id != physical.session_id:
            self._reset()
            return
        phases = {
            SessionStatus.RUNNING: NovelSessionStatus.RUNNING,
            SessionStatus.PAUSED: NovelSessionStatus.PAUSED,
            SessionStatus.FINISHING: NovelSessionStatus.FINISHING,
        }
        phase = phases.get(physical.status)
        if phase is None:
            self._reset()
            return
        self._phase = phase
