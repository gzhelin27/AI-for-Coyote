import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import backend.story as story_domain
import backend.story.analysis_store as analysis_store_module
from backend.story.analysis_store import AnalysisStore, AnalysisStoreError
from backend.story.models import AnalysisKey, StoryChapter, StoryMap, StoryScene


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.cache_directory = Path(self.temporary_directory.name) / "analysis"
        self.key = AnalysisKey(
            source_hash="source-hash",
            model="model-a",
            prompt_version="faithful-v1",
            dlc_version="dlc-v1",
        )
        self.scene_map = StoryMap(
            source_hash="source-hash",
            chapters=(
                StoryChapter(
                    id=StoryChapter.stable_id("source-hash", 0),
                    index=0,
                    start_offset=0,
                    end_offset=30,
                    title="Opening",
                    summary="The storm begins.",
                    scenes=(
                        StoryScene(
                            id=StoryScene.stable_id("source-hash", 0, 0),
                            index=0,
                            start_offset=0,
                            end_offset=12,
                            summary="Rain reaches the window.",
                            pace=1.0,
                        ),
                        StoryScene(
                            id=StoryScene.stable_id("source-hash", 0, 1),
                            index=1,
                            start_offset=12,
                            end_offset=30,
                            summary="The door opens.",
                            pace=1.0,
                        ),
                    ),
                ),
            ),
            text_length=30,
        )

    def test_cache_key_changes_with_model_prompt_or_dlc(self):
        base = AnalysisKey("source-hash", "model-a", "faithful-v1", "dlc-v1")

        self.assertNotEqual(base.digest(), replace(base, model="model-b").digest())
        self.assertNotEqual(
            base.digest(), replace(base, prompt_version="faithful-v2").digest()
        )
        self.assertNotEqual(base.digest(), replace(base, dlc_version="dlc-v2").digest())

    def test_analysis_models_include_pace_text_length_and_source_namespace(self):
        self.assertIn("pace", StoryScene.__dataclass_fields__)
        self.assertIn("text_length", StoryMap.__dataclass_fields__)
        self.assertEqual(StoryChapter.stable_id("source-hash", 0), "ch-badb34c8bf5a-0001")
        self.assertEqual(
            StoryScene.stable_id("source-hash", 0, 1),
            "ch-badb34c8bf5a-0001-sc-0002",
        )
        self.assertNotEqual(
            StoryChapter.stable_id("source-hash", 0),
            StoryChapter.stable_id("other-source", 0),
        )

    def test_story_domain_exports_analysis_cache_contract(self):
        self.assertIs(story_domain.AnalysisKey, AnalysisKey)
        self.assertIs(story_domain.AnalysisStore, AnalysisStore)
        self.assertIs(story_domain.AnalysisStoreError, AnalysisStoreError)
        self.assertIs(story_domain.StoryChapter, StoryChapter)
        self.assertIs(story_domain.StoryMap, StoryMap)
        self.assertIs(story_domain.StoryScene, StoryScene)

    def test_scene_map_round_trip_preserves_stable_ids(self):
        store = AnalysisStore(self.cache_directory)

        store.save(self.key, self.scene_map)

        self.assertEqual(store.load(self.key), self.scene_map)
        self.assertEqual(self.scene_map.chapters[0].id, "ch-badb34c8bf5a-0001")
        self.assertEqual(
            self.scene_map.chapters[0].scenes[1].id,
            "ch-badb34c8bf5a-0001-sc-0002",
        )

    def test_models_are_immutable_and_ids_do_not_depend_on_summaries(self):
        chapter = self.scene_map.chapters[0]
        scene = chapter.scenes[0]

        with self.assertRaises(FrozenInstanceError):
            scene.summary = "Changed"

        self.assertEqual(StoryChapter.stable_id("source-hash", 0), chapter.id)
        self.assertEqual(StoryScene.stable_id("source-hash", 0, 0), scene.id)

    def test_load_rejects_invalid_scene_id_and_quarantines_only_cache_file(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["story_map"]["chapters"][0]["scenes"][1]["id"] = "made-from-summary"
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertFalse(cache_path.exists())
        quarantined = list(self.cache_directory.glob("*.invalid"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].parent, self.cache_directory)

    def test_load_rejects_unknown_fields_future_schema_without_using_them(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["future_field"] = {"not": "trusted"}
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertFalse(cache_path.exists())

    def test_load_rejects_boolean_schema_version(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["schema_version"] = True
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertFalse(cache_path.exists())

    def test_load_rejects_source_hash_mismatch(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["story_map"]["source_hash"] = "different-source-hash"
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertFalse(cache_path.exists())

    def test_quarantine_does_not_replace_existing_invalid_cache(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        existing_invalid = self.cache_directory / "prior-cache.invalid"
        existing_invalid.write_text("preserve me", encoding="utf-8")
        cache_path.write_text("not json", encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertEqual(existing_invalid.read_text(encoding="utf-8"), "preserve me")
        self.assertEqual(len(list(self.cache_directory.glob("*.invalid"))), 2)

    def test_load_rejects_out_of_order_scene_offsets(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["story_map"]["chapters"][0]["scenes"][1]["start_offset"] = 11
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        self.assertFalse(cache_path.exists())

    def test_scene_pace_rejects_non_positive_non_finite_and_boolean_values(self):
        scene = self.scene_map.chapters[0].scenes[0]

        for invalid_pace in (0, -1, math.nan, math.inf, -math.inf, True):
            with self.subTest(invalid_pace=invalid_pace), self.assertRaises(ValueError):
                replace(scene, pace=invalid_pace)

    def test_maps_reject_empty_summaries_and_empty_collections(self):
        chapter = self.scene_map.chapters[0]
        scene = chapter.scenes[0]

        with self.assertRaises(ValueError):
            replace(scene, summary="")
        with self.assertRaises(ValueError):
            replace(chapter, summary="")
        with self.assertRaises(ValueError):
            replace(chapter, scenes=())
        with self.assertRaises(ValueError):
            replace(self.scene_map, chapters=())

    def test_maps_reject_leading_internal_and_trailing_scene_gaps(self):
        chapter = self.scene_map.chapters[0]
        first, second = chapter.scenes
        invalid_chapters = (
            replace(chapter, scenes=(replace(first, start_offset=1), second)),
            replace(chapter, scenes=(first, replace(second, start_offset=13))),
            replace(chapter, scenes=(first, replace(second, end_offset=29))),
        )

        for invalid_chapter in invalid_chapters:
            with self.subTest(scenes=invalid_chapter.scenes), self.assertRaises(ValueError):
                replace(self.scene_map, chapters=(invalid_chapter,))

    def test_maps_reject_leading_internal_and_trailing_chapter_gaps(self):
        source_hash = self.key.source_hash
        first_chapter = StoryChapter(
            id=StoryChapter.stable_id(source_hash, 0),
            index=0,
            start_offset=0,
            end_offset=12,
            title="First",
            summary="First chapter.",
            scenes=(
                StoryScene(
                    id=StoryScene.stable_id(source_hash, 0, 0),
                    index=0,
                    start_offset=0,
                    end_offset=12,
                    summary="First scene.",
                    pace=1.0,
                ),
            ),
        )
        second_chapter = StoryChapter(
            id=StoryChapter.stable_id(source_hash, 1),
            index=1,
            start_offset=12,
            end_offset=30,
            title="Second",
            summary="Second chapter.",
            scenes=(
                StoryScene(
                    id=StoryScene.stable_id(source_hash, 1, 0),
                    index=0,
                    start_offset=12,
                    end_offset=30,
                    summary="Second scene.",
                    pace=1.0,
                ),
            ),
        )
        leading = replace(
            first_chapter,
            start_offset=1,
            scenes=(replace(first_chapter.scenes[0], start_offset=1),),
        )
        internal = replace(
            second_chapter,
            start_offset=13,
            scenes=(replace(second_chapter.scenes[0], start_offset=13),),
        )

        with self.assertRaises(ValueError):
            StoryMap(source_hash=source_hash, text_length=30, chapters=(leading,))
        with self.assertRaises(ValueError):
            StoryMap(source_hash=source_hash, text_length=30, chapters=(first_chapter, internal))
        with self.assertRaises(ValueError):
            StoryMap(source_hash=source_hash, text_length=31, chapters=(first_chapter, second_chapter))

    def test_load_rejects_duplicate_keys_and_nested_unknown_fields(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        duplicate_model = cache_path.read_text(encoding="utf-8").replace(
            '"model":"model-a",', '"model":"model-a","model":"other",'
        )
        cache_path.write_text(duplicate_model, encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        store.save(self.key, self.scene_map)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["story_map"]["chapters"][0]["scenes"][0]["future"] = "ignored"
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))

    def test_truncated_utf8_and_embedded_key_mismatch_are_quarantined_then_recover(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        cache_path.write_bytes(b'{"schema_version":2,\xff')

        self.assertIsNone(store.load(self.key))
        store.save(self.key, self.scene_map)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["analysis_key"]["model"] = "different-model"
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(store.load(self.key))
        store.save(self.key, self.scene_map)
        self.assertEqual(store.load(self.key), self.scene_map)

    def test_quarantine_name_collision_preserves_existing_file(self):
        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        collision = cache_path.with_name(f"{cache_path.name}.fixed.invalid")
        collision.write_text("preserved", encoding="utf-8")
        cache_path.write_text("bad", encoding="utf-8")

        with patch.object(analysis_store_module.secrets, "token_hex", side_effect=("fixed", "fresh")):
            self.assertIsNone(store.load(self.key))

        self.assertEqual(collision.read_text(encoding="utf-8"), "preserved")
        self.assertTrue(cache_path.with_name(f"{cache_path.name}.fresh.invalid").exists())

    def test_failed_dump_fsync_or_replace_removes_only_its_temporary_file(self):
        for failure_target in ("json.dump", "os.fsync", "Path.replace"):
            with self.subTest(failure_target=failure_target):
                directory = Path(self.temporary_directory.name) / failure_target.replace(".", "-")
                store = AnalysisStore(directory)
                module_name, attribute = failure_target.split(".")
                target = getattr(analysis_store_module, module_name)
                with patch.object(target, attribute, side_effect=OSError("simulated")):
                    with self.assertRaises(AnalysisStoreError):
                        store.save(self.key, self.scene_map)
                self.assertEqual(list(directory.glob("*.tmp")), [])
                self.assertIsNone(store.load(self.key))

    def test_symlinked_cache_and_analysis_directory_are_not_followed(self):
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        linked_directory = Path(self.temporary_directory.name) / "linked-analysis"
        try:
            linked_directory.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        unsafe_store = AnalysisStore(linked_directory)

        self.assertIsNone(unsafe_store.load(self.key))
        with self.assertRaises(AnalysisStoreError):
            unsafe_store.save(self.key, self.scene_map)

        store = AnalysisStore(self.cache_directory)
        store.save(self.key, self.scene_map)
        cache_path = store.cache_path(self.key)
        external_file = outside / "external.json"
        external_file.write_text("outside", encoding="utf-8")
        cache_path.unlink()
        try:
            cache_path.symlink_to(external_file)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")

        self.assertIsNone(store.load(self.key))
        self.assertEqual(external_file.read_text(encoding="utf-8"), "outside")
        with self.assertRaises(AnalysisStoreError):
            store.save(self.key, self.scene_map)

    def test_concurrent_writers_leave_one_complete_readable_cache(self):
        store_one = AnalysisStore(self.cache_directory)
        store_two = AnalysisStore(self.cache_directory)
        alternative_scene = replace(self.scene_map.chapters[0].scenes[0], summary="Updated rain.")
        alternative_chapter = replace(self.scene_map.chapters[0], scenes=(alternative_scene, self.scene_map.chapters[0].scenes[1]))
        alternative_map = replace(self.scene_map, chapters=(alternative_chapter,))
        barrier = threading.Barrier(3)
        failures = []

        def save(store, story_map):
            try:
                barrier.wait()
                store.save(self.key, story_map)
            except BaseException as exc:
                failures.append(exc)

        first = threading.Thread(target=save, args=(store_one, self.scene_map))
        second = threading.Thread(target=save, args=(store_two, alternative_map))
        first.start()
        second.start()
        barrier.wait()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertIn(store_one.load(self.key), (self.scene_map, alternative_map))
        self.assertEqual(list(self.cache_directory.glob("*.tmp")), [])

    def test_in_process_save_waits_for_failed_load_quarantine(self):
        store_one = AnalysisStore(self.cache_directory)
        store_two = AnalysisStore(self.cache_directory)
        store_one.save(self.key, self.scene_map)
        alternative_scene = replace(self.scene_map.chapters[0].scenes[0], summary="Saved after read.")
        alternative_chapter = replace(self.scene_map.chapters[0], scenes=(alternative_scene, self.scene_map.chapters[0].scenes[1]))
        alternative_map = replace(self.scene_map, chapters=(alternative_chapter,))
        decoder_entered = threading.Event()
        release_decoder = threading.Event()
        writer_started = threading.Event()
        writer_finished = threading.Event()
        reader_result = []

        def fail_decode(*_args):
            decoder_entered.set()
            release_decoder.wait(timeout=5)
            raise analysis_store_module._CacheValidationError("simulated corruption")

        def read():
            reader_result.append(store_one.load(self.key))

        def write():
            writer_started.set()
            store_two.save(self.key, alternative_map)
            writer_finished.set()

        with patch.object(AnalysisStore, "_decode_document", side_effect=fail_decode):
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(decoder_entered.wait(timeout=5))
            writer = threading.Thread(target=write)
            writer.start()
            self.assertTrue(writer_started.wait(timeout=5))
            self.assertFalse(writer_finished.wait(timeout=0.1))
            release_decoder.set()
            reader.join(timeout=5)
            writer.join(timeout=5)

        self.assertEqual(reader_result, [None])
        self.assertTrue(writer_finished.is_set())
        self.assertEqual(store_one.load(self.key), alternative_map)


if __name__ == "__main__":
    unittest.main()
