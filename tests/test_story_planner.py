from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.config import DEFAULTS
from backend.safety import SafetyManager
from backend.story.models import ImportedStory, StoryChapter, StoryMap, StoryScene
from backend.story.planner import ChapterPlanError, ChapterPlanner
from backend.timeline.models import CycleGapPolicy, DirectiveMode
from backend.llm import LLM, StructuredResponseError


class RecordingStructuredClient:
    def __init__(self, response: object = None, *, error: Exception | None = None):
        self.model = "model-a"
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> object:
        self.calls.append((system_prompt, user_content, schema_name))
        if self.error is not None:
            raise self.error
        return deepcopy(self.response)


class AlternatingStructuredClient(RecordingStructuredClient):
    def __init__(self, responses: list[object]):
        super().__init__()
        self.responses = responses

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> object:
        self.calls.append((system_prompt, user_content, schema_name))
        response = self.responses[(len(self.calls) - 1) % len(self.responses)]
        return deepcopy(response)


class BlockingStructuredClient(RecordingStructuredClient):
    def __init__(self, response: object):
        super().__init__(response)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> object:
        self.calls.append((system_prompt, user_content, schema_name))
        self.started.set()
        await self.release.wait()
        return deepcopy(self.response)


class SequencedStructuredClient(RecordingStructuredClient):
    def __init__(self, outcomes: list[object]):
        super().__init__()
        self.outcomes = outcomes

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> object:
        self.calls.append((system_prompt, user_content, schema_name))
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return deepcopy(outcome)


class RequestAwareStructuredClient(RecordingStructuredClient):
    """Return a legal response for the exact ordered scenes in each request."""

    async def complete_json(
        self, system_prompt: str, user_content: str, schema_name: str
    ) -> object:
        self.calls.append((system_prompt, user_content, schema_name))
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


class RecordingSafetyAdapter:
    def __init__(self, safety: SafetyManager, *, reject_at: int | None = None):
        self.safety = safety
        self.reject_at = reject_at
        self.actions: list[dict] = []

    def validate(self, action: dict):
        self.actions.append(deepcopy(action))
        if self.reject_at == len(self.actions):
            return False, "dry validation rejected", None
        return self.safety.validate(action)


class RecordingHTTPClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append((url, deepcopy(headers), deepcopy(json)))
        return self.response


def make_story() -> tuple[ImportedStory, StoryMap, str]:
    source_hash = "a" * 64
    text = "FIRST-ONLY\nABCDWXYZ"
    first = StoryChapter(
        id=StoryChapter.stable_id(source_hash, 0),
        index=0,
        start_offset=0,
        end_offset=11,
        title="First",
        summary="不应发送。",
        scenes=(
            StoryScene(
                id=StoryScene.stable_id(source_hash, 0, 0),
                index=0,
                start_offset=0,
                end_offset=11,
                summary="不应发送的场景。",
                pace=1.0,
            ),
        ),
    )
    second = StoryChapter(
        id=StoryChapter.stable_id(source_hash, 1),
        index=1,
        start_offset=11,
        end_offset=19,
        title="Second",
        summary="选择的章节。",
        scenes=(
            StoryScene(
                id=StoryScene.stable_id(source_hash, 1, 0),
                index=0,
                start_offset=11,
                end_offset=15,
                summary="前四字。",
                pace=1.0,
            ),
            StoryScene(
                id=StoryScene.stable_id(source_hash, 1, 1),
                index=1,
                start_offset=15,
                end_offset=19,
                summary="后四字。",
                pace=2.0,
            ),
        ),
    )
    story = ImportedStory(
        filename="story.txt",
        extension=".txt",
        original_bytes=text.encode(),
        text=text,
        source_sha256=source_hash,
    )
    story_map = StoryMap(source_hash=source_hash, text_length=19, chapters=(first, second))
    return story, story_map, second.id


def valid_response(story_map: StoryMap) -> dict:
    scenes = story_map.chapters[1].scenes
    return {
        "scenes": [
            {
                "scene_id": scenes[0].id,
                "channels": {
                    "A": {"mode": "set", "base_strength": 20},
                    "B": {"mode": "keep"},
                },
            },
            {
                "scene_id": scenes[1].id,
                "channels": {
                    "A": {"mode": "stop"},
                    "B": {"mode": "set", "base_strength": 10},
                },
            },
        ]
    }


