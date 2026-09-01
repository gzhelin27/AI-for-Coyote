# -*- coding: utf-8 -*-
"""AI 郊狼驯服师 —— 主程序入口（FastAPI）。

启动后：
- 后台连接 dglab-websocket-server v4 中继；
- Web 页面：聊天、手动控制、实时状态、急停、配对二维码、日志；
- 所有设备命令统一走 safety -> device_ops -> relay 链路。
"""
import asyncio
import contextlib
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
import io
import json
import os
import re
import secrets
import socket
import sys
import urllib.parse
import zipfile
from pathlib import Path

import httpx
import qrcode
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from .audio import AudioManager
from .camera import Camera
from .config import (
    load_config,
    patch_character_add_role,
    patch_character_prompt_file,
    reload_character,
    save_character_runtime,
    save_device_channels,
)
from .game_loop import GameLoop
from .llm import LLM
from .logging_utils import setup_logging
from .provenance import dlc_provenance, runtime_content_fingerprint
from .relay_client import RelayClient
from .safety import DeviceOutputError, SafetyManager
from .story import (
    OFFLINE_ANALYSIS_VERSION,
    AnalysisLookup,
    AnalysisStore,
    ImportedStory,
    NovelSessionController,
    NovelSessionError,
    NovelSessionState,
    StoryMap,
    StorySourceError,
    StorySourceLoader,
    offline_analysis_key,
)
from .story.planner import ChapterPlanError, ChapterPlanner
from .story.source_store import (
    PinnedStorySourceStore,
    StoredStorySource,
    StorySourceStorageError,
)
from .timeline.models import CycleGapPolicy
from .timeline.replay_store import ReplayStore, ReplayStoreError, ReplaySummary
from .timeline.session import SessionController

# 打包（PyInstaller）后以 exe 所在目录为项目根；开发时以仓库根
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
_READER_SLICE_MAX_CHARS = 8192
_WINDOWS_REPARSE_POINT = 0x400


@dataclass(slots=True)
class _StorySourceRecord:
    """One server-owned source mapping; its storage path is never serialized."""

    source_id: str
    story: ImportedStory
    encoding: str
    storage_path: Path


@dataclass(frozen=True, slots=True)
class _StoryRuntimeSignature:
    client_identity: tuple[int, int]
    model: str
    prompt_version: str
    dlc_version: str
    waveforms: tuple[str, ...]
    caps: tuple[tuple[str, int], ...]


@dataclass(eq=False, slots=True)
class _StoryRuntimeOwner:
    kind: str


@dataclass(frozen=True, slots=True)
class _StoryPlanningContext:
    generation: int
    source_id: str
    chapter_id: str
    speed: object
    planner: ChapterPlanner
    story_map: StoryMap
    runtime_signature: _StoryRuntimeSignature
    runtime_owner: _StoryRuntimeOwner


