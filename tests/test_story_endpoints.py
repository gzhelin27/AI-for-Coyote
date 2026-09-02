from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import warnings

from docx import Document
import httpx

import backend.main as main_module
from backend.story import (
    AnalysisStore,
    NovelSessionController,
    StoryChapter,
    StoryMap,
    StoryScene,
    StorySourceLoader,
    offline_analysis_key,
)
from backend.story.planner import ChapterPlanner
from backend.story.source_store import PinnedStorySourceStore
from backend.timeline.models import CycleGapPolicy
from tests.test_session_endpoints import CapturingWebSocket, make_endpoint_state
from tests.test_game_loop_timeline import make_game_loop_for_test


class RaisingStructuredClient:
    """A bounded structured seam whose calls are visible to endpoint tests."""

    model = "model-one"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> dict:
        self.call_count += 1
        return _chapter_response(user_content)


def _chapter_response(user_content: str) -> dict:
    request = json.loads(user_content)
    return {
        "scenes": [
            {
                "scene_id": scene["scene_id"],
                "channels": {
                    "A": {"mode": "keep"},
                    "B": {"mode": "keep"},
                },
            }
            for scene in request["scenes"]
        ]
    }


class BlockingStructuredClient:
    model = "model-one"

    def __init__(self) -> None:
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> dict:
        self.call_count += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return _chapter_response(user_content)


class CloseAwareStructuredClient(RaisingStructuredClient):
    def __init__(self, *, blocked: bool = False) -> None:
        super().__init__()
        self.client = self
        self.closed = False
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def aclose(self) -> None:
        self.closed = True

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> dict:
        self.call_count += 1
        if self.blocked:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.closed:
            raise RuntimeError("closed-client-secret")
        return _chapter_response(user_content)


class ChangingStrengthClient(RaisingStructuredClient):
    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> dict:
        self.call_count += 1
        request = json.loads(user_content)
        strength = 80 if self.call_count == 1 else 20
        return {
            "scenes": [
                {
                    "scene_id": scene["scene_id"],
                    "channels": {
                        "A": {"mode": "set", "base_strength": strength},
                        "B": {"mode": "keep"},
                    },
                }
                for scene in request["scenes"]
            ]
        }


