import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import unittest

import backend.story as story_domain
from backend.story.analysis_store import AnalysisStore
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
                        ),
                        StoryScene(
                            id=StoryScene.stable_id("source-hash", 0, 1),
                            index=1,
                            start_offset=12,
                            end_offset=30,
                            summary="The door opens.",
                        ),
                    ),
                ),
            ),
        )

    def test_cache_key_changes_with_model_prompt_or_dlc(self):
        base = AnalysisKey("source-hash", "model-a", "faithful-v1", "dlc-v1")

        self.assertNotEqual(base.digest(), replace(base, model="model-b").digest())
        self.assertNotEqual(
            base.digest(), replace(base, prompt_version="faithful-v2").digest()
        )
        self.assertNotEqual(base.digest(), replace(base, dlc_version="dlc-v2").digest())

    def test_story_domain_exports_analysis_cache_contract(self):
        self.assertIs(story_domain.AnalysisKey, AnalysisKey)
        self.assertIs(story_domain.AnalysisStore, AnalysisStore)
        self.assertIs(story_domain.StoryChapter, StoryChapter)
        self.assertIs(story_domain.StoryMap, StoryMap)
        self.assertIs(story_domain.StoryScene, StoryScene)

    def test_scene_map_round_trip_preserves_stable_ids(self):
        store = AnalysisStore(self.cache_directory)

        store.save(self.key, self.scene_map)

        self.assertEqual(store.load(self.key), self.scene_map)
        self.assertEqual(self.scene_map.chapters[0].id, "ch-0001")
        self.assertEqual(self.scene_map.chapters[0].scenes[1].id, "ch-0001-sc-0002")

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


if __name__ == "__main__":
    unittest.main()
