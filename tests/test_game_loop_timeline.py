import asyncio
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.config import DEFAULTS
from backend.game_loop import GameLoop
from backend.safety import SafetyManager
from backend.timeline.cycle_runner import RunnerPhase
from backend.timeline.models import CycleGapPolicy
from backend.timeline.replay_store import ReplayStore
from backend.timeline.session import SessionController
from tests.timeline_fakes import (
    ControlledSleeper,
    FakeTimelineResolver,
    SequenceGapRandom,
)


class FakeRelay:
    def __init__(self) -> None:
        self.status = "paired"
        self.controller_id = "controller-test"
        self.clients = {}
        self.sent_frames = []

    def first_client_id(self):
        return "client-test"

    def get_slot_id(self, _client_id=None):
        return "slot-test"

    async def send_frame(self, frame):
        self.sent_frames.append(frame)
        return True

    def to_state(self):
        return {
            "status": self.status,
            "controller_id": self.controller_id,
            "url": "ws://relay.test",
            "clients": [],
            "last_error": "",
        }


def make_game_loop_for_test(
    replay_root: Path,
    *,
    autopilot_interval: float = 12,
    gap_tenths=(5,),
):
    cfg = deepcopy(DEFAULTS)
    cfg["app"]["dry_run"] = True
    cfg["autopilot"] = {"enabled": False, "interval_s": autopilot_interval}
    cfg["character_file"] = str(replay_root / "private-character.yaml")
    cfg["log_dir"] = str(replay_root / "private-logs")
    cfg["character"] = {
        "name": "Timeline Test",
        "role": "触手",
        "role_title": "主人",
        "roles": [
            {
                "name": "触手",
                "label": "触手",
                "profiles": [{"name": "纯爱", "available": True}],
            },
            {
                "name": "装置",
                "label": "装置",
                "profiles": [{"name": "调教", "available": True}],
            },
        ],
        "profile": "纯爱",
        "profiles": ["纯爱"],
        "profile_available": {"纯爱": True},
        "profile_level": "中",
        "rage_baseline": 0,
        "player_nick": "tester",
    }
    raw_frames = ["RAW_FRAME_SECRET_0"]
    cfg["presets"]["呼吸"] = {
        "waveform": "wave_test",
        "label": "呼吸",
        "category": "test",
        "frames": raw_frames,
        "default_duration_s": 5,
        "max_duration_s": 10,
    }
    cfg["llm"]["api_key"] = "API_KEY_SECRET"

    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=(
                "timeline line",
                [{"op": "hold_strength", "channel": "A", "value": 20}],
            )
        )
    )
    safety = SafetyManager(cfg)
    relay = FakeRelay()
    loop = GameLoop(cfg, llm, safety, relay)
    clock = ControlledSleeper()
    resolver = FakeTimelineResolver()
    cycle_rngs = {
        "A": SequenceGapRandom(gap_tenths),
        "B": SequenceGapRandom(()),
    }
    store = ReplayStore(replay_root)
    controller = SessionController(
        game_loop=loop,
        store=store,
        seed=20260831,
        frames={"呼吸": tuple(raw_frames)},
        strength_jitter=4,
        cycle_gap_policy=CycleGapPolicy(),
        clock=lambda: clock.now_ms / 1000,
        sleeper=clock,
        resolver_factory=lambda _seed: resolver,
        cycle_rngs=cycle_rngs,
        timestamp_factory=lambda: "2026-08-31T00:00:00+00:00",
    )
    loop.timeline_session = controller
    return SimpleNamespace(
        cfg=cfg,
        llm=llm,
        safety=safety,
        relay=relay,
        loop=loop,
        clock=clock,
        resolver=resolver,
        cycle_rngs=cycle_rngs,
        store=store,
        controller=controller,
    )


class GameLoopTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.harness = make_game_loop_for_test(Path(self.temporary))

    async def asyncTearDown(self):
        with suppress(Exception):
            await self.harness.controller.stop()

    async def _automatic_turn(self):
        with patch("backend.game_loop.reload_character"):
            return await self.harness.loop._autopilot_turn()

    async def _advance_until_phase(self, channel, phase):
        for _ in range(20):
            if self.harness.controller.runners[channel].state().phase is phase:
                return
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.fail(f"runner {channel} did not reach {phase.value}")

    async def test_automatic_turn_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        result = await self._automatic_turn()

        self.assertEqual(result["line"], "timeline line")
        self.assertEqual(self.harness.controller.to_state().cursor, 1)
        self.assertEqual(
            self.harness.controller.runners["A"].state().phase,
            RunnerPhase.CYCLE,
        )

    async def test_auto_open_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop.auto_open()

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_observation_turn_routes_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop._auto_observe_turn()

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_user_turn_routes_ai_actions_into_running_live_session(self):
        await self.harness.controller.start_live()

        with patch("backend.game_loop.reload_character"):
            await self.harness.loop.handle_user_message("hello")

        self.assertEqual(self.harness.controller.to_state().cursor, 1)

    async def test_cycle_completion_uses_cycle_rng_without_another_llm_turn(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        self.harness.llm.chat.reset_mock()

        await self._advance_until_phase("A", RunnerPhase.GAP)

        self.assertEqual(len(self.harness.resolver.calls), 1)
        self.assertEqual(self.harness.cycle_rngs["A"].calls, 1)
        self.harness.llm.chat.assert_not_awaited()

    async def test_state_exposes_only_current_runner_schedule_fields(self):
        await self.harness.controller.start_live()
        await self._automatic_turn()
        await self._advance_until_phase("A", RunnerPhase.GAP)

        state = self.harness.loop.build_state()

        self.assertEqual(
            set(state["session"]),
            {
                "status",
                "mode",
                "session_id",
                "replay_id",
                "cursor",
                "current_event_id",
                "adjusted",
            },
        )
        self.assertEqual(
            state["runners"]["A"],
            {
                "phase": "gap",
                "pattern": "呼吸",
                "strength": 20,
                "cycle_index": 1,
                "next_cycle_start_ms": 150,
            },
        )

    async def test_autopilot_loop_waits_the_configured_turn_interval(self):
        harness = make_game_loop_for_test(
            Path(self.temporary) / "cadence", autopilot_interval=12
        )
        self.addAsyncCleanup(harness.controller.stop)
        harness.safety.estop_active = True
        observed_timeouts = []

        async def record_wait(awaitable, *, timeout):
            observed_timeouts.append(timeout)
            awaitable.close()
            harness.loop.autopilot_stop.set()
            raise asyncio.TimeoutError

        with patch("backend.game_loop.asyncio.wait_for", side_effect=record_wait):
            await harness.loop._autopilot_loop()

        self.assertEqual(harness.loop.autopilot_interval, 12)
        self.assertEqual(observed_timeouts, [12])
        self.assertFalse(hasattr(harness.controller, "next_interval_s"))

    async def test_manual_pulse_controls_bypass_timeline_scheduling(self):
        await self.harness.controller.start_live()

        for action in (
            {
                "op": "pulse",
                "channel": "A",
                "pattern": "呼吸",
                "duration_s": 1,
            },
            {"op": "pulse_hold", "channel": "B", "pattern": "呼吸"},
        ):
            with self.subTest(op=action["op"]):
                executed, dropped = await self.harness.loop.execute_actions([action])
                self.assertEqual(dropped, [])
                self.assertEqual(len(executed), 1)

        self.assertEqual(self.harness.controller.to_state().cursor, 0)
        self.assertTrue(
            all(
                runner.state().phase is RunnerPhase.IDLE
                for runner in self.harness.controller.runners.values()
            )
        )

    async def test_paused_live_session_blocks_ai_actions_from_direct_fallback(self):
        await self.harness.controller.start_live()
        await self.harness.controller.pause()

        executed, dropped, timeline_managed = (
            await self.harness.loop._execute_ai_actions(
                [{"op": "hold_strength", "channel": "A", "value": 20}]
            )
        )

        self.assertTrue(timeline_managed)
        self.assertEqual(executed, [])
        self.assertEqual(dropped, [])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertEqual(self.harness.controller.to_state().cursor, 0)

    async def test_resume_transition_serializes_against_finish(self):
        await self.harness.controller.start_live()
        await self.harness.controller.pause()
        resume_timeline_session = self.harness.loop.resume_timeline_session
        original_resume = self.harness.controller.resume
        resume_applied = asyncio.Event()
        allow_resume_return = asyncio.Event()

        async def blocking_resume(cursor=None):
            result = await original_resume(cursor)
            resume_applied.set()
            await allow_resume_return.wait()
            return result

        with patch.object(
            self.harness.controller, "resume", side_effect=blocking_resume
        ):
            resume_task = asyncio.create_task(resume_timeline_session(None))
            await asyncio.wait_for(resume_applied.wait(), timeout=0.2)
            finish_task = asyncio.create_task(
                self.harness.loop.finish_timeline_session()
            )
            await asyncio.sleep(0)

            self.assertFalse(finish_task.done())
            allow_resume_return.set()
            await resume_task
            await finish_task

        self.assertFalse(self.harness.loop.autopilot)
        self.assertEqual(self.harness.controller.to_state().status.value, "idle")
        self.assertEqual(len(self.harness.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