def alternate_valid_response(story_map: StoryMap) -> dict:
    scenes = story_map.chapters[1].scenes
    return {
        "scenes": [
            {
                "scene_id": scenes[0].id,
                "channels": {
                    "A": {"mode": "stop"},
                    "B": {"mode": "set", "base_strength": 27},
                },
            },
            {
                "scene_id": scenes[1].id,
                "channels": {
                    "A": {"mode": "set", "base_strength": 35},
                    "B": {"mode": "keep"},
                },
            },
        ]
    }


def make_recording_llm(response: httpx.Response) -> tuple[LLM, RecordingHTTPClient]:
    cfg = deepcopy(DEFAULTS)
    client = RecordingHTTPClient(response)
    with patch("backend.llm.httpx.AsyncClient", return_value=client):
        llm = LLM(cfg)
    return llm, client


class StoryPlannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.story, self.story_map, self.chapter_id = make_story()
        cfg = deepcopy(DEFAULTS)
        cfg["safety"]["max_strength_step"] = 40
        cfg["presets"] = {
            "wave-a": {
                "waveform": "BREATH",
                "frames": ["frame-a"],
                "default_duration_s": 5,
                "max_duration_s": 10,
            },
            "wave-b": {
                "waveform": "TIDE",
                "frames": ["frame-b"],
                "default_duration_s": 5,
                "max_duration_s": 10,
            },
        }
        safety = SafetyManager(cfg)
        safety.set_user_cap("A", 40)
        safety.set_user_cap("B", 30)
        self.safety = RecordingSafetyAdapter(safety)
        self.policy = CycleGapPolicy()
        self.client = RecordingStructuredClient(valid_response(self.story_map))
        self.planner = self.make_planner(self.client, self.safety)

    def make_planner(
        self,
        client,
        safety,
        *,
        model_identity: str | None = None,
        prompt_version: str = "chapter-plan-v1",
        dlc_version: str = "dlc-provenance-a",
    ):
        return ChapterPlanner(
            client,
            waveform_registry={"wave-a": ("frame-a",), "wave-b": ("frame-b",)},
            effective_caps={"A": 40, "B": 30},
            reading_speed_cpm={"slow": 30, "standard": 60, "fast": 120},
            cycle_gap_policy=self.policy,
            safety_adapter=safety,
            model_identity=model_identity or client.model,
            prompt_version=prompt_version,
            dlc_version=dlc_version,
        )

    async def test_selected_chapter_request_and_plan_have_exact_order_coverage_and_timing(self):
        plan = await self.planner.plan(
            self.story,
            self.story_map,
            self.chapter_id,
            speed="standard",
            seed=88,
        )

        self.assertEqual(len(self.client.calls), 1)
        system_prompt, user_content, schema_name = self.client.calls[0]
        request = json.loads(user_content)
        self.assertEqual(schema_name, "chapter_plan")
        self.assertIn("faithful", system_prompt.lower())
        self.assertEqual(
            set(request),
            {"chapter", "chapter_text", "scenes", "capabilities", "constraints"},
        )
        self.assertEqual(
            request["chapter"],
            {
                "id": self.chapter_id,
                "index": 1,
                "start_offset": 11,
                "end_offset": 19,
                "title": "Second",
                "summary": "选择的章节。",
            },
        )
        self.assertEqual(request["chapter_text"], "ABCDWXYZ")
        self.assertNotIn("FIRST-ONLY", user_content)
        self.assertEqual(
            request["scenes"],
            [
                {
                    "scene_id": self.story_map.chapters[1].scenes[0].id,
                    "index": 0,
                    "start_offset": 11,
                    "end_offset": 15,
                    "summary": "前四字。",
                },
                {
                    "scene_id": self.story_map.chapters[1].scenes[1].id,
                    "index": 1,
                    "start_offset": 15,
                    "end_offset": 19,
                    "summary": "后四字。",
                },
            ],
        )
        self.assertEqual(
            request["capabilities"],
            {
                "A": {
                    "modes": ["keep", "set", "stop"],
                    "set_fields": ["base_strength"],
                    "effective_cap": 40,
                },
                "B": {
                    "modes": ["keep", "set", "stop"],
                    "set_fields": ["base_strength"],
                    "effective_cap": 30,
                },
            },
        )
        self.assertTrue(request["constraints"]["faithful_mode"])
        self.assertTrue(request["constraints"]["preserve_scene_order"])
        self.assertEqual(request["constraints"]["one_entry_per_scene"], True)
        self.assertEqual(request["constraints"]["waveform_source"], "seeded_resolver")

        scenes = self.story_map.chapters[1].scenes
        self.assertEqual(plan.source_hash, self.story.source_sha256)
        self.assertEqual(plan.chapter_id, self.chapter_id)
        self.assertEqual(plan.speed, "standard")
        self.assertEqual(plan.seed, 88)
        self.assertEqual([event.scene_id for event in plan.plot_events], [s.id for s in scenes])
        self.assertEqual([event.offset_ms for event in plan.plot_events], [0, 4_000])
        self.assertEqual(plan.chapter_duration_ms, 12_000)
        self.assertEqual(plan.timeline_request.plot_events, plan.plot_events)
        self.assertEqual(plan.timeline_request.chapter_duration_ms, 12_000)
        self.assertEqual(plan.timeline_request.cycle_gap_policy, self.policy)

        first, second = plan.plot_events
        self.assertEqual(first.channels["A"].mode, DirectiveMode.SET)
        self.assertEqual(first.channels["A"].base_strength, 20)
        self.assertIn(first.channels["A"].pattern, {"wave-a", "wave-b"})
        self.assertLessEqual(first.channels["A"].resolved_strength, 40)
        self.assertEqual(first.channels["B"].mode, DirectiveMode.KEEP)
        self.assertEqual(second.channels["A"].mode, DirectiveMode.STOP)
        self.assertEqual(second.channels["B"].mode, DirectiveMode.SET)
        self.assertEqual(second.channels["B"].base_strength, 10)
        self.assertIn(second.channels["B"].pattern, {"wave-a", "wave-b"})
        self.assertLessEqual(second.channels["B"].resolved_strength, 30)

        self.assertEqual(
            self.safety.actions,
            [
                {"op": "hold_strength", "channel": "A", "value": first.channels["A"].resolved_strength},
                {"op": "pulse_cycle", "channel": "A", "pattern": first.channels["A"].pattern},
                {"op": "clear", "channel": "A"},
                {"op": "hold_strength", "channel": "B", "value": second.channels["B"].resolved_strength},
                {"op": "pulse_cycle", "channel": "B", "pattern": second.channels["B"].pattern},
            ],
        )

    async def test_same_seed_and_speed_produce_same_plan(self):
        first = await self.planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=88
        )
        second = await self.planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=88
        )
        self.assertEqual(first, second)

    async def test_validated_intent_is_cached_across_seed_and_speed(self):
        client = AlternatingStructuredClient(
            [valid_response(self.story_map), alternate_valid_response(self.story_map)]
        )
        planner = self.make_planner(client, self.safety)

        first = await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="slow", seed=1
        )
        second = await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="fast", seed=2
        )
        repeated = await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="slow", seed=1
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(first, repeated)
        self.assertEqual(
            [
                (directive.mode, directive.base_strength)
                for event in first.plot_events
                for directive in event.channels.values()
            ],
            [
                (directive.mode, directive.base_strength)
                for event in second.plot_events
                for directive in event.channels.values()
            ],
        )
        self.assertNotEqual(first.chapter_duration_ms, second.chapter_duration_ms)

    async def test_request_fingerprint_invalidates_every_selected_chapter_input(self):
        chapter = self.story_map.chapters[1]
        first_chapter = self.story_map.chapters[0]

        def story_map_with(selected: StoryChapter) -> StoryMap:
            return StoryMap(
                source_hash=self.story_map.source_hash,
                text_length=self.story_map.text_length,
                chapters=(first_chapter, selected),
            )

        shifted_scenes = (
            replace(chapter.scenes[0], end_offset=14),
            replace(chapter.scenes[1], start_offset=14),
        )
        three_scenes = (
            replace(chapter.scenes[0], end_offset=13),
            StoryScene(
                id=StoryScene.stable_id(self.story.source_sha256, 1, 1),
                index=1,
                start_offset=13,
                end_offset=16,
                summary="中三字。",
                pace=1.0,
            ),
            StoryScene(
                id=StoryScene.stable_id(self.story.source_sha256, 1, 2),
                index=2,
                start_offset=16,
                end_offset=19,
                summary="末三字。",
                pace=2.0,
            ),
        )
        shortened_first_scene = replace(first_chapter.scenes[0], end_offset=10)
        shifted_first = replace(
            first_chapter, end_offset=10, scenes=(shortened_first_scene,)
        )
        shifted_chapter_scenes = (
            replace(chapter.scenes[0], start_offset=10),
            chapter.scenes[1],
        )
        shifted_chapter = replace(
            chapter, start_offset=10, scenes=shifted_chapter_scenes
        )
        shifted_chapter_map = StoryMap(
            source_hash=self.story_map.source_hash,
            text_length=self.story_map.text_length,
            chapters=(shifted_first, shifted_chapter),
        )

        variants = {
            "chapter title": (
                self.story,
                story_map_with(replace(chapter, title="Second revised")),
            ),
            "chapter summary": (
                self.story,
                story_map_with(replace(chapter, summary="修订章节摘要。")),
            ),
            "chapter bounds": (self.story, shifted_chapter_map),
            "exact chapter text": (
                replace(
                    self.story,
                    text="FIRST-ONLY\nABCEWXYZ",
                    original_bytes=b"FIRST-ONLY\nABCEWXYZ",
                ),
                self.story_map,
            ),
            "scene offsets": (
                self.story,
                story_map_with(replace(chapter, scenes=shifted_scenes)),
            ),
            "scene summary": (
                self.story,
                story_map_with(
                    replace(
                        chapter,
                        scenes=(
                            replace(chapter.scenes[0], summary="修订场景摘要。"),
                            chapter.scenes[1],
                        ),
                    )
                ),
            ),
            "scene count": (
                self.story,
                story_map_with(replace(chapter, scenes=three_scenes)),
            ),
        }

        for label, (changed_story, changed_map) in variants.items():
            with self.subTest(label=label):
                client = RequestAwareStructuredClient()
                planner = self.make_planner(client, self.safety)
                await planner.plan(
                    self.story,
                    self.story_map,
                    self.chapter_id,
                    speed="slow",
                    seed=1,
                )
                changed = await planner.plan(
                    changed_story,
                    changed_map,
                    self.chapter_id,
                    speed="fast",
                    seed=2,
                )

                self.assertEqual(len(client.calls), 2)
                self.assertEqual(
                    len(changed.plot_events), len(changed_map.chapters[1].scenes)
                )

    async def test_internal_intent_length_mismatch_is_a_typed_atomic_failure(self):
        planner = self.make_planner(RequestAwareStructuredClient(), self.safety)

        with patch.object(
            planner, "_cached_intents", AsyncMock(return_value=())
        ), self.assertRaisesRegex(ChapterPlanError, "internal scene count"):
            await planner.plan(
                self.story,
                self.story_map,
                self.chapter_id,
                speed="standard",
                seed=88,
            )

    async def test_concurrent_same_key_plans_share_one_request(self):
        client = BlockingStructuredClient(valid_response(self.story_map))
        planner = self.make_planner(client, self.safety)

        first = asyncio.create_task(
            planner.plan(
                self.story, self.story_map, self.chapter_id, speed="slow", seed=1
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        second = asyncio.create_task(
            planner.plan(
                self.story, self.story_map, self.chapter_id, speed="fast", seed=2
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(client.calls), 1)

        client.release.set()
        first_plan, second_plan = await asyncio.gather(first, second)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            [event.channels["A"].mode for event in first_plan.plot_events],
            [event.channels["A"].mode for event in second_plan.plot_events],
        )

    async def test_cancelled_waiter_does_not_cancel_shared_intent_request(self):
        client = BlockingStructuredClient(valid_response(self.story_map))
        planner = self.make_planner(client, self.safety)

        cancelled = asyncio.create_task(
            planner.plan(
                self.story, self.story_map, self.chapter_id, speed="slow", seed=1
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        survivor = asyncio.create_task(
            planner.plan(
                self.story, self.story_map, self.chapter_id, speed="fast", seed=2
            )
        )
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(client.release.is_set())

        client.release.set()
        await survivor
        await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=3
        )
        self.assertEqual(len(client.calls), 1)

    async def test_failed_intent_flight_is_evicted_and_can_retry(self):
        client = SequencedStructuredClient(
            [RuntimeError("provider down"), valid_response(self.story_map)]
        )
        planner = self.make_planner(client, self.safety)

        with self.assertRaisesRegex(ChapterPlanError, "model"):
            await planner.plan(
                self.story, self.story_map, self.chapter_id, speed="standard", seed=1
            )
        recovered = await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=1
        )
        cached = await planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=1
        )

        self.assertEqual(recovered, cached)
        self.assertEqual(len(client.calls), 2)

    def test_planning_cache_identity_fields_fail_fast_when_empty(self):
        for field in ("model_identity", "prompt_version", "dlc_version"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.make_planner(
                    self.client,
                    self.safety,
                    **{field: " "},
                )

    async def test_planning_cache_rejects_mismatched_or_changed_client_model(self):
        with self.assertRaisesRegex(ValueError, "model identity"):
            self.make_planner(self.client, self.safety, model_identity="model-b")

        self.client.model = "model-b"
        with self.assertRaisesRegex(ChapterPlanError, "model identity"):
            await self.planner.plan(
                self.story,
                self.story_map,
                self.chapter_id,
                speed="standard",
                seed=88,
            )
        self.assertEqual(self.client.calls, [])

    async def test_different_seeds_may_vary_only_resolved_random_fields(self):
        first = await self.planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=1
        )
        second = await self.planner.plan(
            self.story, self.story_map, self.chapter_id, speed="standard", seed=2
        )

        self.assertEqual(
            [(event.scene_id, event.offset_ms) for event in first.plot_events],
            [(event.scene_id, event.offset_ms) for event in second.plot_events],
        )
        self.assertNotEqual(first.plot_events, second.plot_events)

    async def test_strict_scene_and_directive_schema_rejects_entire_plan(self):
        base = valid_response(self.story_map)
        mutations = {
            "missing scene": lambda value: value["scenes"].pop(),
            "reordered scenes": lambda value: value["scenes"].reverse(),
            "extra top-level field": lambda value: value.update({"extra": True}),
            "missing channel": lambda value: value["scenes"][0]["channels"].pop("B"),
            "extra channel": lambda value: value["scenes"][0]["channels"].update(
                {"C": {"mode": "keep"}}
            ),
            "keep with strength": lambda value: value["scenes"][0]["channels"]["B"].update(
                {"base_strength": 1}
            ),
            "stop with pattern": lambda value: value["scenes"][1]["channels"]["A"].update(
                {"pattern": "wave-a"}
            ),
            "set missing strength": lambda value: value["scenes"][0]["channels"]["A"].pop(
                "base_strength"
            ),
            "set supplies waveform": lambda value: value["scenes"][0]["channels"]["A"].update(
                {"pattern": "wave-a"}
            ),
            "boolean strength": lambda value: value["scenes"][0]["channels"]["A"].update(
                {"base_strength": True}
            ),
            "cap overflow": lambda value: value["scenes"][0]["channels"]["A"].update(
                {"base_strength": 41}
            ),
            "unknown mode": lambda value: value["scenes"][0]["channels"]["A"].update(
                {"mode": "pulse"}
            ),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                response = deepcopy(base)
                mutate(response)
                client = RecordingStructuredClient(response)
                with self.assertRaises(ChapterPlanError):
                    await self.make_planner(client, self.safety).plan(
                        self.story,
                        self.story_map,
                        self.chapter_id,
                        speed="standard",
                        seed=88,
                    )
                self.assertEqual(len(client.calls), 1)

    async def test_model_timing_and_safety_failures_are_typed_and_never_emit(self):
        model_client = RecordingStructuredClient(error=RuntimeError("provider down"))
        with self.assertRaisesRegex(ChapterPlanError, "model"):
            await self.make_planner(model_client, self.safety).plan(
                self.story, self.story_map, self.chapter_id, speed="standard", seed=88
            )
        self.assertEqual(len(model_client.calls), 1)

        with self.assertRaisesRegex(ChapterPlanError, "speed"):
            await self.planner.plan(
                self.story, self.story_map, self.chapter_id, speed="turbo", seed=88
            )

        rejecting = RecordingSafetyAdapter(self.safety.safety, reject_at=2)
        with self.assertRaisesRegex(ChapterPlanError, "safety"):
            await self.make_planner(
                RecordingStructuredClient(valid_response(self.story_map)), rejecting
            ).plan(
                self.story, self.story_map, self.chapter_id, speed="standard", seed=88
            )
        self.assertEqual(len(rejecting.actions), 2)
        self.assertFalse(hasattr(rejecting, "player"))
        self.assertFalse(hasattr(rejecting, "device"))

    async def test_source_map_and_chapter_identity_fail_before_model_request(self):
        wrong_story = ImportedStory(
            filename=self.story.filename,
            extension=self.story.extension,
            original_bytes=self.story.original_bytes,
            text=self.story.text,
            source_sha256="b" * 64,
        )
        for story, story_map, chapter_id in (
            (wrong_story, self.story_map, self.chapter_id),
            (self.story, self.story_map, "missing-chapter"),
        ):
            with self.subTest(chapter_id=chapter_id), self.assertRaises(ChapterPlanError):
                await self.planner.plan(
                    story, story_map, chapter_id, speed="standard", seed=88
                )
        self.assertEqual(self.client.calls, [])

    async def test_duplicate_raw_json_keys_fail_closed_without_retry(self):
        compact = json.dumps(
            valid_response(self.story_map), ensure_ascii=False, separators=(",", ":")
        )
        duplicate_documents = {
            "scenes": compact.replace('"scenes":[', '"scenes":[],"scenes":[', 1),
            "channels": compact.replace(
                '"channels":{"A"', '"channels":{},"channels":{"A"', 1
            ),
            "mode": compact.replace(
                '"mode":"set","base_strength":20',
                '"mode":"keep","mode":"set","base_strength":20',
                1,
            ),
            "base_strength": compact.replace(
                '"base_strength":20', '"base_strength":1,"base_strength":20', 1
            ),
        }

        for field, content in duplicate_documents.items():
            with self.subTest(field=field):
                llm, http_client = make_recording_llm(
                    httpx.Response(
                        200,
                        json={"choices": [{"message": {"content": content}}]},
                    )
                )
                with self.assertRaisesRegex(ChapterPlanError, "model"):
                    await self.make_planner(
                        llm, self.safety, model_identity=llm.model
                    ).plan(
                        self.story,
                        self.story_map,
                        self.chapter_id,
                        speed="standard",
                        seed=88,
                    )
                self.assertEqual(len(http_client.calls), 1)

    async def test_parsed_only_provider_response_fails_closed(self):
        llm, http_client = make_recording_llm(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"parsed": valid_response(self.story_map)}}
                    ]
                },
            )
        )

        with self.assertRaisesRegex(ChapterPlanError, "model"):
            await self.make_planner(llm, self.safety, model_identity=llm.model).plan(
                self.story,
                self.story_map,
                self.chapter_id,
                speed="standard",
                seed=88,
            )

        self.assertEqual(len(http_client.calls), 1)


class StructuredLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_json_makes_exactly_one_structured_request(self):
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"scenes":[]}'}}]},
        )
        llm, client = make_recording_llm(response)

        result = await llm.complete_json("system", "selected chapter", "chapter_plan")

        self.assertEqual(result, {"scenes": []})
        self.assertEqual(len(client.calls), 1)
        _, _, payload = client.calls[0]
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "selected chapter"},
        ])
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    async def test_complete_json_does_not_retry_malformed_model_output(self):
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
        )
        llm, client = make_recording_llm(response)

        with self.assertRaises(StructuredResponseError):
            await llm.complete_json("system", "selected chapter", "chapter_plan")

        self.assertEqual(len(client.calls), 1)

    async def test_complete_json_rejects_oversized_raw_content_without_retry(self):
        contents = (
            '{"value":"' + "x" * 300_000 + '"}',
            " " * 300_000 + '{"scenes":[]}',
        )
        for content in contents:
            with self.subTest(prefix=content[:12]):
                response = httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": content}}]},
                )
                llm, client = make_recording_llm(response)

                with self.assertRaisesRegex(StructuredResponseError, "size"):
                    await llm.complete_json(
                        "system", "selected chapter", "chapter_plan"
                    )

                self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
