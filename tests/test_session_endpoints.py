import asyncio
from contextlib import suppress
from copy import deepcopy
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch
import warnings

import httpx
from fastapi import WebSocketDisconnect

import backend.main as main_module
from backend.output_coordinator import OutputIntentKind
from backend.timeline.models import SessionStatus
from backend.timeline.player import RecordedCyclePlayer
from backend.timeline.replay_store import ReplayStore
from tests.test_game_loop_timeline import (
    make_game_loop_for_test,
    relay_output_operations,
)
from tests.timeline_fakes import SessionHarness, make_replay_bundle


class FakeSensor:
    enabled = False
    error = None

    async def start(self):
        return None

    async def stop(self):
        return None

    def has_frame(self):
        return False

    def to_state(self):
        return {"running": False, "error": None}


class CapturingWebSocket:
    def __init__(self):
        self.messages = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.messages.append(message)

    async def receive_text(self):
        raise WebSocketDisconnect()


def make_endpoint_state(harness):
    state = main_module.AppState.__new__(main_module.AppState)
    state.cfg = harness.cfg
    state.logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    state.safety = harness.safety
    state.relay = harness.relay
    state.llm = harness.llm
    state.camera = FakeSensor()
    state.audio = FakeSensor()
    state.loop = harness.loop
    state.replay_store = harness.store
    state.timeline_session = harness.controller
    state.ws_clients = set()
    state.tasks = []
    state.auto_opened = True
    state.sensors_on = False
    state.sensor_watch_task = None
    state.timeline_transition_lock = asyncio.Lock()
    state.broadcast_lock = asyncio.Lock()
    state.state_revision = 0
    state.story_source_generation = 0
    state.story_planning_task = None
    state.story_planning_context = None
    state.story_runtime_owner = None
    state.story_source_store_task = None
    state.story_source_io_tasks = set()
    state.story_cleanup_tasks = set()
    state.story_import_requests = set()
    state.story_shutting_down = False
    state.chapter_planner = SimpleNamespace(cancel_pending=lambda: ())
    state.layout = {}
    state.sensor_switches = {"camera": False, "audio": False}
    state.start_background = AsyncMock()
    state.shutdown = AsyncMock()
    state.set_sensors = AsyncMock()
    state.broadcast = AsyncMock()
    state.broadcast_chat = AsyncMock()
    state._on_ws_clients_change = lambda: None
    return state


class SessionEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.replay_root = Path(temporary.name)
        self.harness = make_game_loop_for_test(
            self.replay_root, autopilot_interval=3600, gap_tenths=(5, 0, 0)
        )
        self.state = make_endpoint_state(self.harness)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with (
                patch.object(main_module, "load_config", return_value=self.harness.cfg),
                patch.object(main_module, "AppState", return_value=self.state),
            ):
                self.app = main_module.make_app()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        set_autopilot = self.harness.loop.set_autopilot
        if asyncio.iscoroutinefunction(set_autopilot):
            with suppress(Exception):
                await set_autopilot(False)
        else:
            set_autopilot(False)
        with suppress(Exception):
            await self.harness.controller.stop()

    async def _automatic_turn(self):
        with patch("backend.game_loop.reload_character"):
            return await self.harness.loop._autopilot_turn()

    async def _start_physical_live(self, strength: int) -> None:
        self.harness.safety.dry_run = False
        self.harness.llm.chat.return_value = (
            "timeline line",
            [{"op": "hold_strength", "channel": "A", "value": strength}],
        )
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()
        self.assertEqual(self.harness.safety.current["A"], strength)

    async def _complete_active_cycle(self, channel: str) -> None:
        initial = sum(
            record.channel == channel and record.completed
            for record in self.harness.controller.recorded_cycles
        )
        for _ in range(60):
            completed = [
                record
                for record in self.harness.controller.recorded_cycles
                if record.channel == channel and record.completed
            ]
            if len(completed) > initial:
                return
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.fail("active cycle did not complete")

    async def _complete_one_cycle(self):
        await self._automatic_turn()
        for _ in range(40):
            if self.harness.controller.recorded_cycles:
                return
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.fail("live cycle did not complete")

    async def _finish_replay_with_cycle(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._complete_one_cycle()
        finished = await self.client.post("/api/session/finish")
        self.assertEqual(finished.status_code, 200)
        return finished.json()

    async def _start_active_replay(self):
        summary = await self._finish_replay_with_cycle()
        playing = await self.client.post(
            f"/api/replays/{summary['replay_id']}/play", json={"cursor": 0}
        )
        self.assertEqual(playing.status_code, 200)
        for _ in range(40):
            if self.harness.safety.current["A"]:
                return summary
            await asyncio.sleep(0)
        self.fail("replay did not produce active output")

    async def test_live_session_routes_pause_resume_finish_and_list(self):
        started = await self.client.post("/api/session/start")
        paused = await self.client.post("/api/session/pause")
        resumed = await self.client.post(
            "/api/session/resume", json={"cursor": None}
        )
        finished = await self.client.post("/api/session/finish")
        listed = await self.client.get("/api/replays")

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["status"], "running")
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "running")
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(finished.json()["status"], "completed")
        self.assertIn("title", finished.json())
        self.assertEqual(finished.json()["cycle_count"], 0)
        self.assertNotIn("seed", finished.json())
        self.assertFalse(self.harness.loop.autopilot)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["replay_id"] for item in listed.json()],
            [finished.json()["replay_id"]],
        )
        self.assertEqual(listed.json()[0]["title"], finished.json()["title"])
        self.assertEqual(listed.json()[0]["cycle_count"], finished.json()["cycle_count"])
        self.assertNotIn("seed", listed.json()[0])
        self.assertEqual(
            self.state.set_sensors.await_args_list,
            [call(True), call(False), call(True), call(False)],
        )

    async def test_stalled_replay_list_and_download_keep_event_loop_responsive(self):
        summary = await self._finish_replay_with_cycle()
        replay_id = summary["replay_id"]

        for method_name, request in (
            ("list", lambda: self.client.get("/api/replays")),
            (
                "read_validated",
                lambda: self.client.get(f"/api/replays/{replay_id}/download"),
            ),
        ):
            with self.subTest(method=method_name):
                original = getattr(self.harness.store, method_name)
                started = threading.Event()
                release = threading.Event()

                def stalled(*args, **kwargs):
                    started.set()
                    release.wait(0.25)
                    return original(*args, **kwargs)

                ticked_at: list[float] = []
                started_at = time.perf_counter()

                async def tick() -> None:
                    await asyncio.sleep(0.01)
                    ticked_at.append(time.perf_counter())

                with patch.object(
                    self.harness.store, method_name, side_effect=stalled
                ):
                    response, _ = await asyncio.gather(request(), tick())
                release.set()

                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(started.is_set())
                self.assertLess(ticked_at[0] - started_at, 0.1)

    async def test_stalled_replay_load_never_holds_global_transition_lock(self):
        summary = await self._finish_replay_with_cycle()
        original_load = self.harness.store.load
        started = threading.Event()
        release = threading.Event()

        def stalled_load(*args, **kwargs):
            started.set()
            release.wait(0.25)
            return original_load(*args, **kwargs)

        started_at = time.perf_counter()
        with patch.object(
            self.harness.store, "load", side_effect=stalled_load
        ):
            playing = asyncio.create_task(
                self.client.post(
                    f"/api/replays/{summary['replay_id']}/play",
                    json={"cursor": 0},
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
            async with asyncio.timeout(0.1):
                async with self.state.timeline_transition_lock:
                    acquired_at = time.perf_counter()
            release.set()
            response = await asyncio.wait_for(playing, timeout=0.5)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(acquired_at - started_at, 0.1)
        await self.client.post("/api/replays/playback/stop")

    async def test_shutdown_state_rejects_replay_activation_after_prepare(self):
        summary = await self._finish_replay_with_cycle()
        self.state.story_shutting_down = True

        response = await self.client.post(
            f"/api/replays/{summary['replay_id']}/play",
            json={"cursor": 0},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)

    async def test_replay_player_preparation_runs_before_the_short_start_lock(self):
        summary = await self._finish_replay_with_cycle()
        original_load = RecordedCyclePlayer.load
        load_started = threading.Event()

        def stalled_player_load(player, replay):
            load_started.set()
            time.sleep(0.25)
            return original_load(player, replay)

        loop = asyncio.get_running_loop()
        probe_scheduled_at: list[float] = []
        probe_ran_at: list[float] = []
        probe_ran = asyncio.Event()

        def record_probe() -> None:
            probe_ran_at.append(time.perf_counter())
            probe_ran.set()

        def schedule_probe_from_worker() -> None:
            if not load_started.wait(0.5):
                raise AssertionError("player load did not start")
            probe_scheduled_at.append(time.perf_counter())
            loop.call_soon_threadsafe(record_probe)

        with patch.object(
            RecordedCyclePlayer, "load", new=stalled_player_load
        ):
            probe = asyncio.create_task(
                asyncio.to_thread(schedule_probe_from_worker)
            )
            response = await self.client.post(
                f"/api/replays/{summary['replay_id']}/play",
                json={"cursor": 0},
            )
            await asyncio.wait_for(probe_ran.wait(), timeout=0.5)
            await probe

        self.assertTrue(load_started.is_set())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(probe_ran_at[0] - probe_scheduled_at[0], 0.1)
        await self.client.post("/api/replays/playback/stop")

    async def test_stalled_finish_save_clears_then_releases_global_transition_lock(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200, started.text)
        original_save = self.harness.store.save
        save_started = threading.Event()
        save_release = threading.Event()

        def stalled_save(*args, **kwargs):
            save_started.set()
            save_release.wait(0.25)
            return original_save(*args, **kwargs)

        with patch.object(
            self.harness.store, "save", side_effect=stalled_save
        ):
            finishing = asyncio.create_task(
                self.client.post("/api/session/finish")
            )
            self.assertTrue(await asyncio.to_thread(save_started.wait, 0.5))
            lock_wait_started = time.perf_counter()
            async with asyncio.timeout(0.1):
                async with self.state.timeline_transition_lock:
                    acquired_at = time.perf_counter()
                    self.assertEqual(
                        self.harness.controller.to_state().status.value,
                        "finishing",
                    )
                    self.assertTrue(
                        self.harness.loop.output_clear_is_confirmed(("A", "B"))
                    )
            disconnect_started = time.perf_counter()
            await asyncio.wait_for(
                main_module.AppState.on_relay_event(
                    self.state, "client_disconnected", {}
                ),
                timeout=0.1,
            )
            self.assertLess(
                time.perf_counter() - disconnect_started, 0.1
            )
            estop_started = time.perf_counter()
            estopped = await asyncio.wait_for(
                self.client.post("/api/estop"), timeout=0.1
            )
            self.assertEqual(estopped.status_code, 200, estopped.text)
            self.assertLess(time.perf_counter() - estop_started, 0.1)
            save_release.set()
            response = await asyncio.wait_for(finishing, timeout=0.5)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(acquired_at - lock_wait_started, 0.1)

    async def test_cancelled_stalled_finish_still_finalizes_after_archive_save(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200, started.text)
        original_save = self.harness.store.save
        save_started = threading.Event()
        save_release = threading.Event()

        def stalled_save(*args, **kwargs):
            save_started.set()
            save_release.wait(0.5)
            return original_save(*args, **kwargs)

        with patch.object(
            self.harness.store, "save", side_effect=stalled_save
        ):
            finishing = asyncio.create_task(
                self.client.post("/api/session/finish")
            )
            self.assertTrue(await asyncio.to_thread(save_started.wait, 0.2))
            finishing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(finishing.done())
            save_release.set()
            result = await asyncio.gather(finishing, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )
        self.assertEqual(len(self.harness.store.list()), 1)
        self.assertFalse(self.harness.controller._store_io_tasks)

    async def test_lowering_cap_reduces_active_runner_and_records_current_strength(self):
        await self._start_physical_live(30)
        frames_before = len(self.harness.relay.sent_frames)

        response = await self.client.post(
            "/api/device/channels/cap", json={"channel": "A", "value": 10}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.safety.current["A"], 10)
        self.assertGreater(len(self.harness.relay.sent_frames), frames_before)
        await self._complete_active_cycle("A")
        completed = [
            record
            for record in self.harness.controller.recorded_cycles
            if record.channel == "A" and record.completed
        ]
        self.assertEqual(completed[-1].effective_strength, 10)

    async def test_overheat_reduces_active_runner_through_physical_game_loop_path(self):
        await self._start_physical_live(35)
        frames_before = len(self.harness.relay.sent_frames)
        self.harness.relay.clients = {
            "client-test": {
                "props": {},
                "slotState": {
                    "channelA": {"comfortLimit": {"overheat": True}}
                },
            }
        }

        await self.state.on_relay_event("slots_patch", {})

        self.assertTrue(self.harness.safety.overheat["A"])
        self.assertEqual(self.harness.safety.current["A"], 20)
        self.assertGreater(len(self.harness.relay.sent_frames), frames_before)
        await self._complete_active_cycle("A")
        completed = [
            record
            for record in self.harness.controller.recorded_cycles
            if record.channel == "A" and record.completed
        ]
        self.assertEqual(completed[-1].effective_strength, 20)

    async def test_infinite_app_cap_report_does_not_abort_endpoint_overheat_path(self):
        await self._start_physical_live(30)
        self.harness.relay.clients = {
            "client-test": {
                "props": {},
                "slotState": {
                    "channelA": {
                        "comfortLimit": {
                            "overheat": True,
                            "comfortMax": float("inf"),
                        }
                    }
                },
            }
        }

        await self.state.on_relay_event("slots_patch", {})

        self.assertTrue(self.harness.safety.overheat["A"])
        self.assertIsNone(self.harness.safety.app_caps["A"])
        self.assertEqual(self.harness.safety.current["A"], 20)
        self.assertEqual(
            self.harness.loop.output_coordinator.confirmed("A").strength,
            20,
        )
        self.assertIsNone(
            self.harness.loop.output_coordinator.pending("A").target_strength
        )

    async def test_live_slots_patch_identical_and_changed_strength_keep_owner_active(self):
        # Catches a safe report that invalidates a live session or its helper
        # ownership even though no safety reduction is required.
        await self._start_physical_live(20)

        for reported_strength in (20, 15):
            helper_generation = self.harness.loop.output_coordinator.helper_generation(
                "A"
            )
            self.harness.safety.pulse_until["A"] = 0.0
            self.harness.relay.clients = {
                "client-test": {
                    "props": {"intensityA": reported_strength},
                    "slotState": {},
                }
            }

            await self.state.on_relay_event("slots_patch", {})
            await self._complete_active_cycle("A")
            await self._complete_active_cycle("A")

            self.assertIn("A", self.harness.controller.runners)
            self.assertIsNone(
                self.harness.controller.runners["A"].state().failure
            )
            self.assertEqual(
                self.harness.loop.output_coordinator.helper_generation("A"),
                helper_generation,
            )

    async def test_replay_slots_patch_identical_and_changed_strength_keep_owner_active(self):
        # Catches a safe report that stops replay progression or changes helper
        # ownership without an over-cap safety decision.
        bundle = make_replay_bundle([0, 0, 0], "completed")
        self.harness.store.save(bundle.manifest, bundle.timeline)
        playing = await self.client.post(
            "/api/replays/replay-1/play", json={"cursor": 0}
        )
        self.assertEqual(playing.status_code, 200)

        for _ in range(40):
            if self.harness.controller.to_state().cursor >= 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.harness.controller.to_state().cursor, 1)

        for expected_cursor, reported_strength in ((2, 20), (3, 15)):
            helper_generation = self.harness.loop.output_coordinator.helper_generation(
                "A"
            )
            self.harness.safety.pulse_until["A"] = 0.0
            self.harness.relay.clients = {
                "client-test": {
                    "props": {"intensityA": reported_strength},
                    "slotState": {},
                }
            }
            await self.state.on_relay_event("slots_patch", {})
            remaining = self.harness.clock.next_remaining_ms
            self.assertIsNotNone(remaining)
            self.harness.clock.advance(remaining)
            for _ in range(40):
                if self.harness.controller.to_state().cursor >= expected_cursor:
                    break
                await asyncio.sleep(0)
            self.assertEqual(
                self.harness.controller.to_state().cursor, expected_cursor
            )
            self.assertEqual(
                self.harness.controller.to_state().status,
                SessionStatus.REPLAYING,
            )
            self.assertEqual(
                self.harness.loop.output_coordinator.helper_generation("A"),
                helper_generation,
            )

    async def test_cap_transport_exception_returns_safe_retryable_service_error(self):
        await self._start_physical_live(30)
        self.harness.relay.fail_next_strength_delta(
            "A", OSError("PRIVATE_RELAY_FAILURE_DETAIL")
        )

        response = await self.client.post(
            "/api/device/channels/cap", json={"channel": "A", "value": 10}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "运行时上限物理降档失败"})
        self.assertNotIn("PRIVATE_RELAY_FAILURE_DETAIL", response.text)
        self.assertEqual(self.harness.safety.user_caps["A"], 10)
        self.assertEqual(self.harness.safety.current["A"], 30)
        self.assertEqual(
            self.harness.loop.output_coordinator.pending("A").target_strength,
            10,
        )

    async def test_disable_clear_failure_is_not_persisted_or_reported_as_disabled(self):
        await self._start_physical_live(25)
        self.harness.relay.fail_next_clear("A")

        with patch.object(main_module, "save_device_channels") as save_channels:
            response = await self.client.post(
                "/api/device/channels/enabled",
                json={"channel": "A", "enabled": False},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "通道物理清除失败"})
        save_channels.assert_not_called()
        self.assertTrue(self.harness.safety.enabled["A"])
        self.assertEqual(self.harness.safety.current["A"], 25)
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )

    async def test_disabling_active_channel_physically_clears_and_never_outputs_later(self):
        await self._start_physical_live(25)

        response = await self.client.post(
            "/api/device/channels/enabled",
            json={"channel": "A", "enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.harness.safety.enabled["A"])
        self.assertEqual(self.harness.safety.current["A"], 0)
        frames_after_clear = len(self.harness.relay.sent_frames)
        for _ in range(20):
            remaining = self.harness.clock.next_remaining_ms
            if remaining is not None:
                self.harness.clock.advance(remaining)
            await asyncio.sleep(0)
        self.assertEqual(len(self.harness.relay.sent_frames), frames_after_clear)
        self.assertNotIn("A", self.harness.controller.runners)

    async def test_replay_playback_routes_pause_resume_and_stop(self):
        summary = await self._finish_replay_with_cycle()

        playing = await self.client.post(
            f"/api/replays/{summary['replay_id']}/play", json={"cursor": 0}
        )
        paused = await self.client.post("/api/replays/playback/pause")
        resumed = await self.client.post("/api/replays/playback/resume")
        stopped = await self.client.post("/api/replays/playback/stop")

        self.assertEqual(playing.status_code, 200)
        self.assertEqual(playing.json()["status"], "replaying")
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "replaying")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["status"], "idle")

    async def test_paused_replay_stop_retries_new_pending_clear(self):
        await self._start_active_replay()
        paused = await self.client.post("/api/replays/playback/pause")
        self.assertEqual(paused.status_code, 200)
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_clear()

        failed = await self.client.post("/api/replays/playback/stop")

        self.assertEqual(failed.status_code, 409)
        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.FINISHING,
        )
        for channel in ("A", "B"):
            self.assertTrue(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

        retried = await self.client.post("/api/replays/playback/stop")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "idle")
        for channel in ("A", "B"):
            self.assertFalse(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

    async def test_paused_replay_disconnect_retries_new_pending_clear(self):
        await self._start_active_replay()
        paused = await self.client.post("/api/replays/playback/pause")
        self.assertEqual(paused.status_code, 200)
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_clear()

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await self.state.on_relay_event("client_disconnected", {})

        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.FINISHING,
        )
        for channel in ("A", "B"):
            self.assertTrue(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

        await self.state.on_relay_event("client_disconnected", {})
        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.PAUSED,
        )
        for channel in ("A", "B"):
            self.assertFalse(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

    async def test_paused_live_disconnect_confirms_new_pending_clear(self):
        await self._start_physical_live(20)
        paused = await self.client.post("/api/session/pause")
        self.assertEqual(paused.status_code, 200)
        for channel in ("A", "B"):
            self.assertFalse(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

        await self.state.on_relay_event("client_disconnected", {})

        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.PAUSED,
        )
        for channel in ("A", "B"):
            self.assertFalse(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

    async def test_paused_live_disconnect_clear_failure_stays_retryable(self):
        await self._start_physical_live(20)
        paused = await self.client.post("/api/session/pause")
        self.assertEqual(paused.status_code, 200)
        self.harness.relay.fail_next_clear()

        with self.assertRaisesRegex(RuntimeError, "clear"):
            await self.state.on_relay_event("client_disconnected", {})

        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.FINISHING,
        )
        for channel in ("A", "B"):
            self.assertTrue(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

        await self.state.on_relay_event("client_disconnected", {})
        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.PAUSED,
        )
        for channel in ("A", "B"):
            self.assertFalse(
                self.harness.loop.output_coordinator.pending(
                    channel
                ).clear_required
            )

    async def test_cancelled_replay_pause_endpoint_finishes_physical_clear(self):
        await self._start_active_replay()
        clear_started = asyncio.Event()
        release_clear = asyncio.Event()
        original_clear = self.harness.loop.clear_output

        async def blocked_clear(channel=None):
            clear_started.set()
            await release_clear.wait()
            return await original_clear(channel)

        route = next(
            item
            for item in self.app.routes
            if item.path == "/api/replays/playback/pause"
        )
        with patch.object(
            self.harness.loop, "clear_output", side_effect=blocked_clear
        ):
            request = asyncio.create_task(route.endpoint())
            await asyncio.wait_for(clear_started.wait(), timeout=0.2)
            request.cancel()
            await asyncio.sleep(0)

            self.assertFalse(request.done())
            self.assertGreater(self.harness.safety.current["A"], 0)
            release_clear.set()
            result = await asyncio.gather(request, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        resumed = await self.client.post("/api/replays/playback/resume")
        self.assertEqual(resumed.status_code, 200)

    async def test_cancelled_replay_stop_endpoint_finishes_clear_before_idle(self):
        await self._start_active_replay()
        clear_started = asyncio.Event()
        release_clear = asyncio.Event()
        original_clear = self.harness.loop.clear_output

        async def blocked_clear(channel=None):
            clear_started.set()
            await release_clear.wait()
            return await original_clear(channel)

        route = next(
            item
            for item in self.app.routes
            if item.path == "/api/replays/playback/stop"
        )
        with patch.object(
            self.harness.loop, "clear_output", side_effect=blocked_clear
        ):
            request = asyncio.create_task(route.endpoint())
            await asyncio.wait_for(clear_started.wait(), timeout=0.2)
            request.cancel()
            await asyncio.sleep(0)

            self.assertFalse(request.done())
            release_clear.set()
            result = await asyncio.gather(request, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )

    async def test_cancelled_replay_resume_endpoint_owns_transition_to_completion(self):
        await self._start_active_replay()
        paused = await self.client.post("/api/replays/playback/pause")
        self.assertEqual(paused.status_code, 200)
        resume_started = asyncio.Event()
        release_resume = asyncio.Event()
        original_resume = self.harness.controller.resume

        async def blocked_resume(cursor=None):
            result = await original_resume(cursor)
            resume_started.set()
            await release_resume.wait()
            return result

        route = next(
            item
            for item in self.app.routes
            if item.path == "/api/replays/playback/resume"
        )
        with patch.object(
            self.harness.controller, "resume", side_effect=blocked_resume
        ):
            request = asyncio.create_task(route.endpoint())
            await asyncio.wait_for(resume_started.wait(), timeout=0.2)
            request.cancel()
            await asyncio.sleep(0)

            self.assertFalse(request.done())
            release_resume.set()
            result = await asyncio.gather(request, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.REPLAYING
        )

    async def test_manual_action_pauses_live_before_unrecorded_output(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()
        event_cursor = self.harness.controller.to_state().cursor
        operations = []
        original_execute = self.harness.loop._execute_actions_locked

        async def record_actions(actions):
            operations.append([dict(action) for action in actions])
            return await original_execute(actions)

        with patch.object(
            self.harness.loop,
            "_execute_actions_locked",
            side_effect=record_actions,
        ):
            response = await self.client.post(
                "/api/manual",
                json={"op": "hold_strength", "channel": "A", "value": 7},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        self.assertEqual(self.harness.controller.to_state().cursor, event_cursor)
        self.assertEqual(self.harness.safety.current["A"], 7)
        self.assertEqual(operations[-1][0]["op"], "hold_strength")
        self.assertTrue(
            any(batch[0].get("op") == "stop" for batch in operations[:-1])
        )

    async def test_manual_action_stops_replay_before_output_and_keeps_archive_exact(self):
        summary = await self._start_active_replay()
        replay_id = summary["replay_id"]

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 6},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )
        self.assertEqual(self.harness.safety.current["A"], 6)
        before = len(self.harness.relay.sent_frames)
        self.harness.clock.advance(10000)
        await asyncio.sleep(0)
        self.assertEqual(len(self.harness.relay.sent_frames), before)
        stored = self.harness.store.load(replay_id)
        self.assertFalse(stored.manifest.adjusted)

    async def test_repeated_live_manual_action_clears_previous_manual_output(self):
        await self._start_physical_live(20)
        first = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 7},
        )
        self.assertEqual(first.status_code, 200)
        frame_count = len(self.harness.relay.sent_frames)

        second = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 6},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.harness.safety.current["A"], 6)
        operations = relay_output_operations(
            self.harness.relay.sent_frames[frame_count:]
        )
        self.assertTrue(any(item[0] == "clear" for item in operations))
        self.assertTrue(
            any(item[0] == "strength_delta" for item in operations)
        )

    async def test_idle_manual_after_replay_takeover_keeps_unrelated_output(self):
        await self._start_active_replay()
        self.harness.safety.dry_run = False
        first = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 7},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )
        frame_count = len(self.harness.relay.sent_frames)

        second = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "B", "value": 6},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.harness.safety.current, {"A": 7, "B": 6})
        operations = relay_output_operations(
            self.harness.relay.sent_frames[frame_count:]
        )
        self.assertFalse(any(item[0] == "clear" for item in operations))
        self.assertTrue(
            any(item[0] == "strength_delta" for item in operations)
        )

    async def test_idle_manual_after_live_takeover_stop_keeps_unrelated_output(self):
        await self._start_physical_live(20)
        first = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 7},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        stopped = await self.harness.loop.stop_timeline_session()
        self.assertEqual(stopped.status, SessionStatus.IDLE)
        frame_count = len(self.harness.relay.sent_frames)

        second = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "B", "value": 6},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.harness.safety.current, {"A": 7, "B": 6})
        operations = relay_output_operations(
            self.harness.relay.sent_frames[frame_count:]
        )
        self.assertFalse(any(item[0] == "clear" for item in operations))
        self.assertTrue(
            any(item[0] == "strength_delta" for item in operations)
        )

    async def test_failed_live_prerequisite_clear_blocks_manual_device_output(self):
        await self._start_physical_live(20)
        self.harness.relay.fail_next_clear()
        frame_count = len(self.harness.relay.sent_frames)

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 7},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count + 4)
        self.assertEqual(self.harness.safety.current["A"], 20)
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("B").clear_required
        )
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.FINISHING
        )

    async def test_live_transport_false_creates_no_cycle_record(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)

        await self._automatic_turn()
        for _ in range(20):
            if "A" not in self.harness.controller.runners:
                break
            await asyncio.sleep(0)

        self.assertEqual(self.harness.controller.recorded_cycles, ())
        self.assertEqual(self.harness.store.list(), [])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertFalse(
            any(
                operation[0] == "waveform"
                for operation in relay_output_operations(
                    self.harness.relay.sent_frames
                )
            )
        )

    async def test_live_transport_exception_creates_no_cycle_record(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta(
            "A", RuntimeError("injected live transport exception")
        )
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)

        await self._automatic_turn()
        for _ in range(20):
            if "A" not in self.harness.controller.runners:
                break
            await asyncio.sleep(0)

        self.assertEqual(self.harness.controller.recorded_cycles, ())
        self.assertEqual(self.harness.store.list(), [])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertFalse(
            any(
                operation[0] == "waveform"
                for operation in relay_output_operations(
                    self.harness.relay.sent_frames
                )
            )
        )

    async def test_replay_strength_failure_sends_no_waveform_frame(self):
        summary = await self._finish_replay_with_cycle()
        self.harness.safety.dry_run = False
        self.harness.relay.sent_frames.clear()
        self.harness.relay.fail_next_strength_delta("A")

        playing = await self.client.post(
            f"/api/replays/{summary['replay_id']}/play",
            json={"cursor": 0},
        )
        self.assertEqual(playing.status_code, 200)
        for _ in range(40):
            await asyncio.sleep(0)
            if self.harness.relay.sent_frames:
                break

        operations = relay_output_operations(self.harness.relay.sent_frames)
        self.assertTrue(
            any(operation[0] == "strength_delta" for operation in operations)
        )
        self.assertFalse(
            any(operation[0] == "waveform" for operation in operations)
        )

    async def test_timeline_cycle_without_strength_prerequisite_sends_nothing(self):
        self.harness.safety.dry_run = False
        generations = self.harness.loop.begin_timeline_output(("A",))
        self.harness.relay.sent_frames.clear()

        executed, dropped = await self.harness.loop.execute_timeline_actions(
            [
                {
                    "op": "pulse_cycle",
                    "channel": "A",
                    "pattern": "呼吸",
                }
            ],
            generations,
        )

        self.assertEqual(executed, [])
        self.assertTrue(dropped)
        self.assertEqual(self.harness.relay.sent_frames, [])

    async def test_failed_replay_prerequisite_clear_blocks_manual_device_output(self):
        await self._start_active_replay()
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_clear()
        frame_count = len(self.harness.relay.sent_frames)

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 6},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count + 4)
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("B").clear_required
        )
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.FINISHING
        )

    async def test_failed_manual_primary_cleans_up_default_wave_helper(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dropped"])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertIsNone(self.harness.loop.patterns["A"])
        self.assertNotIn("A", self.harness.loop.loop_tasks)
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertIsNone(confirmed.waveform)
        self.assertIsNone(confirmed.waveform_mode)
        self.assertFalse(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )

    async def test_manual_primary_exception_cleans_up_default_wave_helper(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta(
            "A", RuntimeError("injected transport exception")
        )

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dropped"])
        self.assertEqual(self.harness.safety.current["A"], 0)
        self.assertIsNone(self.harness.loop.patterns["A"])
        self.assertNotIn("A", self.harness.loop.loop_tasks)

    async def test_cancelled_primary_retires_queued_helper_without_blocking_estop(self):
        self.harness.safety.dry_run = False
        self.harness.cfg["playback"]["loop_batch_s"] = 2.0
        self.harness.cfg["playback"]["loop_overlap_s"] = 0.3
        helper_delivered = asyncio.Event()
        resend_queued = asyncio.Event()
        primary_cancelled = asyncio.Event()
        original_send = self.harness.relay.send_frame
        original_run_locked = (
            self.harness.loop.output_coordinator._run_channel_locked
        )
        helper_task = None
        resend_task = None
        run_count = 0

        async def observe_run_locked(*args, **kwargs):
            nonlocal resend_task, run_count
            run_count += 1
            if run_count == 2:
                resend_task = asyncio.current_task()
                resend_queued.set()
            return await original_run_locked(*args, **kwargs)

        async def cancel_primary(frame):
            nonlocal helper_task
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            data = inner.get("data", {}) if isinstance(inner, dict) else {}
            if inner.get("m") == "device.op" and data.get("t") == 0:
                sent = await original_send(frame)
                helper_delivered.set()
                return sent
            if inner.get("m") == "device.op" and data.get("t") == 3:
                await helper_delivered.wait()
                helper_task = self.harness.loop.loop_tasks.get("A")
                await resend_queued.wait()
                primary_cancelled.set()
                raise asyncio.CancelledError
            return await original_send(frame)

        request = None
        stopping = None
        try:
            with (
                patch.object(
                    self.harness.loop.output_coordinator,
                    "_run_channel_locked",
                    side_effect=observe_run_locked,
                ),
                patch.object(
                    self.harness.relay, "send_frame", side_effect=cancel_primary
                ),
            ):
                request = asyncio.create_task(
                    self.harness.loop.execute_manual_action(
                        {"op": "hold_strength", "channel": "A", "value": 5}
                    )
                )
                await helper_delivered.wait()
                await resend_queued.wait()
                await primary_cancelled.wait()
                stopping = asyncio.create_task(self.harness.loop.estop())
                done, _pending = await asyncio.wait(
                    {request, stopping}, timeout=0.5
                )

            self.assertIn(request, done)
            self.assertIn(stopping, done)
            result = await asyncio.gather(
                request, stopping, return_exceptions=True
            )
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertFalse(isinstance(result[1], BaseException))
            self.assertTrue(result[1]["estop"])
            self.assertIsNotNone(helper_task)
            self.assertTrue(helper_task.done())
            self.assertNotIn("A", self.harness.loop.loop_tasks)
            self.assertNotIn("A", self.harness.loop.loop_events)
            confirmed = self.harness.loop.output_coordinator.confirmed("A")
            self.assertEqual(confirmed.strength, 0)
            self.assertIsNone(confirmed.waveform)
            self.assertIsNone(confirmed.waveform_mode)
            self.assertFalse(
                self.harness.loop.output_coordinator.pending("A").clear_required
            )
            helper_frames = len(
                [
                    frame
                    for frame in self.harness.relay.sent_frames
                    if frame.get("data", {}).get("data", {}).get("t") == 0
                ]
            )
            frame_count = len(self.harness.relay.sent_frames)
            await asyncio.sleep(0)
            self.assertEqual(len(self.harness.relay.sent_frames), frame_count)
            self.assertEqual(helper_frames, 1)
        finally:
            if resend_task is not None and not resend_task.done():
                resend_task.cancel()
            self.harness.loop._cancel_loops(None)
            for task in (request, stopping, helper_task, resend_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (request, stopping, helper_task, resend_task) if task),
                return_exceptions=True,
            )

    async def test_failed_helper_cleanup_is_confirmed_and_blocks_later_output(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        self.harness.relay.fail_next_clear("A")

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dropped"])
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertEqual(confirmed.waveform, "呼吸")
        self.assertEqual(confirmed.waveform_mode, "finite")
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertNotIn("A", self.harness.loop.loop_tasks)

        frame_count = len(self.harness.relay.sent_frames)
        blocked = await self.client.post(
            "/api/manual",
            json={"op": "pulse", "channel": "A", "pattern": "呼吸"},
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertTrue(blocked.json()["dropped"])
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count)

    async def test_exceptional_helper_clear_fails_closed_and_blocks_later_output(self):
        # Catches a helper-clear exception that escapes rollback before it
        # publishes the finite conservative waveform and durable clear work.
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        self.harness.relay.fail_next_clear("A", RuntimeError("clear exploded"))

        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dropped"])
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertEqual(confirmed.waveform, "呼吸")
        self.assertEqual(confirmed.waveform_mode, "finite")
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertNotIn("A", self.harness.loop.loop_tasks)

        frame_count = len(self.harness.relay.sent_frames)
        blocked = await self.client.post(
            "/api/manual",
            json={"op": "pulse", "channel": "A", "pattern": "呼吸"},
        )

        self.assertEqual(blocked.status_code, 200)
        self.assertTrue(blocked.json()["dropped"])
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count)

    async def test_missing_relay_ids_cannot_confirm_empty_helper_cleanup(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        connected = True
        original_send = self.harness.relay.send_frame

        def client_id():
            return "client-test" if connected else None

        def slot_id(_client_id=None):
            return "slot-test" if connected else None

        async def disconnect_on_primary(frame):
            nonlocal connected
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            data = inner.get("data", {}) if isinstance(inner, dict) else {}
            if inner.get("m") == "device.op" and data.get("t") == 3:
                connected = False
            return await original_send(frame)

        with (
            patch.object(
                self.harness.relay, "first_client_id", side_effect=client_id
            ),
            patch.object(
                self.harness.relay, "get_slot_id", side_effect=slot_id
            ),
            patch.object(
                self.harness.relay,
                "send_frame",
                side_effect=disconnect_on_primary,
            ),
        ):
            response = await self.client.post(
                "/api/manual",
                json={"op": "hold_strength", "channel": "A", "value": 5},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dropped"])
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertEqual(confirmed.waveform, "呼吸")
        self.assertEqual(confirmed.waveform_mode, "finite")
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )

    async def test_cancelled_failed_helper_cleanup_still_blocks_later_output(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        self.harness.relay.fail_next_clear("A")
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        original_send = self.harness.relay.send_frame

        async def blocked_cleanup(frame):
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            if inner.get("m") == "device.op.clear":
                cleanup_started.set()
                await release_cleanup.wait()
            return await original_send(frame)

        with patch.object(
            self.harness.relay, "send_frame", side_effect=blocked_cleanup
        ):
            request = asyncio.create_task(
                self.harness.loop.execute_manual_action(
                    {"op": "hold_strength", "channel": "A", "value": 5}
                )
            )
            await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)
            request.cancel()
            await asyncio.sleep(0)

            self.assertFalse(request.done())
            release_cleanup.set()
            result = await asyncio.gather(request, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertEqual(confirmed.waveform, "呼吸")
        self.assertEqual(confirmed.waveform_mode, "finite")
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(self.harness.safety.pulse_active()["A"])

        frame_count = len(self.harness.relay.sent_frames)
        blocked = await self.client.post(
            "/api/manual",
            json={"op": "pulse", "channel": "A", "pattern": "呼吸"},
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertTrue(blocked.json()["dropped"])
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count)

    async def test_cancelled_helper_clear_fails_closed_before_reraising(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_strength_delta("A")
        helper_generation = (
            self.harness.loop.output_coordinator.helper_generation("A")
        )
        original_send = self.harness.relay.send_frame
        helper_task = None

        async def cancel_clear(frame):
            nonlocal helper_task
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            data = inner.get("data", {}) if isinstance(inner, dict) else {}
            if inner.get("m") == "device.op" and data.get("t") == 3:
                helper_task = self.harness.loop.loop_tasks.get("A")
            if inner.get("m") == "device.op.clear":
                raise asyncio.CancelledError
            return await original_send(frame)

        with patch.object(
            self.harness.relay, "send_frame", side_effect=cancel_clear
        ):
            result = await asyncio.gather(
                self.harness.loop.execute_manual_action(
                    {"op": "hold_strength", "channel": "A", "value": 5}
                ),
                return_exceptions=True,
            )

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertIsNotNone(helper_task)
        self.assertTrue(helper_task.done())
        self.assertNotIn("A", self.harness.loop.loop_tasks)
        self.assertNotIn("A", self.harness.loop.loop_events)
        self.assertEqual(
            self.harness.loop.output_coordinator.helper_generation("A"),
            helper_generation + 1,
        )
        confirmed = self.harness.loop.output_coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 0)
        self.assertEqual(confirmed.waveform, "呼吸")
        self.assertEqual(confirmed.waveform_mode, "finite")
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(self.harness.safety.pulse_active()["A"])

    async def test_cancelled_failed_temp_revert_still_requires_clear(self):
        self.harness.safety.dry_run = False
        self.harness.safety.pulse_until["A"] = float("inf")
        revert_started = asyncio.Event()
        release_revert = asyncio.Event()
        scheduled = []
        original_send = self.harness.relay.send_frame
        original_schedule = self.harness.loop._schedule_temp_revert

        async def blocked_revert(frame):
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            data = inner.get("data", {}) if isinstance(inner, dict) else {}
            sent = await original_send(frame)
            if inner.get("m") == "device.op" and data.get("t") == 7:
                revert_started.set()
                await release_revert.wait()
                return False
            return sent

        def capture_revert(*args, **kwargs):
            before = set(asyncio.all_tasks())
            original_schedule(*args, **kwargs)
            scheduled.extend(set(asyncio.all_tasks()) - before)

        with (
            patch("backend.game_loop.asyncio.sleep", new=AsyncMock(return_value=None)),
            patch.object(
                self.harness.relay, "send_frame", side_effect=blocked_revert
            ),
            patch.object(
                self.harness.loop,
                "_schedule_temp_revert",
                side_effect=capture_revert,
            ),
        ):
            executed, dropped = await self.harness.loop.execute_manual_action(
                {
                    "op": "temp_strength",
                    "channel": "A",
                    "value": 5,
                    "duration_s": 1,
                }
            )
            self.assertEqual(dropped, [])
            self.assertTrue(executed)
            await asyncio.wait_for(revert_started.wait(), timeout=0.2)
            self.assertEqual(len(scheduled), 1)
            scheduled[0].cancel()
            self.assertFalse(scheduled[0].done())
            release_revert.set()
            result = await asyncio.gather(scheduled[0], return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(
            self.harness.loop.output_coordinator.confirmed("A").strength, 5
        )
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )

    async def test_cancel_after_temp_transport_still_owns_revert_scheduling(self):
        self.harness.safety.dry_run = False
        self.harness.safety.pulse_until["A"] = float("inf")
        transport_committed = asyncio.Event()
        release_boundary = asyncio.Event()
        revert_scheduled = asyncio.Event()
        scheduled = []
        original_transaction = self.harness.loop._run_action_transaction
        original_schedule = self.harness.loop._schedule_temp_revert

        async def expose_post_transport_boundary(**kwargs):
            result = await original_transaction(**kwargs)
            if kwargs["cmd"]["kind"] == "temp":
                transport_committed.set()
                await release_boundary.wait()
            return result

        def capture_revert(*args, **kwargs):
            before = set(asyncio.all_tasks())
            original_schedule(*args, **kwargs)
            scheduled.extend(set(asyncio.all_tasks()) - before)
            revert_scheduled.set()

        with (
            patch.object(
                self.harness.loop,
                "_run_action_transaction",
                side_effect=expose_post_transport_boundary,
            ),
            patch.object(
                self.harness.loop,
                "_schedule_temp_revert",
                side_effect=capture_revert,
            ),
        ):
            request = asyncio.create_task(
                self.harness.loop.execute_manual_action(
                    {
                        "op": "temp_strength",
                        "channel": "A",
                        "value": 5,
                        "duration_s": 10,
                    }
                )
            )
            await asyncio.wait_for(transport_committed.wait(), timeout=0.2)
            self.assertEqual(
                self.harness.loop.output_coordinator.confirmed("A").strength,
                5,
            )

            request.cancel()
            result = await asyncio.gather(request, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertTrue(revert_scheduled.is_set())
        self.assertEqual(len(scheduled), 1)
        scheduled[0].cancel()
        await asyncio.gather(scheduled[0], return_exceptions=True)

    async def test_global_stop_result_maps_both_effective_channels(self):
        response = await self.client.post("/api/manual", json={"op": "stop"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dropped"], [])
        effective = response.json()["executed"][0]["effective"]
        self.assertEqual(effective["op"], "stop")
        self.assertEqual(set(effective["channels"]), {"A", "B"})
        for channel in ("A", "B"):
            self.assertEqual(
                effective["channels"][channel],
                {
                    "effective_strength": 0,
                    "pattern": None,
                    "waveform_mode": None,
                },
            )

    async def test_estop_preempts_queued_disable_and_finishes_globally_clear(self):
        self.harness.safety.dry_run = False
        self.harness.safety.pulse_until["A"] = float("inf")
        generations = self.harness.loop.begin_timeline_output(("A",))
        strength_started = asyncio.Event()
        release_strength = asyncio.Event()
        original_send = self.harness.relay.send_frame
        blocked = False

        async def gated_send(frame):
            nonlocal blocked
            inner = frame.get("data", {}) if isinstance(frame, dict) else {}
            data = inner.get("data", {}) if isinstance(inner, dict) else {}
            if (
                not blocked
                and inner.get("m") == "device.op"
                and data.get("t") == 3
            ):
                blocked = True
                strength_started.set()
                await release_strength.wait()
            return await original_send(frame)

        with patch.object(
            self.harness.relay, "send_frame", side_effect=gated_send
        ):
            normal = asyncio.create_task(
                self.harness.loop.execute_timeline_actions(
                    [
                        {
                            "op": "hold_strength",
                            "channel": "A",
                            "value": 8,
                        }
                    ],
                    generations,
                )
            )
            await asyncio.wait_for(strength_started.wait(), timeout=0.2)
            disabling = asyncio.create_task(
                self.harness.loop.set_channel_enabled("A", False)
            )
            await asyncio.sleep(0)
            stopping = asyncio.create_task(self.harness.loop.estop())
            await asyncio.sleep(0)
            release_strength.set()
            normal_result, disable_result, estop_result = await asyncio.gather(
                normal, disabling, stopping, return_exceptions=True
            )

        self.assertFalse(isinstance(normal_result, BaseException))
        self.assertIsInstance(disable_result, Exception)
        self.assertFalse(isinstance(estop_result, BaseException))
        self.assertTrue(estop_result["estop"])
        for channel in ("A", "B"):
            confirmed = self.harness.loop.output_coordinator.confirmed(channel)
            self.assertEqual(confirmed.strength, 0)
            self.assertIsNone(confirmed.waveform)
            self.assertIsNone(confirmed.waveform_mode)
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertFalse(
            self.harness.loop.output_coordinator.pending("B").clear_required
        )

    async def test_idle_physical_estop_always_sends_global_clear(self):
        self.harness.safety.dry_run = False
        self.harness.relay.sent_frames.clear()

        response = await self.client.post("/api/estop")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sent"])
        operations = relay_output_operations(self.harness.relay.sent_frames)
        self.assertIn(("clear", None), operations)
        self.assertIn(("reset", 0), operations)
        self.assertIn(("reset", 1), operations)

    async def test_resume_releases_estop_only_after_confirmed_clear(self):
        self.harness.safety.dry_run = False
        self.harness.relay.fail_next_clear()

        stopped = await self.client.post("/api/estop")

        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["sent"])
        self.assertTrue(self.harness.safety.estop_active)
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("A").clear_required
        )
        self.assertTrue(
            self.harness.loop.output_coordinator.pending("B").clear_required
        )

        self.harness.relay.fail_next_clear()
        failed_resume = await self.client.post("/api/resume")
        failed_state = (await self.client.get("/api/state")).json()
        frames_before_blocked = len(self.harness.relay.sent_frames)
        blocked = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(failed_resume.status_code, 503)
        self.assertTrue(failed_state["estop"])
        self.assertTrue(self.harness.safety.estop_active)
        self.assertTrue(blocked.json()["dropped"])
        self.assertEqual(len(self.harness.relay.sent_frames), frames_before_blocked)

        resumed = await self.client.post("/api/resume")
        manual = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )
        resumed_state = (await self.client.get("/api/state")).json()

        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed_state["estop"])
        self.assertFalse(manual.json()["dropped"])
        self.assertEqual(self.harness.safety.current["A"], 5)

    async def test_stale_timeline_generation_cannot_reach_transport(self):
        self.harness.safety.dry_run = False
        generations = self.harness.loop.begin_timeline_output(("A",))
        self.harness.loop.output_coordinator.invalidate(
            "A", OutputIntentKind.CLEAR_OR_DISABLE
        )
        frame_count = len(self.harness.relay.sent_frames)

        executed, dropped = await self.harness.loop.execute_timeline_actions(
            [
                {
                    "op": "pulse",
                    "channel": "A",
                    "pattern": "呼吸",
                    "duration_s": 1,
                }
            ],
            generations,
        )

        self.assertEqual(executed, [])
        self.assertTrue(dropped)
        self.assertEqual(len(self.harness.relay.sent_frames), frame_count)

    async def test_idle_manual_action_remains_available(self):
        response = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["dropped"])
        self.assertEqual(self.harness.safety.current["A"], 5)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )

    async def test_repeated_idle_manual_action_does_not_add_session_clear(self):
        self.harness.safety.dry_run = False
        first = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 5},
        )
        self.assertEqual(first.status_code, 200)
        frame_count = len(self.harness.relay.sent_frames)

        second = await self.client.post(
            "/api/manual",
            json={"op": "hold_strength", "channel": "A", "value": 6},
        )

        self.assertEqual(second.status_code, 200)
        operations = relay_output_operations(
            self.harness.relay.sent_frames[frame_count:]
        )
        self.assertFalse(any(item[0] == "clear" for item in operations))
        self.assertEqual(self.harness.safety.current["A"], 6)

    async def test_download_uses_safe_name_and_rejects_missing_or_traversal_ids(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        finished = await self.client.post("/api/session/finish")
        replay_id = finished.json()["replay_id"]

        downloaded = await self.client.get(f"/api/replays/{replay_id}/download")
        missing = await self.client.get("/api/replays/missing-replay/download")
        traversal = await self.client.get(
            "/api/replays/..%5Cprivate-character.yaml/download"
        )

        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.content.startswith(b"PK"))
        self.assertEqual(
            downloaded.headers["content-disposition"],
            f'attachment; filename="{replay_id}.coyote-replay"',
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(traversal.status_code, 400)
        self.assertNotIn(str(self.replay_root), downloaded.headers["content-disposition"])

    async def test_download_streams_the_validated_open_archive_not_swapped_path(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        finished = await self.client.post("/api/session/finish")
        replay_id = finished.json()["replay_id"]
        archive_path = self.harness.store._path(replay_id)
        original_bytes = archive_path.read_bytes()
        swapped_bytes = b"SWAPPED_AFTER_VALIDATION"

        class SwappingDownloadStore:
            root = self.replay_root

            @staticmethod
            def _validate_replay_id(value):
                ReplayStore._validate_replay_id(value)

            def _path(self, value):
                return archive_path

            def load(self, value):
                bundle = self.harness.store.load(value)
                archive_path.write_bytes(swapped_bytes)
                return bundle

            def open_validated(self, value):
                self._validate_replay_id(value)
                opened = io.BytesIO(original_bytes)
                archive_path.write_bytes(swapped_bytes)
                return opened

            def read_validated(self, value):
                with self.open_validated(value) as opened:
                    return opened.read()

        swapping_store = SwappingDownloadStore()
        swapping_store.harness = self.harness
        self.state.replay_store = swapping_store

        downloaded = await self.client.get(
            f"/api/replays/{replay_id}/download"
        )

        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, original_bytes)
        self.assertEqual(archive_path.read_bytes(), swapped_bytes)

    async def test_download_closes_validated_handle_before_response_send_can_fail(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        finished = await self.client.post("/api/session/finish")
        replay_id = finished.json()["replay_id"]
        archive_path = self.harness.store._path(replay_id)
        archive_file = io.BytesIO(archive_path.read_bytes())
        download_route = next(
            route
            for route in self.app.routes
            if route.path == "/api/replays/{replay_id}/download"
        )

        try:
            with patch.object(
                self.harness.store,
                "_open_contained",
                return_value=archive_file,
            ):
                response = await download_route.endpoint(replay_id)

            self.assertTrue(archive_file.closed)

            async def receive():
                return {"type": "http.disconnect"}

            async def failing_send(_message):
                raise OSError("client disconnected during response send")

            with self.assertRaisesRegex(OSError, "client disconnected"):
                await response(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "method": "GET",
                        "path": f"/api/replays/{replay_id}/download",
                        "headers": [],
                    },
                    receive,
                    failing_send,
                )
            self.assertTrue(archive_file.closed)
        finally:
            archive_file.close()

    async def test_http_and_websocket_state_redact_internal_timeline_data(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()

        response = await self.client.get("/api/state")
        state = response.json()
        ws_route = next(route for route in self.app.routes if route.path == "/ws")
        websocket = CapturingWebSocket()
        await ws_route.endpoint(websocket)
        ws_state = websocket.messages[0]["data"]

        self.assertEqual(response.status_code, 200)
        self.assertTrue(websocket.accepted)
        self.assertGreater(ws_state["state_revision"], state["state_revision"])
        ws_content = dict(ws_state)
        http_content = dict(state)
        ws_content.pop("state_revision")
        http_content.pop("state_revision")
        self.assertEqual(ws_content, http_content)
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
                "channels",
            },
        )
        self.assertEqual(state["session"]["channels"], state["runners"])
        self.assertEqual(
            set(state["runners"]["A"]),
            {
                "phase",
                "pattern",
                "strength",
                "cycle_index",
                "next_cycle_start_ms",
            },
        )
        self.assertNotIn("character_file", state["config_info"])
        self.assertNotIn("waveforms_file", state["config_info"])

        def all_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from all_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from all_keys(child)

        blob = json.dumps(state, ensure_ascii=False)
        forbidden_keys = {"seed", "frames", "source", "random_profile", "api_key"}
        self.assertTrue(forbidden_keys.isdisjoint(set(all_keys(state))))
        self.assertNotIn("RAW_FRAME_SECRET_0", blob)
        self.assertNotIn("API_KEY_SECRET", blob)
        self.assertNotIn("private-character.yaml", blob)
        self.assertNotIn(str(self.replay_root), blob)

    async def test_automatic_off_pauses_clears_and_does_not_archive(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()
        self.assertEqual(self.harness.safety.current["A"], 20)

        stopped = await self.client.post("/api/autopilot", json={"enabled": False})

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.store.list(), [])

    async def test_chat_commit_invalidates_a_blocked_current_autopilot_turn(self):
        autopilot_llm_started = asyncio.Event()
        release_autopilot_llm = asyncio.Event()
        chat_lines = []
        llm_calls = 0

        async def record_ai_turn(result):
            chat_lines.append(result["line"])

        async def blocked_autopilot_then_user(*_args, **_kwargs):
            nonlocal llm_calls
            llm_calls += 1
            if llm_calls == 1:
                autopilot_llm_started.set()
                await release_autopilot_llm.wait()
                return (
                    "stale autopilot line",
                    [{"op": "hold_strength", "channel": "A", "value": 40}],
                )
            return "user line", []

        self.harness.loop.autopilot_interval = 0.001
        self.harness.llm.chat.side_effect = blocked_autopilot_then_user
        self.harness.loop.on_ai_turn = record_ai_turn
        try:
            started = await self.client.post("/api/session/start")
            self.assertEqual(started.status_code, 200)
            await asyncio.wait_for(autopilot_llm_started.wait(), timeout=0.2)
            self.harness.loop.autopilot_interval = 3600

            user_response = await self.client.post(
                "/api/chat", json={"message": "user survives"}
            )
            expected_history = [
                {"role": "user", "content": "user survives"},
                {"role": "assistant", "content": "user line"},
            ]
            self.assertEqual(user_response.status_code, 200)
            self.assertEqual(user_response.json()["line"], "user line")
            self.assertEqual(self.harness.loop.history, expected_history)

            release_autopilot_llm.set()

            async def wait_for_autopilot_turn_to_settle():
                while self.harness.loop.turn_busy:
                    await asyncio.sleep(0)

            await asyncio.wait_for(
                wait_for_autopilot_turn_to_settle(), timeout=0.2
            )
            self.assertEqual(
                (
                    self.harness.loop.turn_count,
                    self.harness.loop.history,
                    chat_lines,
                    self.harness.safety.current["A"],
                ),
                (1, expected_history, [], 0),
            )
        finally:
            release_autopilot_llm.set()
            with suppress(Exception):
                await self.client.post("/api/session/stop")

    async def test_history_clear_finishes_live_session_before_mutation(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        self.harness.loop.history = [{"role": "user", "content": "keep me"}]

        cleared = await self.client.post("/api/history/clear", json={})

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.harness.loop.history, [])
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(len(self.harness.store.list()), 1)
        self.state.set_sensors.assert_awaited_with(False)

    async def test_history_finish_archive_save_never_holds_global_lock(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        self.harness.loop.history = [{"role": "user", "content": "keep me"}]
        original_save = self.harness.store.save
        save_started = threading.Event()
        save_release = threading.Event()

        def stalled_save(*args, **kwargs):
            save_started.set()
            save_release.wait(0.5)
            return original_save(*args, **kwargs)

        with patch.object(
            self.harness.store, "save", side_effect=stalled_save
        ):
            clearing = asyncio.create_task(
                self.client.post("/api/history/clear", json={})
            )
            self.assertTrue(await asyncio.to_thread(save_started.wait, 0.2))
            async with asyncio.timeout(0.1):
                async with self.state.timeline_transition_lock:
                    self.assertEqual(
                        self.harness.controller.to_state().status,
                        SessionStatus.FINISHING,
                    )
                    self.assertTrue(self.harness.loop.history)
            save_release.set()
            cleared = await asyncio.wait_for(clearing, timeout=0.5)

        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(self.harness.loop.history, [])
        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.IDLE
        )

    async def test_role_switch_finishes_live_session_before_saving_profile(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        save_observations = []

        def save_after_finish(cfg, *, role=None, profile=None, **_values):
            save_observations.append(
                (self.harness.controller.to_state().status, len(self.harness.store.list()))
            )
            cfg["character"]["role"] = role
            cfg["character"]["profile"] = profile

        with (
            patch.object(main_module, "reload_character"),
            patch.object(
                main_module, "save_character_runtime", side_effect=save_after_finish
            ),
        ):
            switched = await self.client.post(
                "/api/character/profile",
                json={"role": "装置", "profile": "调教"},
            )

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(save_observations, [(SessionStatus.IDLE, 1)])
        self.assertEqual(self.harness.cfg["character"]["role"], "装置")
        self.state.set_sensors.assert_awaited_with(False)

    async def test_profile_switch_reloads_authoritative_roles_before_validation(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        reload_states = []

        def load_new_role(cfg):
            reload_states.append(self.harness.controller.to_state().status)
            cfg["character"]["roles"].append(
                {
                    "name": "新角色",
                    "label": "新角色",
                    "profiles": [{"name": "新风格", "available": True}],
                }
            )

        def save_new_role(cfg, *, role=None, profile=None, **_values):
            cfg["character"]["role"] = role
            cfg["character"]["profile"] = profile

        with (
            patch.object(main_module, "reload_character", side_effect=load_new_role),
            patch.object(
                main_module,
                "save_character_runtime",
                side_effect=save_new_role,
            ),
        ):
            switched = await self.client.post(
                "/api/character/profile",
                json={"role": "新角色", "profile": "新风格"},
            )

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(reload_states, [SessionStatus.RUNNING])
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(len(self.harness.store.list()), 1)
        self.assertEqual(self.harness.cfg["character"]["role"], "新角色")
        self.assertEqual(self.harness.cfg["character"]["profile"], "新风格")
        self.state.set_sensors.assert_awaited_with(False)

    async def _assert_invalid_profile_request_preserves_live_state(
        self, body, *, reload_side_effect=None
    ):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        self.state.sensors_on = True
        before_state = (await self.client.get("/api/state")).json()
        before_character = deepcopy(self.harness.cfg["character"])
        runtime_path = self.replay_root / "character_runtime.yaml"
        self.state.set_sensors.reset_mock()
        self.state.broadcast.reset_mock()

        with (
            patch.object(
                main_module,
                "reload_character",
                side_effect=reload_side_effect,
            ),
            patch("backend.config.CHARACTER_RUNTIME_FILE", runtime_path),
        ):
            response = await self.client.post(
                "/api/character/profile", json=body
            )

        after_state = (await self.client.get("/api/state")).json()
        self.assertEqual(response.status_code, 400)
        before_state.pop("state_revision", None)
        after_state.pop("state_revision", None)
        self.assertEqual(after_state, before_state)
        self.assertEqual(
            self.harness.controller.to_state().status,
            SessionStatus.RUNNING,
        )
        self.assertTrue(self.harness.loop.autopilot)
        self.assertTrue(self.state.sensors_on)
        self.assertEqual(self.harness.store.list(), [])
        self.assertEqual(self.harness.cfg["character"], before_character)
        self.assertFalse(runtime_path.exists())
        self.state.set_sensors.assert_not_awaited()
        self.state.broadcast.assert_not_awaited()
        return response

    async def test_invalid_role_does_not_finish_or_mutate_live_session(self):
        response = await self._assert_invalid_profile_request_preserves_live_state(
            {"role": "不存在", "profile": "纯爱"}
        )
        self.assertIn("未知角色", response.json()["error"])

    async def test_invalid_profile_does_not_finish_or_mutate_live_session(self):
        response = await self._assert_invalid_profile_request_preserves_live_state(
            {"role": "触手", "profile": "不存在"}
        )
        self.assertIn("未知风格", response.json()["error"])

    async def test_unavailable_dlc_does_not_finish_or_mutate_live_session(self):
        def mark_dlc_unavailable(candidate_cfg):
            for role in candidate_cfg["character"]["roles"]:
                if role["name"] == "装置":
                    role["profiles"][0]["available"] = False

        response = await self._assert_invalid_profile_request_preserves_live_state(
            {"role": "装置", "profile": "调教"},
            reload_side_effect=mark_dlc_unavailable,
        )
        self.assertEqual(response.json()["detail"], "dlc_missing")

    async def test_estop_aborts_live_session_without_archive_or_implicit_reset(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()

        stopped = await self.client.post("/api/estop")
        blocked = await self.client.post("/api/session/start")

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(self.harness.safety.estop_active)
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(self.harness.store.list(), [])
        self.state.set_sensors.assert_awaited_with(False)

        resumed = await self.client.post("/api/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(self.harness.safety.estop_active)
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)

    async def test_estop_prevents_concurrent_finish_from_saving_archive(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        original_quiesce = self.harness.controller._quiesce_runners
        original_estop = self.harness.safety.estop
        quiesced = asyncio.Event()
        estop_applied = asyncio.Event()
        allow_finish = asyncio.Event()

        async def blocking_quiesce(*args, **kwargs):
            await original_quiesce(*args, **kwargs)
            quiesced.set()
            await allow_finish.wait()

        def observed_estop():
            result = original_estop()
            estop_applied.set()
            return result

        with (
            patch.object(
                self.harness.controller,
                "_quiesce_runners",
                side_effect=blocking_quiesce,
            ),
            patch.object(
                self.harness.safety, "estop", side_effect=observed_estop
            ),
        ):
            finishing = asyncio.create_task(
                self.client.post("/api/session/finish")
            )
            await asyncio.wait_for(quiesced.wait(), timeout=0.2)
            stopping = asyncio.create_task(self.client.post("/api/estop"))
            await asyncio.wait_for(estop_applied.wait(), timeout=0.2)
            allow_finish.set()
            finished, stopped = await asyncio.gather(finishing, stopping)

        self.assertEqual(finished.status_code, 409)
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(self.harness.safety.estop_active)
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(self.harness.store.list(), [])

    async def test_disconnect_pauses_and_clears_without_archive(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()

        await self.state.on_relay_event("client_disconnected", {})

        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.store.list(), [])
        self.state.set_sensors.assert_awaited_with(False)

    async def test_disconnect_session_lifecycle_cannot_be_disabled(self):
        self.harness.cfg["safety"]["auto_clear_on_disconnect"] = False
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        await self._automatic_turn()

        await self.state.on_relay_event("client_disconnected", {})

        self.assertEqual(
            self.harness.controller.to_state().status, SessionStatus.PAUSED
        )
        self.assertEqual(self.harness.safety.current, {"A": 0, "B": 0})
        self.assertEqual(self.harness.store.list(), [])

    async def test_concurrent_start_and_conflicting_cursor_return_4xx(self):
        first, second = await asyncio.gather(
            self.client.post("/api/session/start"),
            self.client.post("/api/session/start"),
        )

        self.assertEqual(sorted((first.status_code, second.status_code)), [200, 409])
        paused = await self.client.post("/api/session/pause")
        self.assertEqual(paused.status_code, 200)
        bad_cursor = await self.client.post(
            "/api/session/resume", json={"cursor": 0}
        )
        self.assertEqual(bad_cursor.status_code, 400)
        self.assertNotIn(str(self.replay_root), bad_cursor.text)

    async def test_start_response_and_sensors_are_atomic_against_finish(self):
        sensors_started = asyncio.Event()
        allow_sensors = asyncio.Event()

        async def blocking_sensors(on):
            if on:
                sensors_started.set()
                await allow_sensors.wait()

        self.state.set_sensors.side_effect = blocking_sensors
        starting = asyncio.create_task(self.client.post("/api/session/start"))
        await asyncio.wait_for(sensors_started.wait(), timeout=0.2)
        finishing = asyncio.create_task(self.client.post("/api/session/finish"))
        await asyncio.sleep(0)

        self.assertFalse(finishing.done())
        allow_sensors.set()
        started, finished = await asyncio.gather(starting, finishing)

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["status"], "running")
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(finished.json()["status"], "completed")

    async def test_history_finish_sensor_and_mutation_block_a_new_start(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        self.harness.loop.history = [{"role": "user", "content": "keep me"}]
        sensors_stopping = asyncio.Event()
        allow_sensors = asyncio.Event()
        mutation_states = []
        original_clear = self.harness.loop.clear_history

        async def blocking_sensors(on):
            if not on:
                sensors_stopping.set()
                await allow_sensors.wait()

        def observed_clear():
            mutation_states.append(self.harness.controller.to_state().status)
            original_clear()

        self.state.set_sensors.side_effect = blocking_sensors
        with patch.object(
            self.harness.loop, "clear_history", side_effect=observed_clear
        ):
            clearing = asyncio.create_task(
                self.client.post("/api/history/clear", json={})
            )
            await asyncio.wait_for(sensors_stopping.wait(), timeout=0.2)
            starting = asyncio.create_task(self.client.post("/api/session/start"))
            await asyncio.sleep(0)

            self.assertFalse(starting.done())
            allow_sensors.set()
            cleared, restarted = await asyncio.gather(clearing, starting)

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(restarted.status_code, 200)
        self.assertEqual(mutation_states, [SessionStatus.IDLE])
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.RUNNING)

    async def test_profile_reload_finish_sensor_and_save_block_a_new_start(self):
        started = await self.client.post("/api/session/start")
        self.assertEqual(started.status_code, 200)
        sensors_stopping = asyncio.Event()
        allow_sensors = asyncio.Event()
        reload_states = []
        save_states = []

        async def blocking_sensors(on):
            if not on:
                sensors_stopping.set()
                await allow_sensors.wait()

        def observed_reload(_cfg):
            reload_states.append(self.harness.controller.to_state().status)

        def observed_save(cfg, *, role=None, profile=None, **_values):
            save_states.append(self.harness.controller.to_state().status)
            cfg["character"]["role"] = role
            cfg["character"]["profile"] = profile

        self.state.set_sensors.side_effect = blocking_sensors
        with (
            patch.object(main_module, "reload_character", side_effect=observed_reload),
            patch.object(
                main_module, "save_character_runtime", side_effect=observed_save
            ),
        ):
            switching = asyncio.create_task(
                self.client.post(
                    "/api/character/profile",
                    json={"role": "装置", "profile": "调教"},
                )
            )
            await asyncio.wait_for(sensors_stopping.wait(), timeout=0.2)
            starting = asyncio.create_task(self.client.post("/api/session/start"))
            await asyncio.sleep(0)

            self.assertFalse(starting.done())
            allow_sensors.set()
            switched, restarted = await asyncio.gather(switching, starting)

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(restarted.status_code, 200)
        self.assertEqual(reload_states, [SessionStatus.RUNNING])
        self.assertEqual(save_states, [SessionStatus.IDLE])


class SessionFinishCancellationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_finish_before_cleanup_is_retryable_by_stop(self):
        controller = SessionHarness.create(seed=47)
        self.addAsyncCleanup(controller.close)
        await controller.start_live()
        original = controller._runner_watchers.pop("A")
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        cancellation_started = asyncio.Event()

        async def slow_cancel_watcher():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await asyncio.Future()

        controller._runner_watchers["A"] = asyncio.create_task(
            slow_cancel_watcher()
        )
        await asyncio.sleep(0)

        finishing = asyncio.create_task(controller.finish())
        await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
        finishing.cancel()
        result = await asyncio.gather(finishing, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(controller.to_state().status, SessionStatus.FINISHING)
        self.assertEqual(controller.clear_calls, [])

        await controller.stop()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])


if __name__ == "__main__":
    unittest.main()
