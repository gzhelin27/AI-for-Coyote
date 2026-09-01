from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from backend.story.models import ImportedStory, StoryChapter, StoryMap, StoryScene
from backend.story.planner import ChapterTimelineRequest, ValidatedChapterPlan
from backend.story.session import (
    NovelSessionController,
    NovelSessionError,
    NovelSessionStatus,
)
from backend.timeline.models import (
    ChannelDirective,
    CycleGapPolicy,
    DirectiveMode,
    PlotEvent,
    SessionStatus,
)
from tests.timeline_fakes import SessionHarness


def make_novel_inputs() -> tuple[ImportedStory, StoryMap, ValidatedChapterPlan]:
    text = "ABCDE12345"
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    first_scene = StoryScene(
        id=StoryScene.stable_id(source_hash, 0, 0),
        index=0,
        start_offset=0,
        end_offset=5,
        summary="第一场。",
        pace=1.0,
    )
    second_scene = StoryScene(
        id=StoryScene.stable_id(source_hash, 0, 1),
        index=1,
        start_offset=5,
        end_offset=10,
        summary="第二场。",
        pace=1.0,
    )
    chapter = StoryChapter(
        id=StoryChapter.stable_id(source_hash, 0),
        index=0,
        start_offset=0,
        end_offset=10,
        title="第一章",
        summary="完整章节。",
        scenes=(first_scene, second_scene),
    )
    story_map = StoryMap(
        source_hash=source_hash,
        text_length=len(text),
        chapters=(chapter,),
    )
    story = ImportedStory(
        filename="original.txt",
        extension=".txt",
        original_bytes=text.encode("utf-8"),
        text=text,
        source_sha256=source_hash,
    )
    first_event = PlotEvent(
        event_id=f"{chapter.id}-evt-0001",
        scene_id=first_scene.id,
        offset_ms=0,
        channels={
            "A": ChannelDirective(
                channel="A",
                mode=DirectiveMode.SET,
                pattern="呼吸",
                base_strength=20,
                resolved_strength=22,
            ),
            "B": ChannelDirective(channel="B", mode=DirectiveMode.KEEP),
        },
    )
    second_event = PlotEvent(
        event_id=f"{chapter.id}-evt-0002",
        scene_id=second_scene.id,
        offset_ms=1_000,
        channels={
            "A": ChannelDirective(channel="A", mode=DirectiveMode.STOP),
            "B": ChannelDirective(
                channel="B",
                mode=DirectiveMode.SET,
                pattern="呼吸",
                base_strength=10,
                resolved_strength=12,
            ),
        },
    )
    request = ChapterTimelineRequest(
        plot_events=(first_event, second_event),
        chapter_duration_ms=2_000,
        cycle_gap_policy=CycleGapPolicy(),
    )
    plan = ValidatedChapterPlan(
        source_hash=source_hash,
        chapter_id=chapter.id,
        speed="standard",
        seed=71,
        plot_events=request.plot_events,
        chapter_duration_ms=request.chapter_duration_ms,
        timeline_request=request,
    )
    return story, story_map, plan


