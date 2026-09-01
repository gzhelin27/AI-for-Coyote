from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import httpx

from backend.config import DEFAULTS
from backend.safety import SafetyManager
from backend.story.models import ImportedStory, StoryChapter, StoryMap, StoryScene
from backend.story.planner import ChapterPlanError, ChapterPlanner
from backend.timeline.models import CycleGapPolicy, DirectiveMode
from backend.llm import LLM, StructuredResponseError


class RecordingStructuredClient:
    def __init__(self, response: object = None, *, error: Exception | None = None):
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

    def make_planner(self, client, safety):
        return ChapterPlanner(
            client,
            waveform_registry={"wave-a": ("frame-a",), "wave-b": ("frame-b",)},
            effective_caps={"A": 40, "B": 30},
            reading_speed_cpm={"slow": 30, "standard": 60, "fast": 120},
            cycle_gap_policy=self.policy,
            safety_adapter=safety,
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
            set(request), {"chapter_text", "scenes", "capabilities", "constraints"}
        )
        self.assertEqual(request["chapter_text"], "ABCDWXYZ")
        self.assertNotIn("FIRST-ONLY", user_content)
        self.assertEqual(
            request["scenes"],
            [
                {
                    "scene_id": self.story_map.chapters[1].scenes[0].id,
                    "start_offset": 11,
                    "end_offset": 15,
                    "summary": "前四字。",
                },
                {
                    "scene_id": self.story_map.chapters[1].scenes[1].id,
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


class StructuredLLMTests(unittest.IsolatedAsyncioTestCase):
    def make_llm(self, response: httpx.Response) -> tuple[LLM, RecordingHTTPClient]:
        cfg = deepcopy(DEFAULTS)
        client = RecordingHTTPClient(response)
        with patch("backend.llm.httpx.AsyncClient", return_value=client):
            llm = LLM(cfg)
        return llm, client

    async def test_complete_json_makes_exactly_one_structured_request(self):
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"scenes":[]}'}}]},
        )
        llm, client = self.make_llm(response)

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
        llm, client = self.make_llm(response)

        with self.assertRaises(StructuredResponseError):
            await llm.complete_json("system", "selected chapter", "chapter_plan")

        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