class _StoryConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _path_is_redirect(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _story_data_directory(value: object, name: str) -> Path:
    """Resolve one configured repository-local data directory without redirects."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a configured local directory")
    configured = Path(value)
    if configured.is_absolute():
        raise ValueError(f"{name} must be repository-local")
    root = PROJECT_ROOT.resolve()
    candidate = (PROJECT_ROOT / configured).absolute()
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{name} must remain beneath the project root") from exc
    relative = candidate.relative_to(PROJECT_ROOT.absolute())
    current = PROJECT_ROOT.absolute()
    for component in relative.parts:
        current = current / component
        if current.exists() and _path_is_redirect(current):
            raise ValueError(f"{name} contains a filesystem redirect")
    return candidate


def _release_version() -> str:
    """Return only a safe release label suitable for public display."""
    f = PROJECT_ROOT / "version.txt"
    if f.exists():
        v = f.read_text(encoding="utf-8").strip().lstrip("\ufeff")
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", v)
            and not re.fullmatch(r"[0-9a-fA-F]{40,64}", v)
        ):
            return v
    return ""


def _bundled_runtime_content_fingerprint() -> str:
    """Read the build-generated content identity embedded beside this module."""
    resource_path = Path(__file__).with_name("runtime_fingerprint.json")
    try:
        payload = json.loads(resource_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("bundled runtime fingerprint is unavailable") from exc
    fingerprint = payload.get("content_fingerprint") if isinstance(payload, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(
        r"source-sha256:[0-9a-f]{64}", fingerprint
    ):
        raise RuntimeError("bundled runtime fingerprint is invalid")
    return fingerprint


def _runtime_content_fingerprint() -> str:
    """Return loose-source identity or the embedded onefile build identity."""
    if getattr(sys, "frozen", False):
        return _bundled_runtime_content_fingerprint()
    return runtime_content_fingerprint(PROJECT_ROOT)


def _app_version() -> str:
    """Return release identity plus the complete runtime content fingerprint."""
    content_fingerprint = _runtime_content_fingerprint()
    release = _release_version()
    return f"{release}+{content_fingerprint}" if release else content_fingerprint


def _public_app_version() -> str:
    """Keep internal content hashes out of browser-visible application state."""
    return _release_version() or "development"


def _dlc_provenance(
    cfg: dict, *, waveform_policy: str | None = None
) -> str:
    """Compatibility delegate retaining the historical public digest contract."""

    return dlc_provenance(
        cfg, project_root=PROJECT_ROOT, waveform_policy=waveform_policy
    )


def get_lan_ip() -> str:
    """探测本机局域网 IP（用于配对二维码）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def get_local_ips() -> list[str]:
    """列出本机所有可用 IPv4（含自动探测结果），供网络自检。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except OSError:
        pass
    ips.add(get_lan_ip())
    return sorted(ips)


def build_pair_url(cfg, lan_ip: str, controller_id: str) -> str:
    """生成 DG-LAB 4 APP Socket V4 配对链接（局域网或公网中继）。"""
    public_url = str(cfg["relay"].get("public_url") or "").strip()
    base = public_url if public_url else f"ws://{lan_ip}:9998"
    return "https://dungeon-lab.cn/s/?v=1&action=socket&url=" + urllib.parse.quote(
        f"{base}?tid={controller_id}", safe=""
    )


class _ReplayNotFoundError(Exception):
    pass


def _session_payload(controller: SessionController) -> dict:
    return controller.to_state().to_dict()


def _replay_summary_payload(summary: ReplaySummary) -> dict:
    """Serialize the public replay summary without reflecting future internals."""
    return {
        "replay_id": summary.replay_id,
        "session_id": summary.session_id,
        "status": summary.status.value,
        "mode": summary.mode,
        "created_at": summary.created_at,
        "completed_at": summary.completed_at,
        "adjusted": summary.adjusted,
        "model": summary.model,
        "dlc_role": summary.dlc_role,
        "dlc_profile": summary.dlc_profile,
        "title": summary.title,
        "cycle_count": summary.cycle_count,
    }


def _story_source_payload(record: _StorySourceRecord) -> dict[str, object]:
    story = record.story
    return {
        "source_id": record.source_id,
        "filename": story.filename,
        "extension": story.extension,
        "encoding": record.encoding,
        "hash_prefix": story.source_sha256[:12],
        "text_length": len(story.text),
    }


def _story_analysis_payload(
    state: "AppState", record: _StorySourceRecord, status: str
) -> dict[str, object]:
    return {
        "status": status,
        "hash_prefix": record.story.source_sha256[:12],
        "analysis_version": OFFLINE_ANALYSIS_VERSION,
        "dlc_version": state._current_story_dlc_version(),
    }


def _story_chapters_payload(story_map: StoryMap) -> list[dict[str, object]]:
    return [
        {
            "chapter_id": chapter.id,
            "index": chapter.index,
            "title": chapter.title,
            "summary": chapter.summary,
            "start_offset": chapter.start_offset,
            "end_offset": chapter.end_offset,
            "scene_count": len(chapter.scenes),
        }
        for chapter in story_map.chapters
    ]


def _novel_session_payload(session: NovelSessionState) -> dict[str, object]:
    return {
        "status": session.status.value,
        "hash_prefix": (
            session.source_hash[:12] if session.source_hash is not None else None
        ),
        "filename": session.filename,
        "chapter_id": session.chapter_id,
        "speed": session.speed,
        "cursor": session.cursor,
        "current_scene_id": session.current_scene_id,
        "reader_start_offset": session.reader_start_offset,
        "reader_end_offset": session.reader_end_offset,
        "progress": session.progress,
    }


def _idle_novel_session_payload() -> dict[str, object]:
    return {
        "status": "idle",
        "hash_prefix": None,
        "filename": None,
        "chapter_id": None,
        "speed": None,
        "cursor": 0,
        "current_scene_id": None,
        "reader_start_offset": None,
        "reader_end_offset": None,
        "progress": 0.0,
    }


def _story_record(state: "AppState", source_id: str) -> _StorySourceRecord | None:
    sources = getattr(state, "story_sources", None)
    if not isinstance(sources, dict):
        return None
    record = sources.get(source_id)
    return record if isinstance(record, _StorySourceRecord) else None


def _active_story_record(state: "AppState") -> _StorySourceRecord | None:
    source_id = getattr(state, "active_story_source_id", None)
    return _story_record(state, source_id) if isinstance(source_id, str) else None


def _inspect_story_analysis(
    state: "AppState", record: _StorySourceRecord
) -> AnalysisLookup:
    key = offline_analysis_key(record.story, state._current_story_dlc_version())
    return state.story_analysis_store.inspect(key)


def _story_full_state(state: "AppState") -> dict[str, object]:
    record = _active_story_record(state)
    novel = getattr(state, "novel_session", None)
    session_payload = (
        _novel_session_payload(novel.to_state())
        if isinstance(novel, NovelSessionController)
        else _idle_novel_session_payload()
    )
    planning = getattr(state, "story_planning_context", None)
    if (
        record is not None
        and isinstance(planning, _StoryPlanningContext)
        and getattr(state, "story_planning_task", None) is not None
        and planning.source_id == record.source_id
    ):
        session_payload = {
            **_idle_novel_session_payload(),
            "status": "planning",
            "hash_prefix": record.story.source_sha256[:12],
            "filename": record.story.filename,
            "chapter_id": planning.chapter_id,
            "speed": planning.speed,
        }
    if record is None:
        return {
            "selected_source": None,
            "analysis": None,
            "chapters": [],
            "session": session_payload,
        }
    lookup = _inspect_story_analysis(state, record)
    chapters = (
        _story_chapters_payload(lookup.story_map)
        if lookup.status == "ready" and lookup.story_map is not None
        else []
    )
    return {
        "selected_source": _story_source_payload(record),
        "analysis": _story_analysis_payload(state, record, lookup.status),
        "chapters": chapters,
        "session": session_payload,
    }


def _story_not_found_response() -> JSONResponse:
    return JSONResponse(
        {"code": "story_not_found", "error": "story source not found"},
        status_code=404,
    )


def _analysis_error_response(
    state: "AppState", record: _StorySourceRecord, lookup: AnalysisLookup
) -> JSONResponse:
    code = "analysis_invalid" if lookup.status == "invalid" else "analysis_missing"
    status_code = 422 if lookup.status == "invalid" else 409
    return JSONResponse(
        {
            "code": code,
            **_story_analysis_payload(state, record, lookup.status),
        },
        status_code=status_code,
    )


def _story_transition_error(state: "AppState", exc: Exception) -> JSONResponse:
    if isinstance(exc, _StoryConflict):
        return JSONResponse(
            {"code": exc.code, "error": exc.message}, status_code=409
        )
    if isinstance(exc, ChapterPlanError):
        return JSONResponse(
            {"code": "chapter_plan_failed", "error": "chapter planning failed"},
            status_code=422,
        )
    if isinstance(exc, DeviceOutputError):
        return JSONResponse(
            {"code": "story_output_failed", "error": "device output was not confirmed"},
            status_code=exc.status_code,
        )
    if isinstance(exc, NovelSessionError):
        return JSONResponse(
            {
                "code": "story_transition_invalid",
                "error": "story transition is not available",
            },
            status_code=409,
        )
    state.logger.exception("unexpected story transition failure", exc_info=exc)
    return JSONResponse(
        {"code": "story_transition_failed", "error": "story transition failed"},
        status_code=500,
    )


def _guard_story_runtime_change(state: "AppState"):
    """Reserve the story runtime while a hot identity mutation is in flight."""

    def decorate(endpoint):
        @wraps(endpoint)
        async def guarded(*args, **kwargs):
            owner: _StoryRuntimeOwner | None = None
            async with state.timeline_transition_lock:
                planning = state.story_planning_task is not None
                if state._story_runtime_is_busy():
                    conflict = (
                        _StoryConflict(
                            "story_planning_active",
                            "a story chapter is already being planned",
                        )
                        if planning
                        else _StoryConflict(
                            "story_runtime_busy", "story runtime is already active"
                        )
                    )
                    return _story_transition_error(state, conflict)
                owner = state._acquire_story_runtime_owner("dlc")
            try:
                return await endpoint(*args, **kwargs)
            finally:
                async with state.timeline_transition_lock:
                    state._release_story_runtime_owner(owner)

        return guarded

    return decorate


def _timeline_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, _ReplayNotFoundError):
        return JSONResponse({"error": "replay not found"}, status_code=404)
    if isinstance(exc, DeviceOutputError):
        return JSONResponse(
            {"error": "device output was not confirmed"},
            status_code=exc.status_code,
        )
    if isinstance(exc, RuntimeError):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, (ReplayStoreError, TypeError, ValueError)):
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": "timeline request failed"}, status_code=500)


def _validated_replay_archive(store: ReplayStore, replay_id: str) -> bytes:
    """Read one contained archive through its validated opened handle."""
    store._validate_replay_id(replay_id)
    archive_path = store._path(replay_id)
    try:
        resolved = archive_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _ReplayNotFoundError from exc
    root = store.root.resolve()
    if resolved.parent != root:
        raise ReplayStoreError("replay archive path is unsafe")
    return store.read_validated(replay_id)


class AppState:
    """共享运行对象 + Web 广播。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.logger = setup_logging(cfg["log_dir"], cfg["log"]["level"])
        self.timeline_transition_lock = asyncio.Lock()
        self.broadcast_lock = asyncio.Lock()
        self.state_revision = 0

        self.safety = SafetyManager(cfg)
        self.relay = RelayClient(
            cfg["relay"]["url"],
            reconnect_delay_s=float(cfg["relay"]["reconnect_delay_s"]),
            on_event=self.on_relay_event,
            on_action=self.on_relay_action,
        )
        self.llm = LLM(cfg)
        self.camera = Camera(cfg)
        self.audio = AudioManager(
            cfg, on_text=self.on_audio_text, on_moan=self.on_audio_moan
        )
        self.loop = GameLoop(cfg, self.llm, self.safety, self.relay, self.camera, self.audio)
        timeline_cfg = cfg["timeline"]
        replay_root = Path(str(timeline_cfg["replay_dir"]))
        if not replay_root.is_absolute():
            replay_root = PROJECT_ROOT / replay_root
        self.replay_store = ReplayStore(replay_root)
        self.timeline_session = SessionController(
            game_loop=self.loop,
            store=self.replay_store,
            seed_factory=lambda: secrets.randbits(63),
            strength_jitter=int(timeline_cfg["strength_jitter"]),
            cycle_gap_policy=CycleGapPolicy.from_dict(timeline_cfg["cycle_gap"]),
            waveform_policy=str(timeline_cfg["waveform_policy"]),
            manifest_metadata_factory=self._timeline_manifest_metadata,
        )
        self.loop.timeline_session = self.timeline_session
        self.loop.on_ai_turn = self.broadcast_chat  # AI 主动回合推送到页面聊天区

        story_cfg = cfg["story"]
        max_source_bytes = int(float(story_cfg["max_source_mb"]) * 1024 * 1024)
        self.story_import_directory = _story_data_directory(
            story_cfg["import_dir"], "story import directory"
        )
        self.story_source_store = PinnedStorySourceStore(
            self.story_import_directory, project_root=PROJECT_ROOT
        )
        story_analysis_directory = _story_data_directory(
            story_cfg["analysis_dir"], "story analysis directory"
        )
        self.story_source_loader = StorySourceLoader(max_bytes=max_source_bytes)
        self.story_source_max_bytes = max_source_bytes
        self.story_analysis_store = AnalysisStore(story_analysis_directory)
        self.story_sources: dict[str, _StorySourceRecord] = {}
        self.active_story_source_id: str | None = None
        self.story_source_generation = 0
        self.story_planning_task: asyncio.Task | None = None
        self.story_planning_context: _StoryPlanningContext | None = None
        self.story_runtime_owner: _StoryRuntimeOwner | None = None
        self.story_source_store_task: asyncio.Task | None = None
        self.story_import_requests: set[asyncio.Task] = set()
        self.story_shutting_down = False
        self.story_dlc_version = self._current_story_dlc_version()
        self.story_seed_factory = lambda: secrets.randbits(63)
        self.story_planner_signature = self._story_runtime_signature()
        self.chapter_planner = self._build_story_planner(
            self.story_planner_signature
        )
        self.novel_session = NovelSessionController(
            self.timeline_session,
            source_encoding="auto",
            analysis_version=OFFLINE_ANALYSIS_VERSION,
            dlc_version=self.story_dlc_version,
        )

        self.ws_clients: set[WebSocket] = set()
        self.tasks: list[asyncio.Task] = []
        self.auto_opened = False
        self.sensors_on = False
        self.sensor_watch_task: asyncio.Task | None = None
        self.layout: dict = {}  # 前端上报的三栏布局（监测/调试用）
        # 传感器运行时开关（不持久化；初始跟随 config.enabled）
        self.sensor_switches: dict[str, bool] = {
            "camera": bool(self.cfg["camera"].get("enabled", False)),
            "audio": bool(self.cfg["audio"].get("enabled", False)),
        }

    def _current_story_dlc_version(self) -> str:
        current = dlc_provenance(
            self.cfg,
            project_root=PROJECT_ROOT,
            waveform_policy=self.timeline_session.waveform_policy,
        )
        self.story_dlc_version = current
        return current

    def _story_runtime_signature(self) -> _StoryRuntimeSignature:
        model = str(getattr(self.llm, "model", "") or "").strip()
        transport = getattr(self.llm, "client", self.llm)
        return _StoryRuntimeSignature(
            client_identity=(id(self.llm), id(transport)),
            model=model,
            prompt_version=OFFLINE_ANALYSIS_VERSION,
            dlc_version=self._current_story_dlc_version(),
            waveforms=tuple(sorted(self.safety.presets)),
            caps=tuple(
                (channel, self.safety.cap_for(channel))
                for channel in ("A", "B")
            ),
        )

    def _build_story_planner(
        self, signature: _StoryRuntimeSignature
    ) -> ChapterPlanner:
        return ChapterPlanner(
            self.llm,
            waveform_registry=self.safety.presets,
            effective_caps=dict(signature.caps),
            reading_speed_cpm=self.cfg["story"]["reading_speed_cpm"],
            cycle_gap_policy=CycleGapPolicy.from_dict(
                self.cfg["timeline"]["cycle_gap"]
            ),
            safety_adapter=self.safety,
            model_identity=signature.model,
            prompt_version=signature.prompt_version,
            dlc_version=signature.dlc_version,
            strength_jitter=int(self.cfg["timeline"]["strength_jitter"]),
        )

    def _ensure_story_planner_current(self) -> _StoryRuntimeSignature:
        signature = self._story_runtime_signature()
        if getattr(self, "story_planner_signature", None) != signature:
            if self.story_planning_task is not None:
                raise _StoryConflict(
                    "story_planning_active", "a story chapter is already being planned"
                )
            if self.timeline_session.to_state().status.value != "idle":
                raise _StoryConflict(
                    "story_runtime_busy", "story runtime is already active"
                )
            pending = self.chapter_planner.cancel_pending()
            if pending:
                raise _StoryConflict(
                    "story_runtime_busy", "story planner is still shutting down"
                )
            self.chapter_planner = self._build_story_planner(signature)
            self.story_planner_signature = signature
        return signature

    def _story_runtime_is_busy(self) -> bool:
        planning = self.story_planning_task
        session = self.timeline_session.to_state()
        return (
            self.story_shutting_down
            or self.story_runtime_owner is not None
            or planning is not None
            or session.status.value != "idle"
            or bool(self.loop.autopilot)
        )

    def _acquire_story_runtime_owner(self, kind: str) -> _StoryRuntimeOwner:
        if not self.timeline_transition_lock.locked():
            raise RuntimeError("story runtime owner requires the transition lock")
        if self.story_runtime_owner is not None:
            raise _StoryConflict(
                "story_runtime_busy", "story runtime is already reserved"
            )
        owner = _StoryRuntimeOwner(kind=kind)
        self.story_runtime_owner = owner
        return owner

    def _release_story_runtime_owner(
        self, owner: _StoryRuntimeOwner | None
    ) -> None:
        if not self.timeline_transition_lock.locked():
            raise RuntimeError("story runtime owner requires the transition lock")
        if owner is not None and self.story_runtime_owner is owner:
            self.story_runtime_owner = None

    def _reserve_story_planning(
        self,
        record: _StorySourceRecord,
        story_map: StoryMap,
        chapter_id: str,
        speed: object,
    ) -> tuple[asyncio.Task, _StoryPlanningContext]:
        if self.story_planning_task is not None:
            raise _StoryConflict(
                "story_planning_active", "a story chapter is already being planned"
            )
        if self.story_runtime_owner is not None:
            raise _StoryConflict(
                "story_runtime_busy", "story runtime configuration is changing"
            )
        if self.timeline_session.to_state().status.value != "idle" or self.loop.autopilot:
            raise _StoryConflict(
                "story_runtime_busy", "story runtime is already active"
            )
        if self.active_story_source_id != record.source_id:
            raise _StoryConflict(
                "story_state_changed", "the selected story source changed"
            )
        runtime_signature = self._ensure_story_planner_current()
        planner = self.chapter_planner
        runtime_owner = self._acquire_story_runtime_owner("planning")
        context = _StoryPlanningContext(
            generation=self.story_source_generation,
            source_id=record.source_id,
            chapter_id=chapter_id,
            speed=speed,
            planner=planner,
            story_map=story_map,
            runtime_signature=runtime_signature,
            runtime_owner=runtime_owner,
        )
        try:
            task = asyncio.create_task(
                planner.plan(
                    record.story,
                    story_map,
                    chapter_id,
                    speed=speed,
                    seed=self.story_seed_factory(),
                ),
                name=f"story-plan-{record.source_id}",
            )
        except BaseException:
            self._release_story_runtime_owner(runtime_owner)
            raise
        task.add_done_callback(
            lambda completed: (
                None if completed.cancelled() else completed.exception()
            )
        )
        self.story_planning_task = task
        self.story_planning_context = context
        return task, context

    def _release_story_planning(self, task: asyncio.Task | None) -> None:
        if task is not None and self.story_planning_task is task:
            context = self.story_planning_context
            self.story_planning_task = None
            self.story_planning_context = None
            if context is not None:
                self._release_story_runtime_owner(context.runtime_owner)

    def _cancel_story_planning_now(self) -> tuple[asyncio.Task, ...]:
        task = self.story_planning_task
        planner = (
            self.story_planning_context.planner
            if self.story_planning_context is not None
            else self.chapter_planner
        )
        pending: list[asyncio.Task] = []
        if task is not None and not task.done():
            task.cancel()
            pending.append(task)
        cancel_pending = getattr(planner, "cancel_pending", None)
        if callable(cancel_pending):
            pending.extend(cancel_pending())
        if task is not None:
            self.story_source_generation += 1
        context = self.story_planning_context
        self.story_planning_task = None
        self.story_planning_context = None
        if context is not None:
            self._release_story_runtime_owner(context.runtime_owner)
        return tuple(dict.fromkeys(pending))

    @staticmethod
    async def _settle_story_planning(tasks: tuple[asyncio.Task, ...]) -> None:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _await_story_io_task(
        task: asyncio.Task,
    ) -> tuple[object, bool]:
        """Await non-cancellable filesystem work and remember caller cancellation."""

        caller_cancelled = False
        while True:
            try:
                return await asyncio.shield(task), caller_cancelled
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    raise
                caller_cancelled = True

    def _timeline_manifest_metadata(self) -> dict[str, str]:
        """Snapshot provenance when a new live session actually begins."""
        app_fingerprint = _app_version()
        dlc_fingerprint = dlc_provenance(
            self.cfg,
            project_root=PROJECT_ROOT,
            waveform_policy=self.timeline_session.waveform_policy,
        )
        dlc_version = str(
            self.cfg["character"].get("dlc_version") or dlc_fingerprint
        )
        return {
            "app_commit": app_fingerprint,
            "model": str(self.cfg["llm"].get("model") or "unconfigured"),
            "dlc_role": str(self.cfg["character"].get("role") or "default"),
            "dlc_profile": str(
                self.cfg["character"].get("profile") or "default"
            ),
            "dlc_version": dlc_version,
            "app_fingerprint": app_fingerprint,
            "dlc_fingerprint": dlc_fingerprint,
        }

    # ---------- 麦克风转写回调 ----------
    async def on_audio_text(self, text: str) -> None:
        self.loop.add_note(f"麦克风检测到玩家说：「{text}」")
        self.logger.info("麦克风信号已注入：%s", text)
        await self.broadcast()

    # ---------- 麦克风呻吟回调（无文字片段按电平分级） ----------
    async def on_audio_moan(self, kind: str, level: float) -> None:
        if kind == "high":
            self.loop.add_note(
                "麦克风检测到玩家发出较大的呻吟/惨叫（音量高）：应降低强度、安抚并关心，不要继续加码。"
            )
        else:
            self.loop.add_note(
                "麦克风检测到玩家发出普通呻吟/呜呜声（音量中等）：挑逗等级可逐渐增加，小幅加码。"
            )
        self.logger.info("麦克风呻吟信号注入：%s（%.3f）", kind, level)
        await self.broadcast()

    # ---------- 中继事件 ----------
    async def on_relay_event(self, event: str, payload: dict) -> None:
        if event == "slots_patch":
            # 取第一台设备的 props/slotState 同步给安全层
            client = self.relay.clients.get(self.relay.first_client_id() or "")
            if client:
                await self.loop.update_device_state(
                    client.get("props"), client.get("slotState")
                )
        elif event == "client_attached" and not self.auto_opened:
            # 首次配对成功：AI 主动开场（挑逗 + 第一个轻微试探）
            self.auto_opened = True
            asyncio.create_task(self._auto_open_and_broadcast())
        if event == "client_disconnected":
            async with self.timeline_transition_lock:
                planning = self._cancel_story_planning_now()
            await self._settle_story_planning(planning)
            async with self.timeline_transition_lock:
                await self.loop.on_client_disconnected()
                await self.set_sensors(False)
            self.logger.warning("APP 断开，自动清零并停止循环波形")
        await self.broadcast()

    async def _auto_open_and_broadcast(self) -> None:
        try:
            # 配对成功后缓 3 秒再开场，给玩家反应时间
            await asyncio.sleep(3)
            result = await self.loop.auto_open()
            await self.broadcast_chat(result)
            self.logger.info("AI 主动开场完成")
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("主动开场失败: %s", exc)

    async def on_relay_action(self, action: int, client_id: str) -> None:
        await self.loop.handle_feedback(action, client_id)
        await self.broadcast()

    # ---------- 广播 ----------
    async def broadcast(self) -> None:
        async with self.broadcast_lock:
            self.state_revision += 1
            state = self.build_state()
            dead = []
            for ws in list(self.ws_clients):
                try:
                    await ws.send_json({"type": "state", "data": state})
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                self.ws_clients.discard(ws)

    async def send_state(self, ws: WebSocket) -> None:
        async with self.broadcast_lock:
            await ws.send_json({"type": "state", "data": self.build_state()})

    async def broadcast_chat(self, result: dict) -> None:
        """把 AI 主动生成的台词推送到页面聊天区。"""
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json({"type": "chat", **result})
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    def build_state(self) -> dict:
        state = self.loop.build_state()
        state["sensors_on"] = self.sensors_on
        state["sensors"] = dict(self.sensor_switches)
        state["relay"] = self.relay.to_state()
        state["audio"] = self.audio.to_state()
        state["layout"] = dict(self.layout)
        state["character"] = self.cfg["character"]["name"]
        state["config_info"] = {
            "model": self.cfg["llm"]["model"],
            "title": str(self.cfg["app"].get("title", "郊狼 · AI 驯服师")),
            "profile": str(self.cfg["character"].get("profile") or "调教"),
            "player_nick": str(self.cfg["character"].get("player_nick") or "小柳"),
            "version": _public_app_version(),
        }
        state["story"] = _story_full_state(self)
        state["state_revision"] = self.state_revision
        return state

    # ---------- 传感器开关（跟随自动运行；浏览器断开超时自动关） ----------
    async def set_sensors(self, on: bool) -> None:
        """自动运行开启时启动「开关为开」的传感器，关闭时全部停止（config.enabled 为初始默认）。"""
        self.sensors_on = bool(on)
        if on:
            if self.sensor_switches.get("camera"):
                await self.camera.start()
            else:
                await self.camera.stop()
            if self.sensor_switches.get("audio"):
                await self.audio.start()
            else:
                await self.audio.stop()
        else:
            await self.camera.stop()
            await self.audio.stop()

    def _on_ws_clients_change(self) -> None:
        """有浏览器接入：重启传感器（自动运行开着时）；全断开：延迟关传感器。"""
        if self.ws_clients:
            if self.sensor_watch_task:
                self.sensor_watch_task.cancel()
                self.sensor_watch_task = None
            if self.loop.autopilot and not self.sensors_on:
                asyncio.create_task(self.set_sensors(True))
        elif self.sensor_watch_task is None:
            self.sensor_watch_task = asyncio.create_task(self._watch_sensors_idle())

    async def _watch_sensors_idle(self) -> None:
        timeout = float(self.cfg["app"].get("sensor_idle_timeout_s", 30))
        try:
            await asyncio.sleep(timeout)
        finally:
            self.sensor_watch_task = None
        if not self.ws_clients:
            self.logger.info("浏览器已断开 %.0fs，自动关闭摄像头/麦克风", timeout)
            await self.set_sensors(False)
            await self.broadcast()

    async def _sensor_watchdog(self) -> None:
        """看门狗：开关开着但传感器没在跑且有错误时，每 15s 自动重试启动（拔插设备自恢复）。"""
        while True:
            await asyncio.sleep(15)
            try:
                if not self.sensors_on:
                    continue
                cam_bad = (
                    self.sensor_switches.get("camera")
                    and not self.camera.has_frame()
                    and bool(self.camera.error)
                )
                mic_bad = (
                    self.sensor_switches.get("audio")
                    and not self.audio.to_state().get("running")
                    and bool(self.audio.error)
                )
                if cam_bad or mic_bad:
                    self.logger.info("传感器看门狗重试启动（摄像头=%s 麦克风=%s）", cam_bad, mic_bad)
                    await self.set_sensors(True)
                    await self.broadcast()
            except Exception:  # noqa: BLE001
                self.logger.exception("传感器看门狗异常")

    # ---------- 生命周期 ----------
    async def start_background(self) -> None:
        self.tasks.append(asyncio.create_task(self.relay.run()))
        self.tasks.append(asyncio.create_task(self._sensor_watchdog()))
        # 配置里自动运行开着时，启动真正的循环任务（此前只置状态、不启动任务，
        # 导致重启后「假开真停」：AI 一直不说话）
        if self.loop.autopilot:
            async with self.timeline_transition_lock:
                await self.loop.set_autopilot(True)
                # 自动运行开着也只在「已有浏览器接入」时才启动传感器；
                # 无浏览器时不占摄像头/麦克风，等页面连上后由 _on_ws_clients_change 再启动
                if self.ws_clients:
                    await self.set_sensors(True)
        self.loop.start_observe_loop()

    async def shutdown(self) -> None:
        cleanup = asyncio.create_task(
            self._shutdown_cleanup(), name="app-state-shutdown-cleanup"
        )
        caller_cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    break
                caller_cancelled = True
            except BaseException:
                break
        if caller_cancelled:
            if not cleanup.cancelled():
                cleanup.exception()
            raise asyncio.CancelledError
        return cleanup.result()

    async def _shutdown_cleanup(self) -> None:
        observe_task = self.loop.observe_task
        self.loop.stop_observe_loop()
        primary_error: BaseException | None = None
        try:
            async with self.timeline_transition_lock:
                self.story_shutting_down = True
                planning = self._cancel_story_planning_now()
            await self._settle_story_planning(planning)
            async with self.timeline_transition_lock:
                # Process shutdown is always abnormal lifecycle termination: live
                # work and replay playback clear output but never create an archive.
                try:
                    await self.loop.stop_timeline_session()
                except BaseException as exc:
                    primary_error = exc
                try:
                    await self.set_sensors(False)
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc

                if (
                    primary_error is not None
                    or self.cfg["safety"]["auto_clear_on_disconnect"]
                ):
                    try:
                        await self.loop.estop()
                    except BaseException as exc:
                        self.logger.exception(
                            "shutdown estop fallback failed: %s", exc
                        )
        finally:
            imports = tuple(
                task
                for task in self.story_import_requests
                if task is not asyncio.current_task()
            )
            if imports:
                await asyncio.gather(*imports, return_exceptions=True)
            remaining_store = self.story_source_store_task
            if remaining_store is not None and not remaining_store.done():
                await asyncio.gather(remaining_store, return_exceptions=True)
            background = list(self.tasks)
            if self.sensor_watch_task is not None:
                background.append(self.sensor_watch_task)
                self.sensor_watch_task = None
            if observe_task is not None:
                background.append(observe_task)
            unique_tasks = tuple(dict.fromkeys(background))
            for task in unique_tasks:
                if task is not asyncio.current_task() and not task.done():
                    task.cancel()
            if unique_tasks:
                await asyncio.gather(*unique_tasks, return_exceptions=True)
            self.tasks.clear()
            self.story_source_store.close()

        if primary_error is not None:
            raise primary_error


def make_app() -> FastAPI:
    cfg = load_config()
    state = AppState(cfg)
    app = FastAPI(title="AI Coyote Tamer")

    @app.on_event("startup")
    async def startup() -> None:
        await state.start_background()
        state.logger.info(
            "启动完成。Web: http://%s:%s  dry_run=%s",
            cfg["app"]["host"], cfg["app"]["port"], cfg["app"]["dry_run"],
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await state.shutdown()

    # ---------- 页面 ----------
    @app.get("/")
    async def index() -> Response:
        # 托管 React 前端构建产物；未构建时给出一句构建提示
        if (FRONTEND_DIST / "index.html").exists():
            return FileResponse(FRONTEND_DIST / "index.html")
        return Response(
            "前端尚未构建：请在 frontend\\ 目录执行 npm install && npm run build",
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/index.html")
    async def index_html() -> RedirectResponse:
        """收藏夹/手输带 index.html 的地址时别 404，重定向回首页。"""
        return RedirectResponse("/")

    # React 构建产物的静态资源（存在时才挂载）
    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        return JSONResponse(state.build_state())

    # ---------- 离线小说来源 / 阅读器 / 会话 ----------
    @app.post("/api/story/import")
    async def api_story_import(
        file: UploadFile = File(...), encoding: str = Form("auto")
    ) -> JSONResponse:
        try:
            source_bytes = await file.read(state.story_source_max_bytes + 1)
            story = state.story_source_loader.load(
                file.filename or "", source_bytes, encoding=encoding
            )
        except (OSError, OverflowError, StorySourceError, TypeError, ValueError):
            return JSONResponse(
                {"code": "story_import_invalid", "error": "story source is invalid"},
                status_code=400,
            )
        owner: _StoryRuntimeOwner | None = None
        store_task: asyncio.Task | None = None
        cleanup_task: asyncio.Task | None = None
        stored: StoredStorySource | None = None
        record: _StorySourceRecord | None = None
        lookup: AnalysisLookup | None = None
        response: JSONResponse | None = None
        registered = False
        caller_cancelled = False
        request_task = asyncio.current_task()
        if request_task is None:
            return JSONResponse(
                {"code": "story_import_failed", "error": "story import failed"},
                status_code=500,
            )
        async with state.timeline_transition_lock:
            if state._story_runtime_is_busy():
                return _story_transition_error(
                    state,
                    _StoryConflict(
                        "story_runtime_busy", "story runtime is already active"
                    ),
                )
            owner = state._acquire_story_runtime_owner("import")
            source_generation = state.story_source_generation
            store_task = asyncio.create_task(
                asyncio.to_thread(state.story_source_store.store, story),
                name=f"story-source-store-{story.source_sha256[:12]}",
            )
            state.story_source_store_task = store_task
            state.story_import_requests.add(request_task)
        try:
            try:
                stored_result, was_cancelled = await state._await_story_io_task(
                    store_task
                )
                caller_cancelled = caller_cancelled or was_cancelled
                if not isinstance(stored_result, StoredStorySource):
                    raise StorySourceStorageError(
                        "story source store returned an invalid result"
                    )
                stored = stored_result
            except StorySourceStorageError:
                response = JSONResponse(
                    {
                        "code": "story_import_failed",
                        "error": "story source could not be stored",
                    },
                    status_code=500,
                )
            except Exception:
                state.logger.exception("unexpected story source store failure")
                response = JSONResponse(
                    {
                        "code": "story_import_failed",
                        "error": "story source could not be stored",
                    },
                    status_code=500,
                )
            if stored is not None and not caller_cancelled:
                async with state.timeline_transition_lock:
                    runtime_changed = (
                        state.story_runtime_owner is not owner
                        or state.story_shutting_down
                        or state.story_source_generation != source_generation
                        or state.story_planning_task is not None
                        or state.timeline_session.to_state().status.value != "idle"
                        or bool(state.loop.autopilot)
                    )
                    if runtime_changed:
                        response = _story_transition_error(
                            state,
                            _StoryConflict(
                                "story_runtime_busy",
                                "story runtime changed during import",
                            ),
                        )
                    else:
                        record = _StorySourceRecord(
                            source_id=stored.source_id,
                            story=story,
                            encoding=encoding,
                            storage_path=stored.path,
                        )
                        state.story_sources[record.source_id] = record
                        state.active_story_source_id = record.source_id
                        state.story_source_generation += 1
                        lookup = _inspect_story_analysis(state, record)
                        registered = True
        finally:
            try:
                if stored is not None and not registered:
                    cleanup_task = asyncio.create_task(
                        asyncio.to_thread(state.story_source_store.delete, stored),
                        name=f"story-source-store-delete-{stored.source_id}",
                    )
                    async with state.timeline_transition_lock:
                        if state.story_source_store_task is store_task:
                            state.story_source_store_task = cleanup_task
                    try:
                        _, cleanup_cancelled = await state._await_story_io_task(
                            cleanup_task
                        )
                        caller_cancelled = caller_cancelled or cleanup_cancelled
                    except Exception:
                        state.logger.exception(
                            "failed to remove unregistered story source"
                        )
            finally:
                async with state.timeline_transition_lock:
                    if state.story_source_store_task in (store_task, cleanup_task):
                        state.story_source_store_task = None
                    state.story_import_requests.discard(request_task)
                    state._release_story_runtime_owner(owner)
        if caller_cancelled:
            raise asyncio.CancelledError
        if response is not None:
            return response
        if record is None or lookup is None:
            state.logger.error("story import completed without a public result")
            return JSONResponse(
                {"code": "story_import_failed", "error": "story import failed"},
                status_code=500,
            )
        await state.broadcast()
        return JSONResponse(
            {
                "source": _story_source_payload(record),
                "analysis": _story_analysis_payload(state, record, lookup.status),
            }
        )

    @app.get("/api/story/{source_id}/analysis")
    async def api_story_analysis(source_id: str) -> JSONResponse:
        record = _story_record(state, source_id)
        if record is None:
            return _story_not_found_response()
        lookup = _inspect_story_analysis(state, record)
        if lookup.status != "ready":
            return _analysis_error_response(state, record, lookup)
        return JSONResponse(_story_analysis_payload(state, record, "ready"))

    @app.get("/api/story/{source_id}/chapters")
    async def api_story_chapters(source_id: str) -> JSONResponse:
        record = _story_record(state, source_id)
        if record is None:
            return _story_not_found_response()
        lookup = _inspect_story_analysis(state, record)
        if lookup.status != "ready" or lookup.story_map is None:
            return _analysis_error_response(state, record, lookup)
        return JSONResponse(
            {
                "source": _story_source_payload(record),
                "analysis": _story_analysis_payload(state, record, "ready"),
                "chapters": _story_chapters_payload(lookup.story_map),
            }
        )

    @app.post("/api/story/{source_id}/chapters/{chapter_id}/play")
    async def api_story_play(
        source_id: str, chapter_id: str, body: dict
    ) -> JSONResponse:
        record = _story_record(state, source_id)
        if record is None:
            return _story_not_found_response()
        speed = body.get("speed", "standard")
        planning_task: asyncio.Task | None = None
        try:
            async with state.timeline_transition_lock:
                current_record = _story_record(state, source_id)
                if current_record is not record:
                    raise _StoryConflict(
                        "story_state_changed", "the selected story source changed"
                    )
                lookup = _inspect_story_analysis(state, record)
                if lookup.status != "ready" or lookup.story_map is None:
                    return _analysis_error_response(state, record, lookup)
                planning_task, planning_context = state._reserve_story_planning(
                    record, lookup.story_map, chapter_id, speed
                )
            await state.broadcast()
            try:
                plan = await asyncio.shield(planning_task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    async with state.timeline_transition_lock:
                        pending = state._cancel_story_planning_now()
                    await state._settle_story_planning(pending)
                    raise
                raise _StoryConflict(
                    "story_planning_cancelled", "chapter planning was cancelled"
                )
            async with state.timeline_transition_lock:
                if (
                    state.story_planning_task is not planning_task
                    or state.story_planning_context is not planning_context
                    or state.story_runtime_owner
                    is not planning_context.runtime_owner
                    or state.story_source_generation != planning_context.generation
                    or state.active_story_source_id != planning_context.source_id
                    or state.chapter_planner is not planning_context.planner
                ):
                    raise _StoryConflict(
                        "story_state_changed", "story state changed during planning"
                    )
                current_record = _story_record(state, source_id)
                current_lookup = (
                    _inspect_story_analysis(state, current_record)
                    if current_record is record
                    else None
                )
                if (
                    current_lookup is None
                    or current_lookup.status != "ready"
                    or current_lookup.story_map is None
                    or current_lookup.story_map != planning_context.story_map
                    or state._story_runtime_signature()
                    != planning_context.runtime_signature
                    or state.story_planner_signature
                    != planning_context.runtime_signature
                    or state.timeline_session.to_state().status.value != "idle"
                    or state.novel_session.to_state().status.value != "idle"
                ):
                    raise _StoryConflict(
                        "story_state_changed", "story state changed during planning"
                    )
                novel_state = await state.novel_session.start(
                    plan,
                    record.story,
                    planning_context.story_map,
                    source_encoding=record.encoding,
                    dlc_version=planning_context.runtime_signature.dlc_version,
                )
                payload = _novel_session_payload(novel_state)
                state._release_story_planning(planning_task)
        except Exception as exc:  # The helper maps internal details to stable codes.
            async with state.timeline_transition_lock:
                state._release_story_planning(planning_task)
            await state.broadcast()
            return _story_transition_error(state, exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.get("/api/story/reader")
    async def api_story_reader() -> JSONResponse:
        record = _active_story_record(state)
        if record is None:
            return JSONResponse(
                {"code": "story_reader_missing", "error": "no story is selected"},
                status_code=409,
            )
        lookup = _inspect_story_analysis(state, record)
        if lookup.status != "ready" or lookup.story_map is None:
            return _analysis_error_response(state, record, lookup)
        return JSONResponse(
            {
                "source": _story_source_payload(record),
                "analysis": _story_analysis_payload(state, record, "ready"),
                "session": _novel_session_payload(state.novel_session.to_state()),
            }
        )

    @app.get("/api/story/reader/text")
    async def api_story_reader_text(start: int, end: int) -> JSONResponse:
        record = _active_story_record(state)
        if record is None:
            return JSONResponse(
                {"code": "story_reader_missing", "error": "no story is selected"},
                status_code=409,
            )
        lookup = _inspect_story_analysis(state, record)
        if lookup.status != "ready" or lookup.story_map is None:
            return _analysis_error_response(state, record, lookup)
        text = record.story.text
        if (
            start < 0
            or end < start
            or end > len(text)
            or end - start > _READER_SLICE_MAX_CHARS
        ):
            return JSONResponse(
                {"code": "reader_range_invalid", "error": "reader range is invalid"},
                status_code=400,
            )
        return JSONResponse(
            {
                "start": start,
                "end": end,
                "text_length": len(text),
                "text": text[start:end],
            }
        )

    @app.post("/api/story/pause")
    async def api_story_pause() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                novel_state = await state.novel_session.pause()
                payload = _novel_session_payload(novel_state)
        except Exception as exc:
            return _story_transition_error(state, exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/story/resume")
    async def api_story_resume(body: dict) -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                novel_state = await state.novel_session.resume(body.get("from", "current"))
                payload = _novel_session_payload(novel_state)
        except Exception as exc:
            return _story_transition_error(state, exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/story/finish")
    async def api_story_finish() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                summary = await state.novel_session.finish()
                payload = {
                    "replay": _replay_summary_payload(summary),
                    "session": _novel_session_payload(state.novel_session.to_state()),
                }
        except Exception as exc:
            return _story_transition_error(state, exc)
        await state.broadcast()
        return JSONResponse(payload)

    # ---------- 时间线会话 / 重放 ----------
    @app.post("/api/session/start")
    async def api_session_start() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                await state.loop.start_timeline_session()
                await state.set_sensors(True)
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/session/pause")
    async def api_session_pause() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                session_state = state.timeline_session.to_state()
                if session_state.mode != "autopilot":
                    raise RuntimeError("no live session to pause")
                await state.loop.set_autopilot(False)
                await state.set_sensors(False)
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/session/resume")
    async def api_session_resume(body: dict) -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                await state.loop.resume_timeline_session(body.get("cursor"))
                await state.set_sensors(True)
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/session/finish")
    async def api_session_finish() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                summary = await state.loop.finish_timeline_session()
                await state.set_sensors(False)
                payload = _replay_summary_payload(summary)
        except (ReplayStoreError, RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.get("/api/replays")
    async def api_replays() -> JSONResponse:
        try:
            summaries = state.replay_store.list()
        except ReplayStoreError as exc:
            return _timeline_error_response(exc)
        return JSONResponse([_replay_summary_payload(item) for item in summaries])

    @app.post("/api/replays/playback/pause")
    async def api_replay_pause() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                await state.loop.pause_replay_session()
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/replays/playback/resume")
    async def api_replay_resume() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                await state.loop.resume_replay_session()
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/replays/playback/stop")
    async def api_replay_stop() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                if state.timeline_session.to_state().mode != "replay":
                    raise RuntimeError("no replay playback to stop")
                await state.loop.stop_timeline_session()
                payload = _session_payload(state.timeline_session)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/replays/{replay_id}/play")
    async def api_replay_play(replay_id: str, body: dict) -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                session_state = await state.loop.start_replay_session(
                    replay_id, body.get("cursor", 0)
                )
                payload = session_state.to_dict()
        except (ReplayStoreError, RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    @app.get("/api/replays/{replay_id}/download")
    async def api_replay_download(replay_id: str) -> Response:
        try:
            archive_bytes = _validated_replay_archive(
                state.replay_store, replay_id
            )
        except (_ReplayNotFoundError, ReplayStoreError) as exc:
            return _timeline_error_response(exc)
        return Response(
            content=archive_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{replay_id}.coyote-replay"'
                )
            },
        )

    @app.get("/api/qrcode.png")
    async def api_qrcode() -> Response:
        controller_id = state.relay.controller_id
        if not controller_id:
            return Response("中继未连接，暂无配对码", status_code=503, media_type="text/plain")
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        url = build_pair_url(cfg, lan_ip, controller_id)
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    @app.get("/api/pair_url")
    async def api_pair_url() -> JSONResponse:
        controller_id = state.relay.controller_id
        if not controller_id:
            return JSONResponse({"error": "中继未连接"}, status_code=503)
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        return JSONResponse({"url": build_pair_url(cfg, lan_ip, controller_id)})

    @app.get("/api/network")
    async def api_network() -> JSONResponse:
        """网络自检：本机 IP 列表 + 当前配对地址。"""
        controller_id = state.relay.controller_id
        lan_ip = cfg["relay"]["lan_ip"]
        if lan_ip == "auto":
            lan_ip = get_lan_ip()
        pair_url = (
            build_pair_url(cfg, lan_ip, controller_id) if controller_id else None
        )
        return JSONResponse(
            {
                "lan_ip": lan_ip,
                "all_ips": get_local_ips(),
                "pair_url": pair_url,
                "public_url": str(cfg["relay"].get("public_url") or ""),
                "relay_port": 9998,
                "hint": (
                    "手机浏览器打开 http://<电脑IP>:9998/ 若立即显示 "
                    "'WebSocket upgrade required' 说明链路通；超时说明被防火墙/热点隔离拦截。"
                ),
            }
        )

    # ---------- 控制 ----------
    @app.post("/api/chat")
    async def api_chat(body: dict) -> JSONResponse:
        result = await state.loop.handle_user_message(str(body.get("message", "")))
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/estop")
    async def api_estop() -> JSONResponse:
        # Physical e-stop happens before waiting for API transition bookkeeping.
        result = await state.loop.estop()
        async with state.timeline_transition_lock:
            await state.set_sensors(False)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/resume")
    async def api_resume() -> JSONResponse:
        try:
            async with state.timeline_transition_lock:
                result = await state.loop.resume()
        except (DeviceOutputError, RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(result)

    @app.post("/api/manual")
    async def api_manual(body: dict) -> JSONResponse:
        """手动控制：与 AI 指令走完全相同的安全链路。"""
        if not isinstance(body, dict) or "op" not in body:
            return JSONResponse({"error": "缺少 op"}, status_code=400)
        try:
            async with state.timeline_transition_lock:
                was_active = state.timeline_session.to_state().status.value != "idle"
                executed, dropped = await state.loop.execute_manual_action(body)
                if was_active:
                    await state.set_sensors(False)
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse({"executed": executed, "dropped": dropped})

    @app.post("/api/device/channels")
    async def api_device_channels(body: dict) -> JSONResponse:
        """通道配件设置：{A:{name,location}, B:{...}}，保存到 device_channels.yaml。"""
        try:
            save_device_channels(cfg, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await state.broadcast()
        return JSONResponse({"ok": True, "device_channels": cfg["device_channels"]})

    @app.post("/api/device/channels/enabled")
    async def api_device_channel_enabled(body: dict) -> JSONResponse:
        """手动开关通道：{channel:"A", enabled:false}。关闭的通道拒绝一切动作并清零。"""
        ch = str(body.get("channel") or "").strip().upper()
        if ch not in ("A", "B"):
            return JSONResponse({"error": "channel 只能是 A 或 B"}, status_code=400)
        enabled = bool(body.get("enabled"))
        try:
            async with state.timeline_transition_lock:
                result = await state.loop.set_channel_enabled(ch, enabled)
                save_device_channels(cfg, {ch: {"enabled": enabled}})
        except DeviceOutputError as exc:
            return JSONResponse(
                {"error": "通道物理清除失败"}, status_code=exc.status_code
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        if result["dropped"]:
            return JSONResponse(
                {"error": "通道物理清除失败"},
                status_code=503,
            )
        await state.broadcast()
        return JSONResponse({"ok": True, "enabled_channels": state.safety.enabled})

    @app.post("/api/sensors")
    async def api_sensors(body: dict) -> JSONResponse:
        """运行时单独开关摄像头/麦克风：{camera: bool, audio: bool}（可只传一项；不持久化）。"""
        changed = False
        for key in ("camera", "audio"):
            if key in body and isinstance(body[key], bool):
                if state.sensor_switches.get(key) != body[key]:
                    state.sensor_switches[key] = body[key]
                    changed = True
        if changed:
            # 传感器正在运行时按最新开关重新对齐（关掉的立即停）
            if state.sensors_on:
                await state.set_sensors(True)
            await state.broadcast()
        return JSONResponse({"ok": True, "sensors": state.sensor_switches})

    @app.post("/api/history/clear")
    async def api_history_clear(body: dict) -> JSONResponse:
        """清空对话历史（模型上下文 + 页面记录由前端同步清）。"""
        try:
            async with state.timeline_transition_lock:
                session_state = state.timeline_session.to_state()
                if (
                    session_state.mode == "autopilot"
                    and session_state.status.value != "idle"
                ):
                    await state.loop.finish_timeline_session()
                    await state.set_sensors(False)
                state.loop.clear_history()
        except (ReplayStoreError, RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse({"ok": True})

    @app.post("/api/layout")
    async def api_layout(body: dict) -> JSONResponse:
        """前端上报三栏布局（调试/监测用）：{sidebar_w, control_w, inner_width, zoom}。"""
        try:
            state.layout = {
                "sidebar_w": float(body.get("sidebar_w", 0)),
                "control_w": float(body.get("control_w", 0)),
                "inner_width": float(body.get("inner_width", 0)),
                "zoom": float(body.get("zoom", 1.0)),
            }
        except (TypeError, ValueError):
            return JSONResponse({"error": "数值格式错误"}, status_code=400)
        return JSONResponse({"ok": True, "layout": state.layout})

    @app.post("/api/device/channels/cap")
    async def api_device_channel_cap(body: dict) -> JSONResponse:
        """调通道运行时强度上限（1~硬上限，不持久化）：{channel:"A", value:60}。"""
        ch = str(body.get("channel") or "").strip().upper()
        if ch not in ("A", "B"):
            return JSONResponse({"error": "channel 只能是 A 或 B"}, status_code=400)
        try:
            value = int(body.get("value", 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "value 必须是整数"}, status_code=400)
        planning: tuple[asyncio.Task, ...] = ()
        owner: _StoryRuntimeOwner | None = None
        try:
            async with state.timeline_transition_lock:
                if (
                    state.story_runtime_owner is not None
                    and state.story_runtime_owner.kind != "planning"
                ):
                    raise _StoryConflict(
                        "story_runtime_busy", "story runtime is already reserved"
                    )
                if state.story_planning_task is not None:
                    planning = state._cancel_story_planning_now()
                owner = state._acquire_story_runtime_owner("cap")
            await state._settle_story_planning(planning)
            async with state.timeline_transition_lock:
                if state.story_runtime_owner is not owner:
                    raise _StoryConflict(
                        "story_runtime_busy", "story runtime reservation changed"
                    )
                result = await state.loop.set_runtime_cap(ch, value)
        except _StoryConflict as exc:
            return _story_transition_error(state, exc)
        except DeviceOutputError as exc:
            return JSONResponse(
                {"error": "运行时上限物理降档失败"},
                status_code=exc.status_code,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        finally:
            async with state.timeline_transition_lock:
                state._release_story_runtime_owner(owner)
        if result["dropped"]:
            return JSONResponse(
                {"error": "运行时上限物理降档失败"},
                status_code=503,
            )
        await state.broadcast()
        return JSONResponse(
            {
                "ok": True,
                "user_caps": state.safety.user_caps,
                "effective_caps": {c: state.safety.cap_for(c) for c in ("A", "B")},
            }
        )

    @app.post("/api/character/profile")
    async def api_character_profile(body: dict) -> JSONResponse:
        """切换角色/风格：{role: "触手", profile: "调教"}，保存并热加载；目标 DLC 未安装时拒绝。"""
        requested_role = str(body.get("role") or "").strip()
        requested_profile = str(body.get("profile") or "").strip()
        owner: _StoryRuntimeOwner | None = None
        try:
            async with state.timeline_transition_lock:
                active_session = state.timeline_session.to_state()
                if (
                    state.story_runtime_owner is not None
                    or state.story_planning_task is not None
                    or (
                        active_session.mode in ("novel", "replay")
                        and active_session.status.value != "idle"
                    )
                ):
                    return _story_transition_error(
                        state,
                        _StoryConflict(
                            "story_runtime_busy", "story runtime is already active"
                        ),
                    )
                owner = state._acquire_story_runtime_owner("profile")
                candidate_cfg = deepcopy(cfg)
                reload_character(candidate_cfg)
                roles = {
                    item["name"]: item
                    for item in (
                        candidate_cfg["character"].get("roles") or []
                    )
                }
                role = requested_role or str(
                    candidate_cfg["character"].get("role") or ""
                )
                if role not in roles:
                    return JSONResponse(
                        {"error": f"未知角色，可用：{list(roles)}"},
                        status_code=400,
                    )
                profiles = {
                    item["name"]: item["available"]
                    for item in roles[role]["profiles"]
                }
                profile = requested_profile or next(iter(profiles), "")
                if profile not in profiles:
                    return JSONResponse(
                        {"error": f"未知风格版本，可用：{list(profiles)}"},
                        status_code=400,
                    )
                if not profiles.get(profile, True):
                    return JSONResponse(
                        {
                            "error": (
                                f"「{roles[role]['label']}·{profile}」的 DLC 未安装："
                                "请先在「角色设置」导入对应 DLC 包。"
                            ),
                            "detail": "dlc_missing",
                        },
                        status_code=400,
                    )
                session_state = state.timeline_session.to_state()
                if (
                    session_state.mode == "autopilot"
                    and session_state.status.value != "idle"
                ):
                    await state.loop.finish_timeline_session()
                    await state.set_sensors(False)
                save_character_runtime(cfg, role=role, profile=profile)
                payload = {
                    "ok": True,
                    "role": cfg["character"]["role"],
                    "profile": cfg["character"]["profile"],
                }
        except (ReplayStoreError, RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        finally:
            async with state.timeline_transition_lock:
                state._release_story_runtime_owner(owner)
        await state.broadcast()
        return JSONResponse(payload)

    @app.post("/api/character/nick")
    async def api_character_nick(body: dict) -> JSONResponse:
        """改玩家昵称：{nick: "..."}，保存并热加载。"""
        nick = str(body.get("nick") or "").strip()
        if not nick or len(nick) > 20:
            return JSONResponse({"error": "昵称不能为空且不超过 20 字"}, status_code=400)
        save_character_runtime(cfg, player_nick=nick)
        await state.broadcast()
        return JSONResponse({"ok": True, "player_nick": cfg["character"]["player_nick"]})

    @app.post("/api/dlc/import")
    @_guard_story_runtime_change(state)
    async def api_dlc_import(file: UploadFile = File(...)) -> JSONResponse:
        """导入 DLC：上传 .zip（解出全部 .md）或单个 .md → 拷进 content\\pack\\ 并自动接通 character.yaml。

        zip 可按 DLC 目录分组（支持仓库 zip 单层包装目录、一个 zip 含多个 DLC）；
        单个 md 从文件名解析主题（<主题>-角色提示词-<风格>.md）。不用重启：改完即热加载。
        """
        name = file.filename or ""
        low = name.lower()
        if not (low.endswith(".zip") or low.endswith(".md")):
            return JSONResponse({"error": "只支持 .zip 或 .md 文件"}, status_code=400)
        data = await file.read()
        if not data:
            return JSONResponse({"error": "文件为空"}, status_code=400)
        if len(data) > 50 * 1024 * 1024:
            return JSONResponse({"error": "文件过大（上限 50MB）"}, status_code=400)

        pack_dir = PROJECT_ROOT / "content" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)

        groups: dict[str, dict[str, bytes]] = {}  # DLC 目录名 -> {文件名: 内容}
        if low.endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                return JSONResponse({"error": "zip 包损坏或格式不对"}, status_code=400)
            with zf:
                raw_names = zf.namelist()
                fixed_names: list[str] = []
                for n in raw_names:
                    # Windows 压缩工具不写 UTF-8 标志，先按 cp437 还原原始字节再按 UTF-8 解码中文名
                    fixed = n
                    try:
                        fixed = n.encode("cp437").decode("utf-8")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                    fixed_names.append(fixed)
                for n, fixed in zip(raw_names, fixed_names):
                    bn = Path(fixed).name
                    if not bn.lower().endswith(".md") or bn.startswith("."):
                        continue
                    parts = [p for p in Path(fixed).parts if p not in ("", ".", "..")]
                    # 组名：路径中形如 DLC<序号>-<角色>-<风格> 的组件；否则首个顶层目录（跳过单层包装）
                    grp = next((p for p in parts if re.match(r"^DLC\d+-.+?-.+$", p)), None)
                    if not grp:
                        if len(parts) >= 3 and not re.match(r"^DLC\d+", parts[0]):
                            grp = parts[1]
                        elif parts:
                            grp = parts[0]
                    if not grp:
                        continue
                    groups.setdefault(grp, {})[bn] = zf.read(n)
            if not groups:
                return JSONResponse({"error": "zip 里没有 .md 文件"}, status_code=400)
        else:
            # 单个 md：文件名 <主题>-角色提示词-<风格>.md → 归入「DLC导入-<主题>-<风格>」组
            m2 = re.match(r"^(.+?)-角色提示词-(.+?)\.md$", name)
            grp = f"DLC导入-{m2.group(1)}-{m2.group(2)}" if m2 else (Path(name).stem or "DLC导入")
            groups[grp] = {name: data}

        # 落盘
        for grp, mds in groups.items():
            if grp in (".", "..") or ".." in Path(grp).parts:
                continue
            dlc_dir = pack_dir / grp
            dlc_dir.mkdir(parents=True, exist_ok=True)
            for bn, content in mds.items():
                (dlc_dir / bn).write_bytes(content)

        # 自动接通（每个 DLC 目录一组；角色已存在则接通其风格档并修正档位为中，
        # 新角色自动注册角色块；无法解析主题名时才按当前角色兜底）
        def wire_group(dlc_folder: str, mds: dict[str, bytes]) -> tuple[str | None, str | None]:
            prompt_md = next((b for b in mds if "角色提示词" in b), None) or next(
                (b for b in mds if "提示词" in b), None
            )
            if not prompt_md:
                return None, None
            reload_character(cfg)
            char_path = Path(cfg["character_file"])
            if not char_path.is_absolute():
                char_path = PROJECT_ROOT / char_path
            rel = f"content/pack/{dlc_folder}/{prompt_md}"

            m = re.match(r"^DLC\d+-(.+?)-(.+)$", dlc_folder)
            dlc_role = (m.group(1) if m else "").strip()
            dlc_style = (m.group(2) if m else "").strip()
            if not dlc_role:
                m2 = re.match(r"^(.+?)-角色提示词", prompt_md)
                if m2:
                    dlc_role = m2.group(1).strip()
                    dlc_style = next((s for s in ("纯爱", "调教", "凌辱") if s in prompt_md), "调教")
            roles_map = {r["name"]: r for r in (cfg["character"].get("roles") or [])}

            patched_role = None
            patched_profile = None
            if dlc_role and dlc_role in roles_map:
                profiles_of = [p["name"] for p in roles_map[dlc_role]["profiles"]]
                target = next((p for p in profiles_of if p and p in prompt_md), None)
                if target is None:
                    target = profiles_of[0] if profiles_of else None
                if target and patch_character_prompt_file(char_path, target, rel, role=dlc_role, level="中"):
                    patched_profile = target
                    patched_role = dlc_role
            elif dlc_role:
                style = dlc_style or "调教"
                narrative = "触手" if dlc_role == "触手" else "装置"
                if patch_character_add_role(char_path, dlc_role, dlc_role, style, "中", rel, narrative):
                    patched_profile = style
                    patched_role = dlc_role
            else:
                # 无法解析主题名（旧式单 md）：当前角色内匹配
                avail = cfg["character"].get("profile_available") or {}
                profiles = list(cfg["character"].get("profiles") or [])
                cur_role = str(cfg["character"].get("role") or "")
                target = next((p for p in profiles if p and p in prompt_md), None)
                if target is None:
                    target = next((p for p in profiles if p != "纯爱" and not avail.get(p, True)), None)
                if target is None and "调教" in profiles:
                    target = "调教"
                if target and patch_character_prompt_file(char_path, target, rel, role=cur_role):
                    patched_profile = target
                    patched_role = cur_role
            reload_character(cfg)
            return patched_role, patched_profile

        pre_roles = {r["name"] for r in (cfg["character"].get("roles") or [])}
        results: list[tuple[str, str | None, str | None]] = []
        for grp, mds in groups.items():
            r, p = wire_group(grp, mds)
            results.append((grp, r, p))
        # 响应里优先展示「新注册的主题」，否则最后一个
        chosen = next((x for x in results if x[1] and x[1] not in pre_roles), None) or next(
            (x for x in reversed(results) if x[1]), None
        )

        await state.broadcast()
        all_files = sorted({bn for _, mds in groups.items() for bn in mds})
        return JSONResponse(
            {
                "ok": True,
                "dir": chosen[0] if chosen else (next(iter(groups)) if groups else ""),
                "dirs": [g for g, _, _ in results],
                "files": all_files,
                "role": chosen[1] if chosen else None,
                "profile": chosen[2] if chosen else None,
            }
        )

    @app.post("/api/autopilot")
    async def api_autopilot(body: dict) -> JSONResponse:
        """自动运行开关：{enabled: true/false}。AI 自主观察、调整设备并发言；摄像头/麦克风跟随启停。"""
        enabled = bool(body.get("enabled"))
        try:
            async with state.timeline_transition_lock:
                await state.loop.set_autopilot(enabled)
                await state.set_sensors(enabled)
                payload = {
                    "ok": True,
                    "autopilot": bool(state.loop.autopilot),
                }
        except (RuntimeError, TypeError, ValueError) as exc:
            return _timeline_error_response(exc)
        await state.broadcast()
        return JSONResponse(payload)

    # ---------- AI 模型配置（设置页填写，保存即生效） ----------
    @app.get("/api/settings/llm")
    async def api_settings_llm() -> JSONResponse:
        llm = cfg.get("llm", {})
        key = str(llm.get("api_key") or "")
        masked = (
            key[:4] + "*" * (len(key) - 8) + key[-4:]
            if len(key) > 12
            else ("*" * len(key) or "")
        )
        return JSONResponse(
            {
                "base_url": str(llm.get("base_url", "")),
                "model": str(llm.get("model", "")),
                "api_key_masked": masked,
                "has_key": bool(key),
                "saved": (PROJECT_ROOT / "config" / "config.yaml").exists(),
            }
        )

    def _patch_llm_text(text: str, api_key: str, base_url: str, model: str) -> str:
        """文本级更新 config.yaml 的 llm 小节（仅 2 空格缩进键），保留其余注释与内容。"""
        lines = text.splitlines()
        out: list[str] = []
        in_llm = False
        for ln in lines:
            if ln and not ln.startswith(" "):
                in_llm = ln.startswith("llm:")
                out.append(ln)
                continue
            if in_llm and ln.startswith("  ") and not ln.startswith("    "):
                key = ln.lstrip().split(":", 1)[0]
                if key == "api_key":
                    out.append(f'  api_key: "{api_key}"')
                    continue
                if key == "base_url":
                    out.append(f"  base_url: {base_url}")
                    continue
                if key == "model":
                    out.append(f"  model: {model}")
                    continue
            out.append(ln)
        return "\n".join(out) + "\n"

    @app.post("/api/settings/llm")
    async def api_settings_llm_save(body: dict) -> JSONResponse:
        """保存 AI 配置并热加载：{api_key, base_url, model}；config.yaml 不存在时自动从示例生成。"""
        api_key = str(body.get("api_key") or "").strip()
        base_url = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        if not base_url or not model:
            return JSONResponse({"error": "地址与模型名不能为空"}, status_code=400)
        owner: _StoryRuntimeOwner | None = None
        planning: tuple[asyncio.Task, ...] = ()
        async with state.timeline_transition_lock:
            if state.timeline_session.to_state().status.value != "idle":
                return _story_transition_error(
                    state,
                    _StoryConflict(
                        "story_runtime_busy", "story runtime is already active"
                    ),
                )
            if (
                state.story_runtime_owner is not None
                and state.story_runtime_owner.kind != "planning"
            ):
                return _story_transition_error(
                    state,
                    _StoryConflict(
                        "story_runtime_busy", "story runtime is already reserved"
                    ),
                )
            if state.story_planning_task is not None:
                planning = state._cancel_story_planning_now()
            owner = state._acquire_story_runtime_owner("llm")
        old = None
        try:
            await state._settle_story_planning(planning)
            async with state.timeline_transition_lock:
                if (
                    state.story_runtime_owner is not owner
                    or state.timeline_session.to_state().status.value != "idle"
                ):
                    raise _StoryConflict(
                        "story_runtime_busy", "story runtime is already active"
                    )
                cfg_path = PROJECT_ROOT / "config" / "config.yaml"
                example = PROJECT_ROOT / "config" / "config.example.yaml"
                if not cfg_path.exists():
                    cfg_path.write_text(
                        example.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                cfg_path.write_text(
                    _patch_llm_text(
                        cfg_path.read_text(encoding="utf-8"),
                        api_key,
                        base_url,
                        model,
                    ),
                    encoding="utf-8",
                )
                # 同步内存并热加载（key 留空时回退环境变量 DGLAB_LLM_API_KEY）
                llm = cfg.setdefault("llm", {})
                llm["api_key"] = api_key or os.environ.get(
                    "DGLAB_LLM_API_KEY", ""
                )
                llm["base_url"] = base_url
                llm["model"] = model
                old = state.llm
                state.llm = LLM(cfg)
                state.loop.llm = state.llm
                signature = state._story_runtime_signature()
                state.chapter_planner = state._build_story_planner(signature)
                state.story_planner_signature = signature
        except _StoryConflict as exc:
            return _story_transition_error(state, exc)
        finally:
            async with state.timeline_transition_lock:
                state._release_story_runtime_owner(owner)
        with contextlib.suppress(Exception):
            await old.client.aclose()
        await state.broadcast()
        return JSONResponse({"ok": True, "model": model})

    @app.post("/api/settings/llm/test")
    async def api_settings_llm_test(body: dict) -> JSONResponse:
        """测试连接：用表单值发一条最小请求（不保存）。"""
        api_key = str(body.get("api_key") or "").strip() or os.environ.get("DGLAB_LLM_API_KEY", "")
        base = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        if not api_key or not base or not model:
            return JSONResponse({"ok": False, "error": "请先填写 API Key、地址与模型名"})
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    f"{base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                )
            if r.status_code == 200:
                return JSONResponse({"ok": True, "detail": "连接成功，模型可用"})
            return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:300]})

    # ---------- 实时推送 ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        state.ws_clients.add(ws)
        state._on_ws_clients_change()
        await state.send_state(ws)
        try:
            while True:
                await ws.receive_text()  # 客户端心跳/忽略
        except WebSocketDisconnect:
            pass
        finally:
            state.ws_clients.discard(ws)
            state._on_ws_clients_change()

    return app


app = make_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        app,
        host=cfg["app"]["host"],
        port=int(cfg["app"]["port"]),
        log_level="info",
    )