class NovelSessionControllerTests(unittest.IsolatedAsyncioTestCase):
    def make_controller(
        self,
    ) -> tuple[SessionHarness, NovelSessionController, ImportedStory, StoryMap, ValidatedChapterPlan]:
        harness = SessionHarness.create(seed=71)
        novel = NovelSessionController(
            harness.controller,
            source_encoding="utf-8",
            analysis_version="faithful-offline-v1",
            dlc_version="dlc-test-v1",
        )
        story, story_map, plan = make_novel_inputs()
        return harness, novel, story, story_map, plan

    async def wait_for_scene(
        self, novel: NovelSessionController, scene_id: str
    ) -> None:
        for _ in range(100):
            if novel.to_state().current_scene_id == scene_id:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"novel session did not reach scene {scene_id}")

    async def advance_to_scene(
        self,
        harness: SessionHarness,
        novel: NovelSessionController,
        scene_id: str,
    ) -> None:
        for _ in range(100):
            if novel.to_state().current_scene_id == scene_id:
                return
            remaining = harness.clock.next_remaining_ms
            if remaining is not None:
                harness.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"novel session did not advance to scene {scene_id}")

    async def test_validated_plan_autoplays_through_the_injected_session_owner(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)

        state = await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)

        self.assertEqual(state.status, NovelSessionStatus.RUNNING)
        self.assertEqual(harness.to_state().mode, "novel")
        self.assertIsNone(harness.player)
        self.assertEqual(harness.resolver.calls, [])
        self.assertIs(novel.session_controller, harness.controller)

    async def test_invalid_plan_fails_before_any_output_or_archive(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        forged = replace(plan, source_hash="b" * 64)

        with self.assertRaises(NovelSessionError):
            await novel.start(forged, story, story_map)

        self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)
        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertEqual(harness.game_loop.execute_calls, [])
        self.assertEqual(harness.clear_calls, [])
        self.assertEqual(harness.store.list(), [])

    async def test_pause_clears_immediately_and_preserves_the_event_cursor(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.advance_to_scene(
            harness, novel, plan.plot_events[1].scene_id
        )
        before = novel.to_state()

        paused = await novel.pause()

        self.assertEqual(paused.status, NovelSessionStatus.PAUSED)
        self.assertEqual(paused.cursor, before.cursor)
        self.assertEqual(paused.current_scene_id, before.current_scene_id)
        self.assertEqual(harness.clear_calls[-1:], [None])
        self.assertEqual(harness.store.list(), [])

    async def test_chapter_duration_stops_the_last_scene_without_early_archive(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)

        for _ in range(200):
            if harness.to_state().status is SessionStatus.PAUSED:
                break
            remaining = harness.clock.next_remaining_ms
            if remaining is not None:
                harness.clock.advance(remaining)
            await asyncio.sleep(0)

        self.assertEqual(harness.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.PAUSED)
        self.assertEqual(harness.clear_calls[-1:], [None])
        self.assertEqual(harness.store.list(), [])

    async def test_all_resume_positions_clear_then_reposition_authoritatively(self):
        for from_, expected_cursor in (
            ("current", 1),
            ("chapter_start", 0),
            ("beginning", 0),
        ):
            with self.subTest(from_=from_):
                harness, novel, story, story_map, plan = self.make_controller()
                self.addAsyncCleanup(harness.close)
                await novel.start(plan, story, story_map)
                await self.advance_to_scene(
                    harness, novel, plan.plot_events[1].scene_id
                )
                await novel.pause()
                clear_count = len(harness.clear_calls)

                resumed = await novel.resume(from_=from_)
                expected_scene = plan.plot_events[expected_cursor].scene_id
                await self.wait_for_scene(novel, expected_scene)

                self.assertEqual(resumed.status, NovelSessionStatus.RUNNING)
                self.assertEqual(novel.to_state().cursor, expected_cursor)
                self.assertEqual(novel.to_state().current_scene_id, expected_scene)
                resume_clears = harness.clear_calls[clear_count:]
                self.assertEqual(resume_clears[0], None)
                self.assertEqual(resume_clears.count(None), 1)
                self.assertIsNone(harness.player)
                await novel.abort()

    async def test_resume_current_materializes_prior_set_for_a_keep_scene(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        keep_event = replace(
            plan.plot_events[1],
            channels={
                "A": ChannelDirective(channel="A", mode=DirectiveMode.KEEP),
                "B": ChannelDirective(channel="B", mode=DirectiveMode.KEEP),
            },
        )
        request = replace(
            plan.timeline_request,
            plot_events=(plan.plot_events[0], keep_event),
        )
        keep_plan = replace(
            plan,
            plot_events=request.plot_events,
            timeline_request=request,
        )
        await novel.start(keep_plan, story, story_map)
        await self.advance_to_scene(harness, novel, keep_event.scene_id)
        await novel.pause()

        await novel.resume(from_="current")
        await self.wait_for_scene(novel, keep_event.scene_id)

        runner = harness.runners.get("A")
        self.assertIsNotNone(runner)
        self.assertEqual(runner.state().directive.pattern, "呼吸")
        self.assertEqual(runner.state().directive.requested_strength, 22)

    async def test_chat_actions_cannot_mutate_the_validated_novel_plan(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)

        with self.assertRaisesRegex(RuntimeError, "novel"):
            await harness.process_live_turn(
                [{"op": "hold_strength", "channel": "B", "value": 99}]
            )
        self.assertIs(novel.plan, plan)
        summary = await novel.finish()
        archived = harness.store.load(summary.replay_id)

        self.assertEqual(archived.timeline.plot_events, plan.plot_events)
        self.assertEqual(harness.resolver.calls, [])

    async def test_finish_embeds_exact_source_scenes_and_novel_metadata(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)

        summary = await novel.finish()
        archived = harness.store.load(summary.replay_id)

        self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)
        self.assertEqual(archived.source, story.original_bytes)
        self.assertEqual(archived.source_extension, "txt")
        self.assertEqual(archived.scenes["source_hash"], story_map.source_hash)
        self.assertEqual(
            [item["id"] for item in archived.scenes["chapters"]],
            [chapter.id for chapter in story_map.chapters],
        )
        self.assertEqual(
            archived.manifest.metadata,
            {
                "analysis_version": "faithful-offline-v1",
                "chapter_id": plan.chapter_id,
                "content_type": "novel",
                "dlc_version": "dlc-test-v1",
                "source_encoding": "utf-8",
                "source_text_hash": story.source_sha256,
                "speed": plan.speed,
            },
        )
        self.assertEqual(
            set(archived.manifest.checksums or {}),
            {"timeline.json", "scenes.json", "source.txt"},
        )

    async def test_disconnect_aborts_without_creating_history(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)

        state = await novel.on_disconnect()

        self.assertEqual(state.status, NovelSessionStatus.IDLE)
        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertEqual(harness.store.list(), [])
        self.assertEqual(harness.clear_calls[-1:], [None])

    async def test_authoritative_session_disconnect_reconciles_novel_state(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)

        await harness.on_disconnect()

        self.assertIsNone(novel.plan)
        state = novel.to_state()
        self.assertEqual(state.status, NovelSessionStatus.IDLE)
        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertEqual(harness.store.list(), [])

    async def test_cancelled_start_reconciles_to_the_started_physical_session(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        original_start = harness.controller.start_planned

        async def start_then_cancel(**kwargs):
            await original_start(**kwargs)
            raise asyncio.CancelledError

        with patch.object(harness.controller, "start_planned", start_then_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await novel.start(plan, story, story_map)

        self.assertEqual(harness.to_state().status, SessionStatus.RUNNING)
        self.assertEqual(harness.to_state().mode, "novel")
        self.assertEqual(novel.to_state().status, NovelSessionStatus.RUNNING)
        self.assertIs(novel.plan, plan)

    async def test_cancelled_pause_reconciles_to_paused_and_retains_plan(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        original_pause = harness.controller.pause

        async def pause_then_cancel():
            await original_pause()
            raise asyncio.CancelledError

        with patch.object(harness.controller, "pause", pause_then_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await novel.pause()

        self.assertEqual(harness.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.PAUSED)
        self.assertIs(novel.plan, plan)

    async def test_estop_rejected_resume_stays_paused_and_can_retry(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await novel.pause()
        harness.game_loop.safety.estop_active = True

        with self.assertRaisesRegex(RuntimeError, "emergency stop"):
            await novel.resume(from_="current")

        self.assertEqual(harness.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.PAUSED)
        self.assertIs(novel.plan, plan)

        harness.game_loop.safety.estop_active = False
        resumed = await novel.resume(from_="current")

        self.assertEqual(resumed.status, NovelSessionStatus.RUNNING)
        self.assertIs(novel.plan, plan)

    async def test_cancelled_resume_reconciles_to_the_resumed_physical_session(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await novel.pause()
        original_resume = harness.controller.resume_planned

        async def resume_then_cancel(cursor):
            await original_resume(cursor)
            raise asyncio.CancelledError

        with patch.object(
            harness.controller, "resume_planned", resume_then_cancel
        ):
            with self.assertRaises(asyncio.CancelledError):
                await novel.resume(from_="current")

        self.assertEqual(harness.to_state().status, SessionStatus.RUNNING)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.RUNNING)
        self.assertIs(novel.plan, plan)

    async def test_clear_failure_reconciles_finishing_until_authoritative_stop(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        harness.game_loop.clear_failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await novel.pause()

        self.assertEqual(harness.to_state().status, SessionStatus.FINISHING)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.FINISHING)
        self.assertIs(novel.plan, plan)

        await harness.stop()

        self.assertIsNone(novel.plan)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)

    async def test_cancelled_finish_reconciles_to_idle_without_swallowing_cancel(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        original_finalize = harness.controller.finalize_finish

        async def finalize_then_cancel(prepared):
            await original_finalize(prepared)
            raise asyncio.CancelledError

        with patch.object(
            harness.controller, "finalize_finish", finalize_then_cancel
        ):
            with self.assertRaises(asyncio.CancelledError):
                await novel.finish()

        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)
        self.assertIsNone(novel.plan)
        self.assertEqual(len(harness.store.list()), 1)

    async def test_cancelled_abort_and_disconnect_reconcile_to_idle(self):
        for operation in ("abort", "on_disconnect"):
            with self.subTest(operation=operation):
                harness, novel, story, story_map, plan = self.make_controller()
                self.addAsyncCleanup(harness.close)
                await novel.start(plan, story, story_map)
                physical_method_name = (
                    "stop" if operation == "abort" else "on_disconnect"
                )
                original_transition = getattr(
                    harness.controller, physical_method_name
                )

                async def transition_then_cancel():
                    await original_transition()
                    raise asyncio.CancelledError

                with patch.object(
                    harness.controller,
                    physical_method_name,
                    transition_then_cancel,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await getattr(novel, operation)()

                self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
                self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)
                self.assertIsNone(novel.plan)
                self.assertEqual(harness.store.list(), [])

    async def test_authoritative_replacement_session_discards_retained_novel_plan(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await novel.pause()
        await harness.stop()
        await harness.start_live()

        state = novel.to_state()

        self.assertEqual(harness.to_state().mode, "autopilot")
        self.assertEqual(state.status, NovelSessionStatus.IDLE)
        self.assertIsNone(novel.plan)

    async def test_runner_detected_disconnect_aborts_novel_without_archive_or_tasks(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        harness.game_loop.disconnect_on_cycle = 1

        await novel.start(plan, story, story_map)
        for _ in range(100):
            if harness.to_state().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)

        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertEqual(novel.to_state().status, NovelSessionStatus.IDLE)
        self.assertIsNone(novel.plan)
        self.assertEqual(harness.store.list(), [])
        self.assertEqual(harness.clear_calls, [None])
        self.assertEqual(harness.runners, {})
        self.assertEqual(harness.controller._runner_watchers, {})
        self.assertIsNone(harness.controller._planned_task)

    async def test_runner_and_explicit_disconnect_converge_on_one_idle_abort(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        harness.game_loop.disconnect_on_cycle = 1
        original_clear = harness.game_loop.clear_output
        clear_entered = asyncio.Event()
        release_clear = asyncio.Event()

        async def block_first_global_clear(channel=None):
            result = await original_clear(channel)
            if channel is None and not clear_entered.is_set():
                clear_entered.set()
                await release_clear.wait()
            return result

        harness.game_loop.clear_output = block_first_global_clear
        await novel.start(plan, story, story_map)
        await asyncio.wait_for(clear_entered.wait(), timeout=0.2)
        explicit_disconnect = asyncio.create_task(novel.on_disconnect())
        await asyncio.sleep(0)
        release_clear.set()
        state = await explicit_disconnect

        self.assertEqual(state.status, NovelSessionStatus.IDLE)
        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)
        self.assertIsNone(novel.plan)
        self.assertEqual(harness.store.list(), [])
        self.assertEqual(harness.clear_calls, [None])
        self.assertEqual(harness.runners, {})
        self.assertEqual(harness.controller._runner_watchers, {})
        self.assertIsNone(harness.controller._planned_task)

    async def test_exact_replay_never_calls_the_chapter_planner_or_resolver(self):
        harness, novel, story, story_map, plan = self.make_controller()
        self.addAsyncCleanup(harness.close)
        await novel.start(plan, story, story_map)
        await self.wait_for_scene(novel, plan.plot_events[0].scene_id)
        harness.clock.advance(200)
        await asyncio.sleep(0)
        summary = await novel.finish()
        resolver_calls = list(harness.resolver.calls)

        with patch(
            "backend.story.planner.ChapterPlanner.plan",
            new=AsyncMock(side_effect=AssertionError("planner called during replay")),
        ) as planning:
            await harness.start_replay(summary.replay_id)
            for _ in range(100):
                if harness.to_state().status is SessionStatus.IDLE:
                    break
                remaining = harness.clock.next_remaining_ms
                if remaining is not None:
                    harness.clock.advance(remaining)
                await asyncio.sleep(0)

        planning.assert_not_awaited()
        self.assertEqual(harness.resolver.calls, resolver_calls)
        self.assertEqual(harness.to_state().status, SessionStatus.IDLE)


if __name__ == "__main__":
    unittest.main()
