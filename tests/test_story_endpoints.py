from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
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
        self.state.story_source_loader = StorySourceLoader(max_bytes=1024 * 1024)
        self.state.story_source_max_bytes = 1024 * 1024
        self.state.story_analysis_store = AnalysisStore(self.root / "analysis")
        self.state.story_sources = {}
        self.state.active_story_source_id = None
        self.state.story_dlc_version = "dlc-test-v1"
        self.state.story_seed_factory = lambda: 71
        self.llm = RaisingStructuredClient()
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
        self.assertFalse(self.state.story_import_directory.exists())
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

    async def test_import_rejects_an_ancestor_storage_redirect(self):
        outside = self.root / "outside"
        outside.mkdir()
        redirected_parent = self.root / "redirected-parent"
        try:
            os.symlink(outside, redirected_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            command = subprocess.run(
                [
                    "cmd",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(redirected_parent),
                    str(outside),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if command.returncode:
                self.skipTest(
                    f"directory links are unavailable: {command.stderr or command.stdout}"
                )
            self.addCleanup(
                lambda: redirected_parent.rmdir()
                if redirected_parent.exists()
                else None
            )
        self.state.story_import_directory = redirected_parent / "stories"

        response = await self._import("story.txt", b"ABCD", "utf-8")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "story_import_failed")
        self.assertEqual(self.state.story_sources, {})
        self.assertEqual(list(outside.iterdir()), [])
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
        self.assertEqual(response.json()["dlc_version"], "dlc-test-v1")
        self.assertEqual(self.llm.call_count, 0)

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
