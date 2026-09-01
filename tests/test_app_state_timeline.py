import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import warnings

import backend.main as main_module
from backend.config import DEFAULTS, reload_character
from backend.timeline.replay_store import ReplaySummary
from backend.timeline.session import SessionController
from tests.test_game_loop_timeline import FakeRelay, relay_output_operations
from tests.timeline_fakes import make_replay_bundle


class LifecycleRelay(FakeRelay):
    async def run(self):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return


class LifecycleSensor:
    def __init__(self):
        self.enabled = False
        self.error = None
        self.running = False

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    def has_frame(self):
        return False

    def to_state(self):
        return {"running": self.running, "error": self.error}


def make_production_config(root: Path):
    cfg = deepcopy(DEFAULTS)
    cfg["app"]["dry_run"] = True
    cfg["autopilot"] = {"enabled": False, "interval_s": 3600}
    cfg["log_dir"] = str(root / "logs")
    cfg["timeline"]["replay_dir"] = str(root / "replays")
    cfg["camera"]["enabled"] = False
    cfg["audio"]["enabled"] = False
    cfg["llm"]["model"] = "model-one"
    cfg["character_file"] = str(root / "character.yaml")
    cfg["character"] = {
        "name": "Production State Test",
        "role": "role-one",
        "role_title": "owner",
        "roles": [
            {
                "name": "role-one",
                "label": "Role One",
                "profiles": [{"name": "profile-one", "available": True}],
            }
        ],
        "profile": "profile-one",
        "profiles": ["profile-one"],
        "profile_available": {"profile-one": True},
        "profile_level": "medium",
        "rage_baseline": 0,
        "player_nick": "tester",
        "dlc_version": "dlc-one",
    }
    cfg["presets"]["呼吸"] = {
        "waveform": "wave-test",
        "label": "呼吸",
        "category": "test",
        "frames": ["0000000000000000"],
        "default_duration_s": 1,
        "max_duration_s": 10,
    }
    return cfg


class ProductionAppStateTimelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = make_production_config(self.root)
        self.relay = LifecycleRelay()
        self.camera = LifecycleSensor()
        self.audio = LifecycleSensor()
        self.llm = SimpleNamespace(
            model="model-one",
            chat=AsyncMock(return_value=("line", [])),
            complete_json=AsyncMock(),
        )
        self.logger = SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            exception=lambda *_args, **_kwargs: None,
        )

    @contextmanager
    def _fake_external_dependencies(self):
        with (
            patch.object(main_module, "setup_logging", return_value=self.logger),
            patch.object(main_module, "RelayClient", return_value=self.relay),
            patch.object(main_module, "LLM", return_value=self.llm),
            patch.object(main_module, "Camera", return_value=self.camera),
            patch.object(main_module, "AudioManager", return_value=self.audio),
        ):
            yield

    def _write_character(self, prompt: str, example: str) -> None:
        Path(self.cfg["character_file"]).write_text(
            f"""role: role-one
profile: profile-one
prompt: {prompt}
roles:
  role-one:
    name: Loaded DLC
    title: owner
    profiles:
      profile-one:
        level: 中
        examples:
          - user: {example} user
            assistant: {example} assistant
""",
            encoding="utf-8",
        )

    async def test_real_app_state_refreshes_seed_and_manifest_metadata_per_session(self):
        self.cfg["character"].pop("dlc_version")
        self._write_character("prompt one", "one")
        with (
            self._fake_external_dependencies(),
            patch.object(
                main_module.secrets,
                "randbits",
                side_effect=(101, 202, 303),
            ),
            patch.object(main_module, "_app_version", return_value="commit-one"),
        ):
            state = main_module.AppState(self.cfg)
            first_state = await state.loop.start_timeline_session()
            first = await state.loop.finish_timeline_session()

            self.cfg["llm"]["model"] = "model-two"
            self._write_character("prompt two", "two")
            second_state = await state.loop.start_timeline_session()
            second = await state.loop.finish_timeline_session()

        self.assertIsInstance(state.timeline_session, SessionController)
        self.assertIs(state.loop.timeline_session, state.timeline_session)
        self.assertEqual(state.replay_store.root, self.root / "replays")
        self.assertNotEqual(first_state.session_id, second_state.session_id)
        self.assertNotEqual(first.seed, second.seed)
        self.assertEqual(
            (first.model, first.dlc_role, first.dlc_profile),
            ("model-one", "role-one", "profile-one"),
        )
        self.assertEqual(
            (second.model, second.dlc_role, second.dlc_profile),
            ("model-two", "role-one", "profile-one"),
        )
        self.assertNotEqual(first.dlc_version, second.dlc_version)

    async def test_manifest_uses_nonempty_app_dlc_and_waveform_policy_provenance(self):
        self.cfg["character"].pop("dlc_version")
        with (
            self._fake_external_dependencies(),
            patch.object(main_module, "_app_version", return_value="commit-provenance"),
        ):
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            summary = await state.loop.finish_timeline_session()

        manifest = state.replay_store.load(summary.replay_id).manifest
        self.assertEqual(manifest.app_commit, "commit-provenance")
        self.assertRegex(manifest.dlc_version, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            manifest.random_profile["waveform_policy"], "all_allowed"
        )

    def test_dlc_fingerprint_covers_effective_behavior_in_canonical_order(self):
        self.cfg["character"].pop("dlc_version")
        self.cfg["character"]["prompt"] = "effective prompt"
        self.cfg["character"]["examples"] = [
            {"user": "first", "assistant": "first reply"}
        ]
        prompt_file = self.root / "effective-prompt.txt"
        prompt_file.write_bytes(b"prompt file one")
        self.cfg["character"]["prompt_file"] = str(prompt_file)

        baseline = main_module._dlc_provenance(self.cfg)
        reordered = deepcopy(self.cfg)
        reordered["character"]["examples"] = [
            {"assistant": "first reply", "user": "first"}
        ]
        self.assertEqual(main_module._dlc_provenance(reordered), baseline)

        inline_prompt = deepcopy(self.cfg)
        inline_prompt["character"]["prompt"] = "changed inline prompt"
        self.assertNotEqual(main_module._dlc_provenance(inline_prompt), baseline)

        examples = deepcopy(self.cfg)
        examples["character"]["examples"] = [
            {"user": "first", "assistant": "changed example"}
        ]
        self.assertNotEqual(main_module._dlc_provenance(examples), baseline)

        prompt_file.write_bytes(b"prompt file two")
        self.assertNotEqual(main_module._dlc_provenance(self.cfg), baseline)

        waveform_policy = deepcopy(self.cfg)
        waveform_policy["timeline"]["waveform_policy"] = "changed-policy"
        self.assertNotEqual(main_module._dlc_provenance(waveform_policy), baseline)

    def test_dlc_fingerprint_uses_only_examples_the_llm_consumes(self):
        self.cfg["character"].pop("dlc_version")
        self.cfg["character"]["examples"] = [
            {"user": f"user {index}", "assistant": f"assistant {index}"}
            for index in range(9)
        ]
        baseline = main_module._dlc_provenance(self.cfg)

        unused_example = deepcopy(self.cfg)
        unused_example["character"]["examples"][8]["assistant"] = "changed unused"
        self.assertEqual(main_module._dlc_provenance(unused_example), baseline)

        used_example = deepcopy(self.cfg)
        used_example["character"]["examples"][7]["assistant"] = "changed used"
        self.assertNotEqual(main_module._dlc_provenance(used_example), baseline)

    def test_app_fingerprint_tracks_allowlisted_runtime_sources_beyond_main(self):
        source_root = self.root / "source"
        (source_root / "backend").mkdir(parents=True)
        (source_root / "backend" / "main.py").write_bytes(b"main")
        safety = source_root / "backend" / "safety.py"
        safety.write_bytes(b"safety one")
        with patch.object(main_module, "PROJECT_ROOT", source_root):
            baseline = main_module._app_version()
            safety.write_bytes(b"safety two")
            changed = main_module._app_version()

        self.assertNotEqual(changed, baseline)

    def test_app_fingerprint_tracks_runtime_configuration_schema_files(self):
        source_root = self.root / "source"
        (source_root / "backend").mkdir(parents=True)
        (source_root / "backend" / "main.py").write_bytes(b"main")
        config = source_root / "config"
        config.mkdir()
        waveforms = config / "waveforms.yaml"
        waveforms.write_bytes(b"presets: {one: {}}")
        with patch.object(main_module, "PROJECT_ROOT", source_root):
            baseline = main_module._app_version()
            waveforms.write_bytes(b"presets: {two: {}}")
            changed = main_module._app_version()

        self.assertNotEqual(changed, baseline)

    def test_frozen_runtime_uses_bundled_fingerprint_without_loose_sources(self):
        source_root = self.root / "frozen-root"
        source_root.mkdir()
        bundle_backend = self.root / "bundle" / "backend"
        bundle_backend.mkdir(parents=True)
        bundled = "source-sha256:" + "a" * 64
        (bundle_backend / "runtime_fingerprint.json").write_text(
            json.dumps({"content_fingerprint": bundled}), encoding="utf-8"
        )
        with (
            patch.object(main_module, "PROJECT_ROOT", source_root),
            patch.object(main_module.sys, "frozen", True, create=True),
            patch.object(main_module, "__file__", str(bundle_backend / "main.py")),
        ):
            self.assertEqual(main_module._runtime_content_fingerprint(), bundled)

    def test_frozen_runtime_without_any_fingerprint_content_fails_closed(self):
        source_root = self.root / "frozen-root"
        source_root.mkdir()
        bundle_backend = self.root / "bundle" / "backend"
        bundle_backend.mkdir(parents=True)
        with (
            patch.object(main_module, "PROJECT_ROOT", source_root),
            patch.object(main_module.sys, "frozen", True, create=True),
            patch.object(main_module, "__file__", str(bundle_backend / "main.py")),
        ):
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                main_module._runtime_content_fingerprint()

    def test_public_and_provenance_versions_ignore_commit_environment_and_git(self):
        source_root = self.root / "source"
        (source_root / "backend").mkdir(parents=True)
        (source_root / "backend" / "main.py").write_bytes(b"main")
        commit = "a" * 40
        with (
            patch.object(main_module, "PROJECT_ROOT", source_root),
            patch.dict(os.environ, {"AI_COYOTE_APP_COMMIT": commit}),
        ):
            self.assertEqual(main_module._public_app_version(), "development")
            self.assertNotIn(commit, main_module._app_version())
            with self._fake_external_dependencies():
                state = main_module.AppState(self.cfg)
            try:
                config_info = state.build_state()["config_info"]
                self.assertEqual(config_info["version"], "development")
                self.assertNotIn(commit, json.dumps(config_info))
            finally:
                state.story_source_store.close()

    async def test_live_session_fingerprint_matches_the_character_passed_to_llm(self):
        self.cfg["character"].pop("dlc_version")
        Path(self.cfg["character_file"]).write_text(
            """role: role-one
profile: profile-one
prompt: loaded prompt
roles:
  role-one:
    name: Loaded DLC
    title: owner
    profiles:
      profile-one:
        level: 中
        examples:
          - user: loaded user
            assistant: loaded assistant
""",
            encoding="utf-8",
        )
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            try:
                await state.loop._autopilot_turn()
                actual_character = self.llm.chat.await_args.args[0]
                expected = main_module._dlc_provenance(state.cfg)
                summary = await state.loop.finish_timeline_session()
            finally:
                if state.timeline_session.to_state().status.value != "idle":
                    await state.loop.stop_timeline_session()

        manifest = state.replay_store.load(summary.replay_id).manifest
        self.assertEqual(actual_character["prompt"], "loaded prompt")
        self.assertEqual(
            actual_character["examples"],
            [{"user": "loaded user", "assistant": "loaded assistant"}],
        )
        self.assertEqual(manifest.dlc_fingerprint, expected)

    async def test_paused_session_resume_keeps_original_character_and_new_session_uses_reload(self):
        self.cfg["character"].pop("dlc_version")
        self._write_character("prompt A", "A")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            character_a = deepcopy(state.loop._timeline_character)
            expected_a_cfg = deepcopy(self.cfg)
            expected_a_cfg["character"] = character_a

            await state.loop.set_autopilot(False)
            self._write_character("prompt B", "B")
            await state.loop.start_timeline_session()
            await state.loop._autopilot_turn()
            first = await state.loop.finish_timeline_session()

            await state.loop.start_timeline_session()
            await state.loop._autopilot_turn()
            second = await state.loop.finish_timeline_session()

        first_manifest = state.replay_store.load(first.replay_id).manifest
        second_manifest = state.replay_store.load(second.replay_id).manifest
        self.assertEqual(self.llm.chat.await_args_list[0].args[0]["prompt"], "prompt A")
        self.assertEqual(self.llm.chat.await_args_list[1].args[0]["prompt"], "prompt B")
        self.assertEqual(
            first_manifest.dlc_fingerprint,
            main_module._dlc_provenance(expected_a_cfg),
        )
        self.assertEqual(second_manifest.dlc_fingerprint, main_module._dlc_provenance(self.cfg))

    async def test_estop_terminal_state_discards_frozen_character_before_normal_turn(self):
        self._write_character("prompt A", "A")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            await state.loop.estop()
            self.assertEqual(state.timeline_session.to_state().status.value, "idle")
            await state.loop.resume()

            self._write_character("prompt B", "B")
            await state.loop.handle_user_message("normal turn")

        self.assertEqual(self.llm.chat.await_args.args[0]["prompt"], "prompt B")

    async def test_direct_timeline_stop_discards_frozen_character_before_normal_turn(self):
        self._write_character("prompt A", "A")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            await state.timeline_session.stop()
            self.assertEqual(state.timeline_session.to_state().status.value, "idle")

            self._write_character("prompt B", "B")
            await state.loop.handle_user_message("normal turn")

        self.assertEqual(self.llm.chat.await_args.args[0]["prompt"], "prompt B")

    async def test_missing_character_file_outside_session_reloads_default_character(self):
        self._write_character("prompt A", "A")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.handle_user_message("loads file")
            Path(self.cfg["character_file"]).unlink()

            await state.loop.handle_user_message("must reload")

        self.assertEqual(
            self.llm.chat.await_args.args[0]["prompt"],
            "你是一个有趣的互动角色。",
        )

    async def test_new_session_after_character_deletion_uses_default_input_and_fingerprint(self):
        self.cfg["character"].pop("dlc_version")
        self._write_character("prompt A", "A")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
            await state.loop.start_timeline_session()
            await state.loop.stop_timeline_session()
            Path(self.cfg["character_file"]).unlink()

            expected_cfg = deepcopy(self.cfg)
            reload_character(expected_cfg)
            await state.loop.start_timeline_session()
            await state.loop._autopilot_turn()
            summary = await state.loop.finish_timeline_session()

        manifest = state.replay_store.load(summary.replay_id).manifest
        self.assertEqual(
            self.llm.chat.await_args.args[0]["prompt"],
            "你是一个有趣的互动角色。",
        )
        self.assertEqual(
            manifest.dlc_fingerprint,
            main_module._dlc_provenance(expected_cfg),
        )
    async def test_manifest_uses_controller_waveform_policy_after_config_drift(self):
        self.cfg["character"].pop("dlc_version")
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
        baseline = state._timeline_manifest_metadata()["dlc_fingerprint"]
        self.cfg["timeline"]["waveform_policy"] = "invalid-after-construction"

        self.assertEqual(
            state._timeline_manifest_metadata()["dlc_fingerprint"], baseline
        )

    def test_public_state_and_replay_summaries_redact_provenance_hashes(self):
        with (
            self._fake_external_dependencies(),
            patch.object(main_module, "_app_version", return_value="source-sha256:private"),
        ):
            state = main_module.AppState(self.cfg)
        bundle = make_replay_bundle([], "completed")
        summary = ReplaySummary.from_manifest(
            replace(
                bundle.manifest,
                app_commit="source-sha256:private",
                dlc_version="sha256:private",
            )
        )

        public_payloads = (
            state.build_state(),
            main_module._session_payload(state.timeline_session),
            main_module._replay_summary_payload(summary),
        )
        for payload in public_payloads:
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("sha256", encoded)
            self.assertNotIn("\"seed\"", encoded)

    def test_source_app_version_fails_closed_without_any_allowlisted_content(self):
        source_root = self.root / "source"
        source_root.mkdir()
        (source_root / "version.txt").unlink(missing_ok=True)
        with patch.object(main_module, "PROJECT_ROOT", source_root):
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                main_module._app_version()

    async def test_real_app_lifespan_stops_active_replay_without_archive_when_auto_clear_off(self):
        self.cfg["safety"]["auto_clear_on_disconnect"] = False
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
        bundle = make_replay_bundle([20], "completed")
        state.replay_store.save(bundle.manifest, bundle.timeline)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with (
                patch.object(main_module, "load_config", return_value=self.cfg),
                patch.object(main_module, "AppState", return_value=state),
            ):
                app = main_module.make_app()

        async with app.router.lifespan_context(app):
            await state.timeline_session.start_replay("replay-1", cursor=0)
            self.assertEqual(state.timeline_session.to_state().mode, "replay")
            self.assertEqual(
                state.timeline_session.to_state().status.value, "replaying"
            )

        self.assertEqual(state.timeline_session.to_state().status.value, "idle")
        self.assertEqual(state.safety.current, {"A": 0, "B": 0})
        self.assertEqual(
            [item.replay_id for item in state.replay_store.list()], ["replay-1"]
        )

    async def test_shutdown_stop_failure_estops_and_reaps_background_tasks(self):
        self.cfg["app"]["dry_run"] = False
        self.cfg["safety"]["auto_clear_on_disconnect"] = False
        with self._fake_external_dependencies():
            state = main_module.AppState(self.cfg)
        bundle = make_replay_bundle([0], "completed")
        state.replay_store.save(bundle.manifest, bundle.timeline)
        await state.timeline_session.start_replay("replay-1", cursor=0)
        for _ in range(40):
            if state.safety.current["A"] == 20:
                break
            await asyncio.sleep(0)
        self.assertEqual(state.safety.current["A"], 20)

        background_started = asyncio.Event()
        background_stopped = asyncio.Event()

        async def background():
            background_started.set()
            try:
                await asyncio.Future()
            finally:
                background_stopped.set()

        background_task = asyncio.create_task(background())
        state.tasks.append(background_task)
        await background_started.wait()
        state.relay.fail_next_clear()
        frames_before = len(state.relay.sent_frames)

        try:
            result = await asyncio.gather(
                state.shutdown(), return_exceptions=True
            )

            self.assertIsInstance(result[0], BaseException)
            operations = relay_output_operations(
                state.relay.sent_frames[frames_before:]
            )
            self.assertGreaterEqual(operations.count(("clear", None)), 2)
            self.assertTrue(state.safety.estop_active)
            self.assertTrue(background_task.done())
            self.assertTrue(background_stopped.is_set())
            self.assertEqual(state.tasks, [])
        finally:
            if not background_task.done():
                background_task.cancel()
            await asyncio.gather(background_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
