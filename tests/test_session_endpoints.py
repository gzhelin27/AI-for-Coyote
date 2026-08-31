import asyncio
from contextlib import suppress
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch
import warnings

import httpx
from fastapi import WebSocketDisconnect

import backend.main as main_module
from backend.timeline.models import SessionStatus
from backend.timeline.replay_store import ReplayStore
from tests.test_game_loop_timeline import make_game_loop_for_test
from tests.timeline_fakes import SessionHarness


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
        self.assertFalse(self.harness.loop.autopilot)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["replay_id"] for item in listed.json()],
            [finished.json()["replay_id"]],
        )
        self.assertEqual(
            self.state.set_sensors.await_args_list,
            [call(True), call(False), call(True), call(False)],
        )

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
                "open_validated",
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
        self.assertEqual(ws_state, state)
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
        self.assertEqual(reload_states, [SessionStatus.IDLE])
        self.assertEqual(self.harness.controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(len(self.harness.store.list()), 1)
        self.assertEqual(self.harness.cfg["character"]["role"], "新角色")
        self.assertEqual(self.harness.cfg["character"]["profile"], "新风格")
        self.state.set_sensors.assert_awaited_with(False)

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
        self.assertEqual(reload_states, [SessionStatus.IDLE])
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
        self.assertEqual(controller.to_state().status, SessionStatus.PAUSED)
        self.assertEqual(controller.clear_calls, [])

        await controller.stop()

        self.assertEqual(controller.to_state().status, SessionStatus.IDLE)
        self.assertEqual(controller.clear_calls, [None])
        self.assertEqual(controller.store.list(), [])


if __name__ == "__main__":
    unittest.main()