class BlockingFirstWebSocket(CapturingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def send_json(self, message):
        if not self.messages:
            self.first_started.set()
            await self.release_first.wait()
        self.messages.append(message)


class StalledStorySourceStore:
    def __init__(
        self,
        delegate,
        *,
        fallback_timeout: float = 0.5,
        delete_error: Exception | None = None,
    ) -> None:
        self.delegate = delegate
        self.fallback_timeout = fallback_timeout
        self.delete_error = delete_error
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def store(self, story):
        self.started.set()
        self.release.wait(self.fallback_timeout)
        return self.delegate.store(story)

    def delete(self, stored) -> None:
        self.delegate.delete(stored)
        if self.delete_error is not None:
            raise self.delete_error

    def close(self) -> None:
        self.closed.set()
        self.delegate.close()


class StalledDeleteStorySourceStore:
    def __init__(self, delegate, *, fallback_timeout: float = 0.75) -> None:
        self.delegate = delegate
        self.fallback_timeout = fallback_timeout
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.delete_started = threading.Event()
        self.delete_release = threading.Event()
        self.delete_finished = threading.Event()
        self.closed = threading.Event()
        self.closed_before_delete_finished = False

    def store(self, story):
        self.store_started.set()
        self.store_release.wait(self.fallback_timeout)
        return self.delegate.store(story)

    def delete(self, stored) -> None:
        self.delete_started.set()
        self.delete_release.wait(self.fallback_timeout)
        try:
            self.delegate.delete(stored)
        finally:
            self.delete_finished.set()

    def close(self) -> None:
        if not self.delete_finished.is_set():
            self.closed_before_delete_finished = True
        self.closed.set()
        self.delegate.close()


class StalledAnalysisStore:
    def __init__(self, delegate, *, fallback_timeout: float = 0.75) -> None:
        self.delegate = delegate
        self.fallback_timeout = fallback_timeout
        self.inspect_started = threading.Event()
        self.inspect_release = threading.Event()
        self.inspect_finished = threading.Event()

    def inspect(self, key):
        self.inspect_started.set()
        self.inspect_release.wait(self.fallback_timeout)
        try:
            return self.delegate.inspect(key)
        finally:
            self.inspect_finished.set()


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _story_map(story, *, two_chapters: bool = False) -> StoryMap:
    source_hash = story.source_sha256
    bounds = (0, len(story.text))
    if two_chapters:
        midpoint = len(story.text) // 2
        bounds = (0, midpoint, len(story.text))
    chapters = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
        scene = StoryScene(
            id=StoryScene.stable_id(source_hash, index, 0),
            index=0,
            start_offset=start,
            end_offset=end,
            summary=f"第{index + 1}个场景。",
            pace=1.0,
        )
        chapters.append(
            StoryChapter(
                id=StoryChapter.stable_id(source_hash, index),
                index=index,
                start_offset=start,
                end_offset=end,
                title=f"第{index + 1}章",
                summary=f"第{index + 1}章摘要。",
                scenes=(scene,),
            )
        )
    return StoryMap(
        source_hash=source_hash,
        text_length=len(story.text),
        chapters=tuple(chapters),
    )


class StoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = make_game_loop_for_test(
            self.root / "replays", autopilot_interval=3600
        )
        self.state = make_endpoint_state(self.harness)
        self.state.story_import_directory = self.root / "stories"
        self.state.story_source_store = PinnedStorySourceStore(
            self.state.story_import_directory, project_root=self.root
        )
        self.addCleanup(self.state.story_source_store.close)
        self.state.story_source_loader = StorySourceLoader(max_bytes=1024 * 1024)
        self.state.story_source_max_bytes = 1024 * 1024
        self.state.story_analysis_store = AnalysisStore(self.root / "analysis")
        self.addCleanup(self.state.story_analysis_store.close)
        self.state.story_sources = {}
        self.state.active_story_source_id = None
        self.state.story_source_generation = 0
        self.state.story_planning_task = None
        self.state.story_planning_context = None
        self.state.story_runtime_owner = None
        self.state.story_source_store_task = None
        self.state.story_source_io_tasks = set()
        self.state.story_cleanup_tasks = set()
        self.state.story_import_requests = set()
        self.state.story_shutting_down = False
        self.state.story_dlc_version = main_module.dlc_provenance(
            self.state.cfg,
            project_root=main_module.PROJECT_ROOT,
            waveform_policy=self.harness.controller.waveform_policy,
        )
        self.state.broadcast_lock = asyncio.Lock()
        self.state.state_revision = 0
        self.state.story_seed_factory = lambda: 71
        self.llm = RaisingStructuredClient()
        self.state.llm = self.llm
        self.state.chapter_planner = ChapterPlanner(
            self.llm,
            waveform_registry=self.harness.safety.presets,
            effective_caps={
                channel: self.harness.safety.cap_for(channel)
                for channel in ("A", "B")
            },
            reading_speed_cpm={"slow": 250, "standard": 400, "fast": 600},
            cycle_gap_policy=CycleGapPolicy(),
            safety_adapter=self.harness.safety,
            model_identity=self.llm.model,
            prompt_version="faithful-offline-v1",
            dlc_version=self.state.story_dlc_version,
        )
        self.state.novel_session = NovelSessionController(
            self.harness.controller,
            source_encoding="auto",
            analysis_version="faithful-offline-v1",
            dlc_version=self.state.story_dlc_version,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with (
                patch.object(main_module, "load_config", return_value=self.harness.cfg),
                patch.object(main_module, "AppState", return_value=self.state),
            ):
                self.app = main_module.make_app()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.app, raise_app_exceptions=False
            ),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        set_autopilot = self.harness.loop.set_autopilot
        with suppress(Exception):
            if asyncio.iscoroutinefunction(set_autopilot):
                await set_autopilot(False)
            else:
                set_autopilot(False)
        with suppress(Exception):
            await self.harness.controller.stop()

    async def _import(
        self, filename: str, source_bytes: bytes, encoding: str
    ) -> httpx.Response:
        return await self.client.post(
            "/api/story/import",
            data={"encoding": encoding},
            files={"file": (filename, source_bytes, "application/octet-stream")},
        )

    async def _wait_for_thread_event(self, event: threading.Event) -> None:
        deadline = asyncio.get_running_loop().time() + 0.75
        while not event.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("background story store did not start")
            await asyncio.sleep(0.001)

    async def _wait_for_condition(self, predicate, message: str) -> None:
        deadline = asyncio.get_running_loop().time() + 0.75
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(message)
            await asyncio.sleep(0.001)

    def _stall_story_store(self) -> StalledStorySourceStore:
        stalled = StalledStorySourceStore(self.state.story_source_store)
        self.state.story_source_store = stalled
        return stalled

    def _load_story(self, filename: str, source_bytes: bytes, encoding: str):
        return self.state.story_source_loader.load(
            filename, source_bytes, encoding=encoding
        )

    def _save_analysis(self, story, *, two_chapters: bool = False) -> StoryMap:
        story_map = _story_map(story, two_chapters=two_chapters)
        self.state.story_analysis_store.save(
            offline_analysis_key(story, self.state.story_dlc_version), story_map
        )
        return story_map

    def _replace_planner_client(self, client) -> None:
        self.llm = client
        self.state.llm = client
        self.state.chapter_planner = ChapterPlanner(
            client,
            waveform_registry=self.harness.safety.presets,
            effective_caps={
                channel: self.harness.safety.cap_for(channel)
                for channel in ("A", "B")
            },
            reading_speed_cpm={"slow": 250, "standard": 400, "fast": 600},
            cycle_gap_policy=CycleGapPolicy(),
            safety_adapter=self.harness.safety,
            model_identity=client.model,
            prompt_version="faithful-offline-v1",
            dlc_version=self.state.story_dlc_version,
        )

    async def test_import_accepts_txt_md_docx_and_records_selected_encoding(self):
        cases = (
            ("story.txt", "纯爱文本".encode("utf-8"), "utf-8", ".txt"),
            ("story.md", "# 标题\n正文".encode("gb18030"), "gb18030", ".md"),
            ("story.docx", _docx_bytes("文档正文"), "auto", ".docx"),
        )

        for filename, source_bytes, encoding, extension in cases:
            with self.subTest(filename=filename):
                response = await self._import(filename, source_bytes, encoding)

                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["source"]["filename"], filename)
                self.assertEqual(payload["source"]["extension"], extension)
                self.assertEqual(payload["source"]["encoding"], encoding)
                self.assertRegex(payload["source"]["source_id"], r"^[A-Za-z0-9_-]{20,}$")
                self.assertEqual(payload["analysis"]["status"], "missing")
                self.assertNotIn("path", json.dumps(payload).lower())

        self.assertEqual(self.llm.call_count, 0)

    async def test_import_commits_through_the_pinned_source_store(self):
        real_store = self.state.story_source_store.store
        self.state.story_source_store.store = MagicMock(wraps=real_store)

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 200, response.text)
        self.state.story_source_store.store.assert_called_once()
        source_id = response.json()["source"]["source_id"]
        self.assertEqual(
            sorted(path.name for path in self.state.story_import_directory.iterdir()),
            [f"{source_id}.txt"],
        )

    async def test_stalled_store_keeps_loop_callback_and_disconnect_responsive(self):
        stalled = self._stall_story_store()
        callback_ran = asyncio.Event()

        async def disconnect_after_store_starts():
            await self._wait_for_thread_event(stalled.started)
            await main_module.AppState.on_relay_event(
                self.state, "client_disconnected", {}
            )

        started_at = time.perf_counter()
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))
        disconnect = asyncio.create_task(disconnect_after_store_starts())
        asyncio.get_running_loop().call_soon(callback_ran.set)
        await asyncio.wait_for(disconnect, timeout=0.75)
        elapsed = time.perf_counter() - started_at
        callback_completed = callback_ran.is_set()
        import_was_pending = not importing.done()
        stalled.release.set()
        imported = await asyncio.wait_for(importing, timeout=0.75)

        self.assertLess(elapsed, 0.25)
        self.assertTrue(callback_completed)
        self.assertTrue(import_was_pending)
        self.assertEqual(imported.status_code, 200, imported.text)

    async def test_repeated_cancel_while_final_release_waits_on_lock_eventually_clears_owner(self):
        mutation_started = asyncio.Event()
        release_mutation = asyncio.Event()
        holder_acquired = asyncio.Event()
        release_holder = asyncio.Event()

        async def controlled_cap(_channel: str, _value: int):
            mutation_started.set()
            await release_mutation.wait()
            return {"dropped": False}

        async def hold_transition_lock():
            async with self.state.timeline_transition_lock:
                holder_acquired.set()
                await release_holder.wait()

        self.state.loop.set_runtime_cap = controlled_cap
        changing = asyncio.create_task(
            self.client.post(
                "/api/device/channels/cap", json={"channel": "A", "value": 30}
            )
        )
        await asyncio.wait_for(mutation_started.wait(), timeout=0.2)
        holder = asyncio.create_task(hold_transition_lock())
        release_mutation.set()
        await asyncio.wait_for(holder_acquired.wait(), timeout=0.2)

        changing.cancel()
        changing.cancel()
        changing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await changing
        self.assertIsNotNone(self.state.story_runtime_owner)

        release_holder.set()
        await asyncio.wait_for(holder, timeout=0.2)
        await self._wait_for_condition(
            lambda: (
                self.state.story_runtime_owner is None
                and not self.state.story_cleanup_tasks
            ),
            "cancelled endpoint permanently leaked its runtime owner",
        )
        self.assertEqual(self.state.story_cleanup_tasks, set())

    async def test_cancelled_play_cleanup_cannot_be_interrupted_waiting_for_lock(self):
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        await self.state.timeline_transition_lock.acquire()
        try:
            play.cancel()
            await asyncio.sleep(0)
            play.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await play
        finally:
            self.state.timeline_transition_lock.release()

        await asyncio.sleep(0.05)
        cleanup_settled = (
            self.state.story_runtime_owner is None
            and self.state.story_planning_task is None
            and not self.state.story_cleanup_tasks
        )
        client.release.set()
        async with self.state.timeline_transition_lock:
            pending = self.state._cancel_story_planning_now()
        await self.state._settle_story_planning(pending)

        self.assertTrue(
            cleanup_settled,
            "cancelled play leaked its planning task/runtime owner",
        )
        self.assertTrue(client.cancelled.is_set())

    async def test_shutdown_waits_for_cancel_cleanup_delete_before_closing_store(self):
        stalled = StalledDeleteStorySourceStore(self.state.story_source_store)
        self.state.story_source_store = stalled
        self.state.shutdown = main_module.AppState.shutdown.__get__(
            self.state, main_module.AppState
        )
        self.state._shutdown_cleanup = main_module.AppState._shutdown_cleanup.__get__(
            self.state, main_module.AppState
        )
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))
        await self._wait_for_thread_event(stalled.store_started)
        await self.state.timeline_transition_lock.acquire()
        shutdown = None
        try:
            importing.cancel()
            stalled.store_release.set()
            await self._wait_for_thread_event(stalled.delete_started)
            importing.cancel()
            await asyncio.sleep(0)
            importing.cancel()
            await asyncio.sleep(0)
            if not importing.done():
                importing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await importing

            shutdown = asyncio.create_task(self.state.shutdown())
            self.state.timeline_transition_lock.release()
            await asyncio.sleep(0.05)
            shutdown_was_pending = not shutdown.done()
            closed_early = stalled.closed.is_set()
        finally:
            if self.state.timeline_transition_lock.locked():
                self.state.timeline_transition_lock.release()
            stalled.delete_release.set()
            if shutdown is not None:
                await asyncio.wait_for(shutdown, timeout=0.75)

        self.assertTrue(shutdown_was_pending)
        self.assertFalse(closed_early)
        self.assertFalse(stalled.closed_before_delete_finished)
        self.assertTrue(stalled.delete_finished.is_set())
        self.assertTrue(stalled.closed.is_set())
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)
        self.assertEqual(self.state.story_cleanup_tasks, set())
        self.assertEqual(self.state.story_source_io_tasks, set())

    async def test_cancelled_stalled_import_deletes_committed_orphan_and_clears_tasks(self):
        stalled = self._stall_story_store()
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))
        await self._wait_for_thread_event(stalled.started)

        importing.cancel()
        stalled.release.set()
        was_cancelled = False
        try:
            await asyncio.wait_for(importing, timeout=0.75)
        except asyncio.CancelledError:
            was_cancelled = True

        await self._wait_for_condition(
            lambda: (
                self.state.story_runtime_owner is None
                and not self.state.story_cleanup_tasks
                and not self.state.story_source_io_tasks
            ),
            "cancelled store cleanup did not settle",
        )
        self.assertTrue(was_cancelled)
        self.assertEqual(self.state.story_sources, {})
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)
        self.assertIsNone(getattr(self.state, "story_source_store_task", None))
        self.assertFalse(
            [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and task.get_name().startswith("story-source-store-")
            ]
        )

    async def test_cleanup_exception_cannot_steal_import_owner_or_leak_task(self):
        stalled = StalledStorySourceStore(
            self.state.story_source_store,
            delete_error=RuntimeError("private cleanup detail"),
        )
        self.state.story_source_store = stalled
        self.state.logger.exception = MagicMock()
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))
        await self._wait_for_thread_event(stalled.started)

        importing.cancel()
        stalled.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(importing, timeout=0.75)

        await self._wait_for_condition(
            lambda: (
                self.state.story_runtime_owner is None
                and not self.state.story_cleanup_tasks
                and not self.state.story_source_io_tasks
            ),
            "failing delete cleanup did not settle",
        )
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)
        self.assertIsNone(self.state.story_source_store_task)
        self.assertEqual(self.state.story_import_requests, set())
        self.state.logger.exception.assert_called_once_with(
            "failed to remove unregistered story source"
        )

    async def test_unexpected_store_exception_is_generic_500_and_clears_owner(self):
        self.state.story_source_store.store = MagicMock(
            side_effect=RuntimeError("C:\\private\\source-secret.txt")
        )
        self.state.logger.exception = MagicMock()

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertNotIn("private", response.text.lower())
        self.assertNotIn("secret", response.text.lower())
        self.assertIsNone(self.state.story_runtime_owner)
        self.assertIsNone(self.state.story_source_store_task)
        self.assertEqual(self.state.story_import_requests, set())
        self.state.logger.exception.assert_called_once_with(
            "unexpected story source store failure"
        )

    async def test_inspect_exception_never_publishes_ghost_source(self):
        self.state.story_analysis_store.inspect = MagicMock(
            side_effect=RuntimeError("C:\\private\\analysis-secret.json")
        )
        self.state.logger.exception = MagicMock()

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertNotIn("private", response.text.lower())
        self.assertEqual(self.state.story_sources, {})
        self.assertIsNone(self.state.active_story_source_id)
        self.assertEqual(self.state.story_source_generation, 0)
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_provenance_exception_never_publishes_ghost_source(self):
        self.state.logger.exception = MagicMock()

        with patch.object(
            main_module,
            "dlc_provenance",
            side_effect=RuntimeError("C:\\private\\provenance-secret"),
        ):
            response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertNotIn("private", response.text.lower())
        self.assertEqual(self.state.story_sources, {})
        self.assertIsNone(self.state.active_story_source_id)
        self.assertEqual(self.state.story_source_generation, 0)
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_cancel_during_analysis_inspect_never_publishes_source(self):
        stalled = StalledAnalysisStore(self.state.story_analysis_store)
        self.state.story_analysis_store = stalled
        loop = asyncio.get_running_loop()
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))

        def cancel_during_inspect() -> None:
            if stalled.inspect_started.wait(0.75):
                loop.call_soon_threadsafe(importing.cancel)
            stalled.inspect_release.set()

        coordinator = threading.Thread(target=cancel_during_inspect)
        coordinator.start()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(importing, timeout=1.5)
        finally:
            stalled.inspect_release.set()
            coordinator.join(timeout=0.75)

        await self._wait_for_condition(
            lambda: (
                self.state.story_runtime_owner is None
                and not self.state.story_cleanup_tasks
                and not self.state.story_source_io_tasks
            ),
            "analysis cancellation cleanup did not settle",
        )
        self.assertEqual(self.state.story_sources, {})
        self.assertIsNone(self.state.active_story_source_id)
        self.assertEqual(self.state.story_source_generation, 0)
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])

    async def test_cancel_waiting_to_commit_never_publishes_source(self):
        stalled = StalledAnalysisStore(self.state.story_analysis_store)
        self.state.story_analysis_store = stalled
        loop = asyncio.get_running_loop()
        release_holder = asyncio.Event()
        holder_acquired = threading.Event()
        holder_tasks: list[asyncio.Task] = []
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))

        async def hold_transition_lock() -> None:
            async with self.state.timeline_transition_lock:
                holder_acquired.set()
                await release_holder.wait()

        def create_holder() -> None:
            holder_tasks.append(asyncio.create_task(hold_transition_lock()))

        def cancel_after_inspect() -> None:
            if not stalled.inspect_started.wait(0.75):
                stalled.inspect_release.set()
                return
            loop.call_soon_threadsafe(create_holder)
            holder_acquired.wait(0.2)
            stalled.inspect_release.set()
            stalled.inspect_finished.wait(0.75)
            loop.call_soon_threadsafe(importing.cancel)

        coordinator = threading.Thread(target=cancel_after_inspect)
        coordinator.start()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(importing, timeout=1.5)
        finally:
            stalled.inspect_release.set()
            release_holder.set()
            coordinator.join(timeout=0.75)
            if holder_tasks:
                await asyncio.gather(*holder_tasks, return_exceptions=True)

        await self._wait_for_condition(
            lambda: (
                self.state.story_runtime_owner is None
                and not self.state.story_cleanup_tasks
                and not self.state.story_source_io_tasks
            ),
            "commit cancellation cleanup did not settle",
        )
        self.assertEqual(self.state.story_sources, {})
        self.assertIsNone(self.state.active_story_source_id)
        self.assertEqual(self.state.story_source_generation, 0)
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])

    async def test_partial_commit_exception_rolls_back_memory_and_file(self):
        class InsertThenRaise(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                raise RuntimeError("C:\\private\\commit-secret")

        self.state.story_sources = InsertThenRaise()
        self.state.logger.exception = MagicMock()

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertNotIn("private", response.text.lower())
        self.assertEqual(self.state.story_sources, {})
        self.assertIsNone(self.state.active_story_source_id)
        self.assertEqual(self.state.story_source_generation, 0)
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_broadcast_exception_keeps_committed_source_consistent(self):
        self.state.broadcast = AsyncMock(
            side_effect=RuntimeError("C:\\private\\broadcast-secret")
        )
        self.state.logger.exception = MagicMock()

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertNotIn("private", response.text.lower())
        self.assertEqual(len(self.state.story_sources), 1)
        source_id = self.state.active_story_source_id
        self.assertIn(source_id, self.state.story_sources)
        self.assertEqual(self.state.story_source_generation, 1)
        self.assertEqual(
            sorted(path.name for path in self.state.story_import_directory.iterdir()),
            [f"{source_id}.txt"],
        )
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_shutdown_runs_safety_before_settling_stalled_store_then_cleans_orphan(self):
        stalled = self._stall_story_store()
        safety_progress = asyncio.Event()

        async def stop_sensors(_enabled: bool) -> None:
            safety_progress.set()

        self.state.set_sensors = AsyncMock(side_effect=stop_sensors)
        self.state.shutdown = main_module.AppState.shutdown.__get__(
            self.state, main_module.AppState
        )
        self.state._shutdown_cleanup = main_module.AppState._shutdown_cleanup.__get__(
            self.state, main_module.AppState
        )

        async def shutdown_after_store_starts():
            await self._wait_for_thread_event(stalled.started)
            await self.state.shutdown()

        started_at = time.perf_counter()
        importing = asyncio.create_task(self._import("story.txt", b"ABCD", "utf-8"))
        shutdown = asyncio.create_task(shutdown_after_store_starts())

        await asyncio.wait_for(safety_progress.wait(), timeout=0.75)
        elapsed = time.perf_counter() - started_at
        shutdown_was_pending = not shutdown.done()
        stalled.release.set()
        imported = await asyncio.wait_for(importing, timeout=0.75)
        await asyncio.wait_for(shutdown, timeout=0.75)

        self.assertLess(elapsed, 0.25)
        self.assertTrue(shutdown_was_pending)
        self.assertEqual(imported.status_code, 409, imported.text)
        self.assertEqual(imported.json()["code"], "story_runtime_busy")
        self.assertEqual(self.state.story_sources, {})
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertTrue(stalled.closed.is_set())
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_import_rejects_unsupported_and_oversize_without_storing_a_source(self):
        unsupported = await self._import("secret.pdf", b"not a novel", "auto")
        self.state.story_source_loader = StorySourceLoader(max_bytes=4)
        self.state.story_source_max_bytes = 4
        oversized = await self._import("large.txt", b"12345", "utf-8")

        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.json()["code"], "story_import_invalid")
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(oversized.json()["code"], "story_import_invalid")
        self.assertEqual(self.state.story_sources, {})
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertEqual(self.llm.call_count, 0)

    async def test_source_ids_are_opaque_isolated_and_never_resolve_caller_paths(self):
        first = await self._import("same.txt", b"ABCD", "utf-8")
        second = await self._import("same.txt", b"WXYZ", "utf-8")
        first_id = first.json()["source"]["source_id"]
        second_id = second.json()["source"]["source_id"]

        self.assertNotEqual(first_id, second_id)
        self.assertNotIn("same", first_id)
        self.assertNotIn(first.json()["source"]["hash_prefix"], first_id)
        missing = await self.client.get("/api/story/not-a-server-source/analysis")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "story_not_found")
        stored = sorted(self.state.story_import_directory.iterdir())
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(item.parent == self.state.story_import_directory for item in stored))
        self.assertEqual(self.llm.call_count, 0)

    async def test_import_maps_pinned_store_failure_without_publishing_source(self):
        self.state.story_source_store.store = MagicMock(
            side_effect=main_module.StorySourceStorageError("private path")
        )

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertEqual(self.state.story_sources, {})
        self.assertNotIn("private", response.text.lower())
        self.assertEqual(list(self.state.story_import_directory.iterdir()), [])
        self.assertEqual(self.llm.call_count, 0)

    async def test_missing_analysis_never_calls_llm(self):
        imported = await self._import("story.txt", b"ABCD", "utf-8")
        source_id = imported.json()["source"]["source_id"]

        response = await self.client.get(f"/api/story/{source_id}/analysis")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "analysis_missing")
        self.assertEqual(response.json()["status"], "missing")
        self.assertEqual(response.json()["hash_prefix"], imported.json()["source"]["hash_prefix"])
        self.assertEqual(response.json()["analysis_version"], "faithful-offline-v1")
        self.assertEqual(response.json()["dlc_version"], self.state.story_dlc_version)
        self.assertEqual(self.llm.call_count, 0)

    async def test_state_snapshot_stalled_analysis_keeps_event_loop_responsive(self):
        await self._import("story.txt", b"ABCD", "utf-8")
        stalled = StalledAnalysisStore(
            self.state.story_analysis_store, fallback_timeout=0.25
        )
        self.state.story_analysis_store = stalled
        ticked_at: list[float] = []
        started_at = time.perf_counter()

        async def tick() -> None:
            await asyncio.sleep(0.01)
            ticked_at.append(time.perf_counter())

        response, _ = await asyncio.gather(
            self.client.get("/api/state"), tick()
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(stalled.inspect_finished.is_set())
        self.assertLess(ticked_at[0] - started_at, 0.1)
        self.assertFalse(self.state.story_source_io_tasks)

    async def _assert_stalled_state_inspection_rejects_runtime_change(
        self, mutate_runtime
    ) -> None:
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        self._save_analysis(story)
        ready = await self.client.get(f"/api/story/{source_id}/analysis")
        self.assertEqual(ready.status_code, 200, ready.text)
        stalled = StalledAnalysisStore(self.state.story_analysis_store)
        self.state.story_analysis_store = stalled
        request = asyncio.create_task(self.client.get("/api/state"))
        try:
            self.assertTrue(
                await asyncio.to_thread(stalled.inspect_started.wait, 0.5)
            )
            mutation = mutate_runtime()
            if asyncio.iscoroutine(mutation):
                await mutation
        finally:
            stalled.inspect_release.set()
        response = await asyncio.wait_for(request, timeout=1.0)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["story"]["analysis"]["status"], "missing"
        )

    async def test_stalled_state_inspection_rejects_concurrent_profile_change(self):
        await self._assert_stalled_state_inspection_rejects_runtime_change(
            lambda: self.state.cfg["character"].__setitem__(
                "profile", "concurrent-profile"
            )
        )

    async def test_stalled_state_inspection_rejects_concurrent_dlc_change(self):
        await self._assert_stalled_state_inspection_rejects_runtime_change(
            lambda: self.state.cfg["character"].__setitem__(
                "prompt", "concurrent DLC prompt"
            )
        )

    async def test_stalled_state_inspection_rejects_concurrent_llm_change(self):
        def replace_llm() -> None:
            self.state.llm = SimpleNamespace(model="model-two", client=object())

        await self._assert_stalled_state_inspection_rejects_runtime_change(
            replace_llm
        )

    async def test_stalled_state_inspection_rejects_cap_change_even_after_aba(self):
        original_cap = self.state.safety.user_caps["A"]

        async def change_and_restore_cap() -> None:
            changed = await self.client.post(
                "/api/device/channels/cap", json={"channel": "A", "value": 30}
            )
            restored = await self.client.post(
                "/api/device/channels/cap",
                json={"channel": "A", "value": original_cap},
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(restored.status_code, 200, restored.text)

        await self._assert_stalled_state_inspection_rejects_runtime_change(
            change_and_restore_cap
        )

    async def test_stalled_state_inspection_rejects_concurrent_waveform_change(self):
        def add_waveform() -> None:
            existing = next(iter(self.state.safety.presets.values()))
            self.state.safety.presets["concurrent-waveform"] = dict(existing)

        await self._assert_stalled_state_inspection_rejects_runtime_change(
            add_waveform
        )

    async def test_play_rejects_stale_inspection_without_overwriting_current_dlc(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        stalled = StalledAnalysisStore(self.state.story_analysis_store)
        self.state.story_analysis_store = stalled
        playing = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        try:
            self.assertTrue(
                await asyncio.to_thread(stalled.inspect_started.wait, 0.5)
            )
            self.state.cfg["character"]["profile"] = "new-profile"
            current_dlc = main_module.dlc_provenance(
                self.state.cfg,
                project_root=main_module.PROJECT_ROOT,
                waveform_policy=self.state.timeline_session.waveform_policy,
            )
            self.state.story_dlc_version = current_dlc
        finally:
            stalled.inspect_release.set()
        response = await asyncio.wait_for(playing, timeout=1.0)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "story_state_changed")
        self.assertEqual(self.state.story_dlc_version, current_dlc)
        self.assertEqual(self.llm.call_count, 0)
        self.assertEqual(self.state.novel_session.to_state().status.value, "idle")

    async def test_play_stalled_analysis_never_holds_transition_lock(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        stalled = StalledAnalysisStore(
            self.state.story_analysis_store, fallback_timeout=0.25
        )
        self.state.story_analysis_store = stalled
        started_at = time.perf_counter()
        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )

        self.assertTrue(
            await asyncio.to_thread(stalled.inspect_started.wait, 0.5)
        )
        async with asyncio.timeout(0.1):
            async with self.state.timeline_transition_lock:
                lock_acquired_at = time.perf_counter()
        stalled.inspect_release.set()

        response = await asyncio.wait_for(play, timeout=1.0)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(lock_acquired_at - started_at, 0.1)
        self.assertFalse(self.state.story_source_io_tasks)

    async def test_play_provenance_and_analysis_io_never_run_on_loop_or_under_lock(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        event_loop_thread = threading.get_ident()
        original_provenance = main_module.dlc_provenance
        observations: list[tuple[int, bool]] = []

        def guarded_provenance(*args, **kwargs):
            observation = (
                threading.get_ident(),
                self.state.timeline_transition_lock.locked(),
            )
            observations.append(observation)
            if observation[0] == event_loop_thread or observation[1]:
                raise AssertionError("story provenance I/O crossed the short lock")
            return original_provenance(*args, **kwargs)

        with patch.object(
            main_module, "dlc_provenance", side_effect=guarded_provenance
        ):
            response = await self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(len(observations), 2)
        self.assertTrue(all(thread_id != event_loop_thread for thread_id, _ in observations))
        self.assertTrue(all(not locked for _, locked in observations))

    async def test_corrupt_analysis_is_invalid_once_then_missing_without_llm(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        key = offline_analysis_key(story, self.state.story_dlc_version)
        cache_path = self.state.story_analysis_store.cache_path(key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{broken", encoding="utf-8")

        invalid = await self.client.get(f"/api/story/{source_id}/analysis")
        missing = await self.client.get(f"/api/story/{source_id}/analysis")

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "analysis_invalid")
        self.assertEqual(invalid.json()["status"], "invalid")
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["code"], "analysis_missing")
        self.assertEqual(self.llm.call_count, 0)

    async def test_import_invalid_event_is_not_shared_with_broadcast_or_get(self):
        source_bytes = b"ABCD"
        story = self._load_story("story.txt", source_bytes, "utf-8")
        cache_path = self.state.story_analysis_store.cache_path(
            offline_analysis_key(story, self.state.story_dlc_version)
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{broken", encoding="utf-8")
        socket = BlockingFirstWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )

        importing = asyncio.create_task(
            self._import("story.txt", source_bytes, "utf-8")
        )
        await asyncio.wait_for(socket.first_started.wait(), timeout=0.2)
        source_id = self.state.active_story_source_id
        concurrent = await self.client.get(
            f"/api/story/{source_id}/analysis"
        )
        socket.release_first.set()
        imported = await asyncio.wait_for(importing, timeout=0.2)

        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["analysis"]["status"], "invalid")
        self.assertEqual(concurrent.status_code, 409)
        self.assertEqual(concurrent.json()["code"], "analysis_missing")
        self.assertEqual(
            socket.messages[0]["data"]["story"]["analysis"]["status"],
            "missing",
        )

    async def test_concurrent_corrupt_gets_publish_only_one_invalid_event(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        cache_path = self.state.story_analysis_store.cache_path(
            offline_analysis_key(story, self.state.story_dlc_version)
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{broken", encoding="utf-8")

        first, second = await asyncio.gather(
            self.client.get(f"/api/story/{source_id}/analysis"),
            self.client.get(f"/api/story/{source_id}/analysis"),
        )

        self.assertEqual(sorted((first.status_code, second.status_code)), [409, 422])
        self.assertEqual(
            sorted((first.json()["code"], second.json()["code"])),
            ["analysis_invalid", "analysis_missing"],
        )

    async def test_ready_analysis_and_chapter_listing_are_public_and_model_free(self):
        source_bytes = b"ABCDWXYZ"
        imported = await self._import("story.md", source_bytes, "auto")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.md", source_bytes, "auto")
        story_map = self._save_analysis(story, two_chapters=True)

        status = await self.client.get(f"/api/story/{source_id}/analysis")
        chapters = await self.client.get(f"/api/story/{source_id}/chapters")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "ready")
        self.assertEqual(chapters.status_code, 200)
        self.assertEqual(
            [item["chapter_id"] for item in chapters.json()["chapters"]],
            [chapter.id for chapter in story_map.chapters],
        )
        self.assertEqual(
            [item["title"] for item in chapters.json()["chapters"]],
            ["第1章", "第2章"],
        )
        self.assertNotIn("ABCD", chapters.text)
        self.assertNotIn("WXYZ", chapters.text)
        self.assertEqual(self.llm.call_count, 0)

    async def test_chapters_propagate_missing_and_invalid_business_errors_without_llm(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")

        missing = await self.client.get(f"/api/story/{source_id}/chapters")
        key = offline_analysis_key(story, self.state.story_dlc_version)
        cache_path = self.state.story_analysis_store.cache_path(key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("[]", encoding="utf-8")
        invalid = await self.client.get(f"/api/story/{source_id}/chapters")

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["code"], "analysis_missing")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "analysis_invalid")
        self.assertEqual(self.llm.call_count, 0)

    async def test_reader_returns_only_strictly_bounded_source_slices_without_llm(self):
        source_bytes = ("0123456789" * 900).encode("utf-8")
        imported = await self._import("story.txt", source_bytes, "utf-8")
        story = self._load_story("story.txt", source_bytes, "utf-8")
        self._save_analysis(story)

        reader = await self.client.get("/api/story/reader")
        text = await self.client.get("/api/story/reader/text?start=3&end=8")
        reversed_range = await self.client.get(
            "/api/story/reader/text?start=8&end=3"
        )
        out_of_bounds = await self.client.get(
            "/api/story/reader/text?start=0&end=9001"
        )
        too_large = await self.client.get(
            "/api/story/reader/text?start=0&end=8193"
        )

        self.assertEqual(reader.status_code, 200)
        self.assertEqual(
            reader.json()["source"]["source_id"],
            imported.json()["source"]["source_id"],
        )
        self.assertNotIn("0123456789", reader.text)
        self.assertEqual(
            text.json(),
            {"start": 3, "end": 8, "text_length": 9000, "text": "34567"},
        )
        for response in (reversed_range, out_of_bounds, too_large):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "reader_range_invalid")
        self.assertEqual(self.llm.call_count, 0)

    async def test_reader_propagates_offline_analysis_errors_without_llm(self):
        source_bytes = b"ABCD"
        await self._import("story.txt", source_bytes, "utf-8")
        story = self._load_story("story.txt", source_bytes, "utf-8")

        missing = await self.client.get("/api/story/reader")
        key = offline_analysis_key(story, self.state.story_dlc_version)
        cache_path = self.state.story_analysis_store.cache_path(key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{broken", encoding="utf-8")
        invalid = await self.client.get(
            "/api/story/reader/text?start=0&end=1"
        )

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["code"], "analysis_missing")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "analysis_invalid")
        self.assertEqual(self.llm.call_count, 0)

    async def test_play_is_the_only_story_route_that_plans_and_archives_import_encoding(self):
        source_bytes = "剧情章节".encode("gb18030")
        imported = await self._import("story.txt", source_bytes, "gb18030")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "gb18030")
        story_map = self._save_analysis(story)

        played = await self.client.post(
            f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
            json={"speed": "standard"},
        )
        paused = await self.client.post("/api/story/pause")
        resumed = await self.client.post(
            "/api/story/resume", json={"from": "current"}
        )
        finished = await self.client.post("/api/story/finish")

        self.assertEqual(played.status_code, 200, played.text)
        self.assertEqual(played.json()["status"], "running")
        self.assertEqual(paused.json()["status"], "paused")
        self.assertEqual(resumed.json()["status"], "running")
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["replay"]["status"], "completed")
        self.assertEqual(finished.json()["session"]["status"], "idle")
        self.assertEqual(self.llm.call_count, 1)
        replay = self.harness.store.load(finished.json()["replay"]["replay_id"])
        self.assertEqual(replay.manifest.metadata["source_encoding"], "gb18030")

    async def test_story_mutations_broadcast_generation_gated_full_state(self):
        socket = CapturingWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )

        imported = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(len(socket.messages), 1)
        message = socket.messages[0]
        self.assertEqual(message["type"], "state")
        self.assertEqual(
            message["data"]["story"]["selected_source"]["source_id"],
            imported.json()["source"]["source_id"],
        )
        self.assertEqual(message["data"]["story"]["analysis"]["status"], "missing")
        self.assertEqual(message["data"]["story"]["session"]["status"], "idle")
        self.assertNotIn("ABCD", json.dumps(message))
        self.assertEqual(self.llm.call_count, 0)

    async def test_import_broadcast_reuses_the_validated_ready_lookup(self):
        source_bytes = b"ABCD"
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        socket = CapturingWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )

        imported = await self._import("story.txt", source_bytes, "utf-8")

        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["analysis"]["status"], "ready")
        self.assertEqual(len(socket.messages), 1)
        story_state = socket.messages[0]["data"]["story"]
        self.assertEqual(story_state["analysis"]["status"], "ready")
        self.assertEqual(
            [chapter["chapter_id"] for chapter in story_state["chapters"]],
            [chapter.id for chapter in story_map.chapters],
        )

    async def test_disconnect_cancels_blocking_planning_without_waiting_for_model(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)

        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)

        await asyncio.wait_for(
            main_module.AppState.on_relay_event(
                self.state, "client_disconnected", {}
            ),
            timeout=0.2,
        )
        response = await asyncio.wait_for(play, timeout=0.2)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "story_planning_cancelled")
        self.assertTrue(client.cancelled.is_set())
        self.assertIsNone(self.state.story_planning_task)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")
        self.assertFalse(
            any(
                not task.done() and task.get_name().startswith("chapter-intent-")
                for task in asyncio.all_tasks()
            )
        )

    async def test_shutdown_cancels_blocking_planning_before_transition_lock(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        self.state.shutdown = main_module.AppState.shutdown.__get__(
            self.state, main_module.AppState
        )
        self.state._shutdown_cleanup = main_module.AppState._shutdown_cleanup.__get__(
            self.state, main_module.AppState
        )

        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)

        shutdown = asyncio.create_task(self.state.shutdown())
        completed, _pending = await asyncio.wait({shutdown}, timeout=0.2)
        if shutdown not in completed:
            client.release.set()
            await asyncio.wait_for(play, timeout=0.2)
            await asyncio.wait_for(shutdown, timeout=0.2)
        self.assertIn(shutdown, completed, "shutdown waited on the blocking planner")
        response = await asyncio.wait_for(play, timeout=0.2)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "story_planning_cancelled")
        self.assertTrue(client.cancelled.is_set())
        self.assertIsNone(self.state.story_planning_task)
        self.assertEqual(self.state.tasks, [])

    async def test_shutdown_clears_before_waiting_for_stalled_novel_archive_save(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        played = await self.client.post(
            f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(played.status_code, 200, played.text)
        original_save = self.harness.store.save
        save_started = threading.Event()
        save_release = threading.Event()

        def stalled_save(*args, **kwargs):
            save_started.set()
            save_release.wait(0.5)
            return original_save(*args, **kwargs)

        self.state.shutdown = main_module.AppState.shutdown.__get__(
            self.state, main_module.AppState
        )
        self.state._shutdown_cleanup = main_module.AppState._shutdown_cleanup.__get__(
            self.state, main_module.AppState
        )

        with patch.object(
            self.harness.store, "save", side_effect=stalled_save
        ):
            finishing = asyncio.create_task(
                self.client.post("/api/story/finish")
            )
            self.assertTrue(await asyncio.to_thread(save_started.wait, 0.5))
            shutdown = asyncio.create_task(self.state.shutdown())
            for _ in range(20):
                if self.harness.safety.estop_active:
                    break
                await asyncio.sleep(0.005)

            self.assertTrue(self.harness.safety.estop_active)
            self.assertTrue(
                self.harness.loop.output_clear_is_confirmed(("A", "B"))
            )
            self.assertFalse(shutdown.done())
            save_release.set()
            finished_response, _ = await asyncio.wait_for(
                asyncio.gather(finishing, shutdown), timeout=0.5
            )

        self.assertEqual(finished_response.status_code, 200, finished_response.text)
        self.assertFalse(self.harness.controller._store_io_tasks)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")

    async def test_planning_result_is_discarded_when_selected_source_changes(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)

        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        self.state.active_story_source_id = None
        client.release.set()
        response = await asyncio.wait_for(play, timeout=0.2)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "story_state_changed")
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")

    async def test_concurrent_play_is_rejected_while_first_plan_is_blocked(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        endpoint = f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play"

        first = asyncio.create_task(
            self.client.post(endpoint, json={"speed": "standard"})
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        second = await asyncio.wait_for(
            self.client.post(endpoint, json={"speed": "fast"}), timeout=0.2
        )

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "story_planning_active")
        self.assertEqual(client.call_count, 1)
        client.release.set()
        first_response = await asyncio.wait_for(first, timeout=0.2)
        self.assertEqual(first_response.status_code, 200, first_response.text)

    async def test_blocked_plan_is_published_as_story_planning_state(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        socket = CapturingWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )
        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "fast"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)

        planning = socket.messages[0]["data"]["story"]["session"]

        self.assertEqual(planning["status"], "planning")
        self.assertEqual(planning["chapter_id"], story_map.chapters[0].id)
        self.assertEqual(planning["speed"], "fast")
        self.assertEqual(planning["hash_prefix"], story.source_sha256[:12])
        self.assertGreaterEqual(socket.messages[0]["data"]["state_revision"], 1)
        client.release.set()
        await asyncio.wait_for(play, timeout=0.2)

    async def test_import_during_planning_is_conflict_and_preserves_selection(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        play = asyncio.create_task(
            self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        before_files = sorted(path.name for path in self.state.story_import_directory.iterdir())

        conflict = await self._import("other.txt", b"WXYZ", "utf-8")

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "story_runtime_busy")
        self.assertEqual(self.state.active_story_source_id, source_id)
        self.assertEqual(
            sorted(path.name for path in self.state.story_import_directory.iterdir()),
            before_files,
        )

    async def test_full_state_broadcasts_are_serialized_with_monotonic_revisions(self):
        socket = BlockingFirstWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )
        self.state.layout = {"marker": 1}

        first = asyncio.create_task(self.state.broadcast())
        await asyncio.wait_for(socket.first_started.wait(), timeout=0.2)
        self.state.layout = {"marker": 2}
        second = asyncio.create_task(self.state.broadcast())
        await asyncio.sleep(0)

        self.assertFalse(second.done())
        socket.release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=0.2)
        revisions_by_marker = {
            message["data"]["layout"]["marker"]: message["data"][
                "state_revision"
            ]
            for message in socket.messages
        }
        self.assertEqual(revisions_by_marker, {1: 1, 2: 2})

    async def test_http_snapshot_gets_unique_revision_while_older_ws_send_is_blocked(self):
        socket = BlockingFirstWebSocket()
        self.state.ws_clients.add(socket)
        self.state.broadcast = main_module.AppState.broadcast.__get__(
            self.state, main_module.AppState
        )
        self.state.send_state = main_module.AppState.send_state.__get__(
            self.state, main_module.AppState
        )
        self.state.layout = {"marker": "old-ws"}

        old_broadcast = asyncio.create_task(self.state.broadcast())
        await asyncio.wait_for(socket.first_started.wait(), timeout=0.2)
        self.state.layout = {"marker": "new-http"}
        http = await asyncio.wait_for(self.client.get("/api/state"), timeout=0.2)
        socket.release_first.set()
        await asyncio.wait_for(old_broadcast, timeout=0.2)

        old_frame = socket.messages[0]["data"]
        new_snapshot = http.json()
        self.assertEqual(old_frame["layout"]["marker"], "old-ws")
        self.assertEqual(new_snapshot["layout"]["marker"], "new-http")
        self.assertLess(
            old_frame["state_revision"], new_snapshot["state_revision"]
        )

        initial = CapturingWebSocket()
        await self.state.send_state(initial)
        self.assertGreater(
            initial.messages[0]["data"]["state_revision"],
            new_snapshot["state_revision"],
        )

    async def test_dlc_change_makes_old_analysis_missing_and_replans_new_identity(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        chapter_id = story_map.chapters[0].id
        first = await self.client.post(
            f"/api/story/{source_id}/chapters/{chapter_id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        await self.client.post("/api/story/finish")
        initial_dlc = self.state.story_dlc_version

        self.state.cfg["character"]["profile"] = "runtime-changed"
        missing = await self.client.get(f"/api/story/{source_id}/analysis")

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["code"], "analysis_missing")
        new_dlc = missing.json()["dlc_version"]
        self.assertNotEqual(new_dlc, initial_dlc)
        self.state.story_analysis_store.save(
            offline_analysis_key(story, new_dlc), story_map
        )
        second = await self.client.post(
            f"/api/story/{source_id}/chapters/{chapter_id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.llm.call_count, 2)
        finished = await self.client.post("/api/story/finish")
        replay = self.harness.store.load(finished.json()["replay"]["replay_id"])
        self.assertEqual(replay.manifest.metadata["dlc_version"], new_dlc)

    async def test_cap_change_rebuilds_planner_and_does_not_reuse_old_intent(self):
        client = ChangingStrengthClient()
        self._replace_planner_client(client)
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        endpoint = f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play"

        first = await self.client.post(endpoint, json={"speed": "standard"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(
            self.state.novel_session.plan.plot_events[0].channels["A"].base_strength,
            80,
        )
        await self.client.post("/api/story/finish")
        cap = await self.client.post(
            "/api/device/channels/cap", json={"channel": "A", "value": 30}
        )
        self.assertEqual(cap.status_code, 200, cap.text)
        second = await self.client.post(endpoint, json={"speed": "standard"})

        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(client.call_count, 2)
        self.assertEqual(
            self.state.novel_session.plan.plot_events[0].channels["A"].base_strength,
            20,
        )

    async def test_cap_change_cancels_and_settles_blocking_planning(self):
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        endpoint = f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play"
        play = asyncio.create_task(
            self.client.post(endpoint, json={"speed": "standard"})
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)

        cap = await asyncio.wait_for(
            self.client.post(
                "/api/device/channels/cap", json={"channel": "A", "value": 30}
            ),
            timeout=0.2,
        )
        cancelled = await asyncio.wait_for(play, timeout=0.2)

        self.assertEqual(cap.status_code, 200, cap.text)
        self.assertEqual(cancelled.status_code, 409, cancelled.text)
        self.assertEqual(cancelled.json()["code"], "story_planning_cancelled")
        self.assertTrue(client.cancelled.is_set())
        self.assertIsNone(self.state.story_planning_task)
        self.assertFalse(
            [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and task.get_name().startswith("story-plan-")
            ]
        )

    async def test_dlc_import_is_conflict_during_live_novel_without_writes(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        played = await self.client.post(
            f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(played.status_code, 200, played.text)
        runtime_root = self.root / "runtime-dlc"

        with patch.object(main_module, "PROJECT_ROOT", runtime_root):
            conflict = await self.client.post(
                "/api/dlc/import",
                files={"file": ("theme.md", b"# DLC", "text/markdown")},
            )

        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["code"], "story_runtime_busy")
        self.assertEqual(self.state.novel_session.to_state().status.value, "running")
        self.assertFalse((runtime_root / "content").exists())

    async def test_dlc_import_is_conflict_during_planning_and_does_not_cancel_it(self):
        client = BlockingStructuredClient()
        self._replace_planner_client(client)
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        endpoint = f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play"
        play = asyncio.create_task(
            self.client.post(endpoint, json={"speed": "standard"})
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        runtime_root = self.root / "runtime-dlc"

        with patch.object(main_module, "PROJECT_ROOT", runtime_root):
            conflict = await asyncio.wait_for(
                self.client.post(
                    "/api/dlc/import",
                    files={"file": ("theme.md", b"# DLC", "text/markdown")},
                ),
                timeout=0.2,
            )

        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["code"], "story_planning_active")
        self.assertFalse(client.cancelled.is_set())
        self.assertFalse((runtime_root / "content").exists())
        client.release.set()
        completed = await asyncio.wait_for(play, timeout=0.2)
        self.assertEqual(completed.status_code, 200, completed.text)

    async def test_pending_dlc_owner_rejects_llm_cap_and_story_import_without_being_cleared(self):
        first_broadcast_started = asyncio.Event()
        release_first_broadcast = asyncio.Event()
        broadcast_count = 0

        async def controlled_broadcast():
            nonlocal broadcast_count
            broadcast_count += 1
            if broadcast_count == 1:
                first_broadcast_started.set()
                await release_first_broadcast.wait()

        self.state.broadcast = controlled_broadcast
        runtime_root = self.root / "runtime-owner"
        config_dir = runtime_root / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.example.yaml").write_text(
            "llm:\n"
            '  api_key: ""\n'
            "  base_url: https://old.invalid/v1\n"
            "  model: model-one\n",
            encoding="utf-8",
        )
        replacement = CloseAwareStructuredClient()
        original_llm = self.state.llm

        with (
            patch.object(main_module, "PROJECT_ROOT", runtime_root),
            patch.object(main_module, "LLM", return_value=replacement),
        ):
            dlc = asyncio.create_task(
                self.client.post(
                    "/api/dlc/import",
                    files={"file": ("theme.md", b"# DLC", "text/markdown")},
                )
            )
            await asyncio.wait_for(first_broadcast_started.wait(), timeout=0.2)

            llm = await self.client.post(
                "/api/settings/llm",
                json={
                    "api_key": "replacement-key",
                    "base_url": "https://new.invalid/v1",
                    "model": "model-two",
                },
            )
            cap = await self.client.post(
                "/api/device/channels/cap", json={"channel": "A", "value": 30}
            )
            imported = await self._import("story.txt", b"ABCD", "utf-8")

            for response in (llm, cap, imported):
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "story_runtime_busy")
            self.assertIs(self.state.llm, original_llm)
            self.assertFalse((config_dir / "config.yaml").exists())
            self.assertEqual(self.state.story_sources, {})
            self.assertEqual(list(self.state.story_import_directory.iterdir()), [])

            release_first_broadcast.set()
            completed_dlc = await asyncio.wait_for(dlc, timeout=0.2)
            self.assertEqual(completed_dlc.status_code, 200, completed_dlc.text)
            accepted = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertIsNone(self.state.story_runtime_owner)

    async def test_llm_replace_cancels_old_plan_before_closing_and_uses_new_client(self):
        old = CloseAwareStructuredClient(blocked=True)
        self._replace_planner_client(old)
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        endpoint = f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play"
        play = asyncio.create_task(
            self.client.post(endpoint, json={"speed": "standard"})
        )
        await asyncio.wait_for(old.started.wait(), timeout=0.2)
        new = CloseAwareStructuredClient()
        config_root = self.root / "runtime-config"
        config_directory = config_root / "config"
        config_directory.mkdir(parents=True)
        config_text = (
            "llm:\n"
            '  api_key: ""\n'
            "  base_url: https://old.invalid/v1\n"
            "  model: model-one\n"
        )
        (config_directory / "config.example.yaml").write_text(
            config_text, encoding="utf-8"
        )

        with (
            patch.object(main_module, "PROJECT_ROOT", config_root),
            patch.object(main_module, "LLM", return_value=new),
        ):
            replaced = await self.client.post(
                "/api/settings/llm",
                json={
                    "api_key": "replacement-key",
                    "base_url": "https://new.invalid/v1",
                    "model": "model-one",
                },
            )
        completed, _pending = await asyncio.wait({play}, timeout=0.2)
        if play not in completed:
            old.release.set()
            await asyncio.wait_for(play, timeout=0.2)

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertIn(play, completed, "LLM replacement left old planning active")
        self.assertEqual(play.result().status_code, 409)
        self.assertEqual(play.result().json()["code"], "story_planning_cancelled")
        self.assertTrue(old.cancelled.is_set())
        self.assertTrue(old.closed)
        replayed = await self.client.post(endpoint, json={"speed": "standard"})
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(old.call_count, 1)
        self.assertEqual(new.call_count, 1)

    async def test_unexpected_story_exceptions_are_logged_generic_500_without_text(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        self.state.logger.exception = MagicMock()
        original_start = self.state.novel_session.start
        original_pause = self.state.novel_session.pause
        original_resume = self.state.novel_session.resume
        try:
            self.state.novel_session.start = AsyncMock(
                side_effect=ValueError("C:\\private\\story-secret.txt")
            )
            play = await self.client.post(
                f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
                json={"speed": "standard"},
            )
            self.state.novel_session.pause = AsyncMock(
                side_effect=RuntimeError("runtime-secret-token")
            )
            paused = await self.client.post("/api/story/pause")
            self.state.novel_session.resume = AsyncMock(
                side_effect=TypeError("type-secret-token")
            )
            resumed = await self.client.post(
                "/api/story/resume", json={"from": "current"}
            )
        finally:
            self.state.novel_session.start = original_start
            self.state.novel_session.pause = original_pause
            self.state.novel_session.resume = original_resume

        for response in (play, paused, resumed):
            self.assertEqual(response.status_code, 500, response.text)
            self.assertEqual(response.json()["code"], "story_transition_failed")
            self.assertNotIn("secret", response.text.lower())
            self.assertNotIn("private", response.text.lower())
        self.assertEqual(self.state.logger.exception.call_count, 3)

    async def test_import_during_running_novel_does_not_finish_or_archive(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        played = await self.client.post(
            f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(played.status_code, 200, played.text)
        before_files = sorted(path.name for path in self.state.story_import_directory.iterdir())

        conflict = await self._import("other.txt", b"WXYZ", "utf-8")

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "story_runtime_busy")
        self.assertEqual(self.state.active_story_source_id, source_id)
        self.assertEqual(self.harness.controller.to_state().status.value, "running")
        self.assertEqual(self.harness.store.list(), [])
        self.assertEqual(
            sorted(path.name for path in self.state.story_import_directory.iterdir()),
            before_files,
        )

    async def test_profile_hot_change_is_rejected_during_running_novel(self):
        source_bytes = b"ABCD"
        imported = await self._import("story.txt", source_bytes, "utf-8")
        source_id = imported.json()["source"]["source_id"]
        story = self._load_story("story.txt", source_bytes, "utf-8")
        story_map = self._save_analysis(story)
        played = await self.client.post(
            f"/api/story/{source_id}/chapters/{story_map.chapters[0].id}/play",
            json={"speed": "standard"},
        )
        self.assertEqual(played.status_code, 200, played.text)
        original_role = self.state.cfg["character"]["role"]
        original_profile = self.state.cfg["character"]["profile"]

        changed = await self.client.post(
            "/api/character/profile",
            json={"role": "装置", "profile": "调教"},
        )

        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["code"], "story_runtime_busy")
        self.assertEqual(self.state.cfg["character"]["role"], original_role)
        self.assertEqual(self.state.cfg["character"]["profile"], original_profile)
        self.assertEqual(self.harness.controller.to_state().status.value, "running")
        self.assertEqual(self.harness.store.list(), [])


class StoryAppStateWiringTests(unittest.TestCase):
    def test_app_state_constructs_one_planner_and_one_novel_adapter_over_session_owner(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        harness = make_game_loop_for_test(root / "replays")
        cfg = deepcopy(harness.cfg)
        cfg["story"]["import_dir"] = "data/stories"
        cfg["story"]["analysis_dir"] = "data/story_analysis"
        cfg["story"]["analysis_prompt_version"] = "faithful-offline-v1"
        llm = SimpleNamespace(model="model-one", complete_json=AsyncMock())
        controller = MagicMock(waveform_policy="all_allowed")
        loop = MagicMock()
        planner = object()
        novel = SimpleNamespace(session_controller=controller)

        with (
            patch.object(main_module, "PROJECT_ROOT", root),
            patch.object(main_module, "setup_logging", return_value=MagicMock()),
            patch.object(main_module, "SafetyManager", return_value=harness.safety),
            patch.object(main_module, "RelayClient", return_value=harness.relay),
            patch.object(main_module, "LLM", return_value=llm),
            patch.object(main_module, "Camera", return_value=MagicMock()),
            patch.object(main_module, "AudioManager", return_value=MagicMock()),
            patch.object(main_module, "GameLoop", return_value=loop),
            patch.object(main_module, "ReplayStore", return_value=harness.store),
            patch.object(main_module, "SessionController", return_value=controller),
            patch.object(main_module, "ChapterPlanner", return_value=planner) as planner_type,
            patch.object(
                main_module, "NovelSessionController", return_value=novel
            ) as novel_type,
        ):
            state = main_module.AppState(cfg)

        self.addCleanup(state.story_source_store.close)

        self.assertIs(state.chapter_planner, planner)
        self.assertIs(state.novel_session, novel)
        planner_type.assert_called_once()
        planner_call = planner_type.call_args
        self.assertIs(planner_call.args[0], llm)
        self.assertEqual(planner_call.kwargs["model_identity"], "model-one")
        self.assertEqual(planner_call.kwargs["prompt_version"], "faithful-offline-v1")
        self.assertEqual(planner_call.kwargs["waveform_registry"], harness.safety.presets)
        self.assertEqual(
            planner_call.kwargs["effective_caps"],
            {channel: harness.safety.cap_for(channel) for channel in ("A", "B")},
        )
        novel_type.assert_called_once()
        self.assertIs(novel_type.call_args.args[0], controller)
        self.assertIs(state.novel_session.session_controller, state.timeline_session)


if __name__ == "__main__":
    unittest.main()
