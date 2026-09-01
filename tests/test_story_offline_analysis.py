import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from backend.story.analysis_store import AnalysisStore
import backend.story as story_domain
from backend.story.offline_analysis import (
    OFFLINE_ANALYSIS_VERSION,
    OFFLINE_PRODUCER,
    OfflineAnalysisError,
    OfflineAnalysisImporter,
    offline_analysis_key,
)
from backend.story.source import StorySourceLoader


class CountingAnalysisStore(AnalysisStore):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.save_count = 0

    def save(self, key, story_map) -> None:
        self.save_count += 1
        super().save(key, story_map)


class OfflineAnalysisImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.root = root
        self.source_path = root / "novel.txt"
        self.candidate_directory = root / "data" / "story_candidates"
        self.candidate_directory.mkdir(parents=True)
        self.candidate_path = self.candidate_directory / "candidate.json"
        self.source_text = "第一章\n甲乙\n第二章\n丙丁"
        self.source_path.write_text(self.source_text, encoding="utf-8")
        self.loader = StorySourceLoader(max_bytes=1024 * 1024)
        self.story = self.loader.load(
            self.source_path.name, self.source_path.read_bytes(), encoding="utf-8"
        )
        self.store = CountingAnalysisStore(root / "analysis")
        self.importer = OfflineAnalysisImporter(
            self.loader, self.store, candidate_directory=self.candidate_directory
        )
        self.candidate = self._valid_candidate()
        self._write_candidate(self.candidate)

    def test_import_candidate_uses_real_source_identity_and_saves_once(self):
        result = self.importer.import_candidate(
            self.source_path,
            self.candidate_path,
            encoding="utf-8",
            dlc_version="dlc1-v1",
        )

        self.assertEqual(result.key.model, "codex-offline")
        self.assertEqual(result.key.prompt_version, "faithful-offline-v1")
        self.assertEqual(result.story_map.source_hash, self.story.source_sha256)
        self.assertEqual(result.chapter_count, 2)
        self.assertEqual(result.scene_count, 3)
        self.assertEqual(self.store.save_count, 1)
        self.assertEqual(self.store.load(result.key), result.story_map)

    def test_story_domain_exports_offline_import_contract(self):
        self.assertIs(story_domain.OfflineAnalysisImporter, OfflineAnalysisImporter)
        self.assertIs(story_domain.OfflineAnalysisError, OfflineAnalysisError)
        self.assertEqual(story_domain.OFFLINE_PRODUCER, OFFLINE_PRODUCER)
        self.assertEqual(story_domain.OFFLINE_ANALYSIS_VERSION, OFFLINE_ANALYSIS_VERSION)

    def test_validate_never_writes(self):
        result = self.importer.validate(
            self.source_path,
            self.candidate_path,
            encoding="auto",
            dlc_version="dlc1-v1",
        )

        self.assertEqual(result.story_map.text_length, len(self.source_text))
        self.assertEqual(self.store.save_count, 0)
        self.assertIsNone(self.store.load(result.key))

    def test_validation_fails_closed_for_untrusted_candidate_shapes_and_content(self):
        invalid_cases = {
            "extra root key": lambda payload: payload.update({"future": 1}),
            "forged source hash": lambda payload: payload.update({"source_hash": "forged"}),
            "wrong text length": lambda payload: payload.update({"text_length": len(self.source_text) + 1}),
            "wrong stable id": lambda payload: payload["chapters"][0].update({"id": "wrong"}),
            "chapter gap": lambda payload: payload["chapters"][1].update({"start_offset": 8}),
            "scene overlap": lambda payload: payload["chapters"][0]["scenes"][1].update({"start_offset": 3}),
            "scene disorder": lambda payload: payload["chapters"][0]["scenes"].reverse(),
            "out of range": lambda payload: payload["chapters"][1].update({"end_offset": len(self.source_text) + 1}),
            "empty summary": lambda payload: payload["chapters"][0]["scenes"][0].update({"summary": " \t"}),
            "invalid pace": lambda payload: payload["chapters"][0]["scenes"][0].update({"pace": 0}),
        }
        for name, mutate in invalid_cases.items():
            with self.subTest(name=name):
                candidate = self._valid_candidate()
                mutate(candidate)
                self._write_candidate(candidate)
                with self.assertRaises(OfflineAnalysisError):
                    self.importer.import_candidate(
                        self.source_path, self.candidate_path, encoding="utf-8", dlc_version="dlc1-v1"
                    )
                self.assertEqual(self.store.save_count, 0)

    def test_validation_rejects_duplicate_malformed_deep_and_oversize_json_without_cache(self):
        cases = {
            "duplicate": b'{"source_hash":"a","source_hash":"b","text_length":1,"chapters":[]}',
            "malformed": b'{',
            "deep": (b"[" * 80) + b"0" + (b"]" * 80),
            "oversize": b" " * (2 * 1024 * 1024),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.candidate_path.write_bytes(payload)
                with self.assertRaises(OfflineAnalysisError):
                    self.importer.import_candidate(
                        self.source_path, self.candidate_path, encoding="utf-8", dlc_version="dlc1-v1"
                    )
                self.assertEqual(self.store.save_count, 0)

    def test_validation_rejects_candidate_outside_configured_directory(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self._valid_candidate()), encoding="utf-8")

        with self.assertRaises(OfflineAnalysisError):
            self.importer.validate(
                self.source_path, outside, encoding="utf-8", dlc_version="dlc1-v1"
            )

    def test_validation_rejects_symlinked_candidate_file(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self._valid_candidate()), encoding="utf-8")
        linked = self.candidate_directory / "linked.json"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")

        with self.assertRaises(OfflineAnalysisError):
            self.importer.validate(
                self.source_path, linked, encoding="utf-8", dlc_version="dlc1-v1"
            )

    def test_validation_rejects_junction_candidate_directory(self):
        if os.name != "nt":
            self.skipTest("junctions are a Windows-only filesystem feature")
        outside = self.root / "outside-candidates"
        outside.mkdir()
        junction = self.root / "junction-candidates"
        command = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if command.returncode != 0:
            self.skipTest(f"mklink /J is unavailable: {command.stderr or command.stdout}")
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        candidate = outside / "candidate.json"
        candidate.write_text(json.dumps(self._valid_candidate()), encoding="utf-8")
        importer = OfflineAnalysisImporter(self.loader, self.store, candidate_directory=junction)

        with self.assertRaises(OfflineAnalysisError):
            importer.validate(
                self.source_path, candidate, encoding="utf-8", dlc_version="dlc1-v1"
            )

    def test_validation_rejects_escaped_surrogate_and_huge_pace_as_typed_errors(self):
        cases = {
            "surrogate summary": b'{"source_hash":"placeholder","text_length":1,"chapters":["\\ud800"]}',
            "huge pace": 10**1000,
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                if name == "surrogate summary":
                    payload = value.replace(b'"placeholder"', f'"{self.story.source_sha256}"'.encode())
                    self.candidate_path.write_bytes(payload)
                else:
                    candidate = self._valid_candidate()
                    candidate["chapters"][0]["scenes"][0]["pace"] = value
                    self._write_candidate(candidate)
                with self.assertRaises(OfflineAnalysisError):
                    self.importer.validate(
                        self.source_path, self.candidate_path, encoding="utf-8", dlc_version="dlc1-v1"
                    )
                self.assertIsNone(
                    self.store.load(offline_analysis_key(self.story, "dlc1-v1"))
                )

    def test_normalized_source_variants_produce_the_same_offline_key_and_stable_ids(self):
        first = self.root / "lf.txt"
        second = self.root / "crlf.txt"
        first.write_text("Alpha\nBeta", encoding="utf-8")
        second.write_bytes(b"Alpha\r\nBeta")
        first_candidate = self.candidate_directory / "lf.json"
        second_candidate = self.candidate_directory / "crlf.json"
        first_story = self.loader.load(first.name, first.read_bytes(), encoding="utf-8")
        candidate = {
            "source_hash": first_story.source_sha256,
            "text_length": 10,
            "chapters": [{"id": f"ch-{first_story.source_sha256}-0001", "index": 0, "start_offset": 0, "end_offset": 10, "title": "", "summary": "完整摘要。", "scenes": [{"id": f"ch-{first_story.source_sha256}-0001-sc-0001", "index": 0, "start_offset": 0, "end_offset": 10, "summary": "完整场景。", "pace": 1.0}]}],
        }
        first_candidate.write_text(json.dumps(candidate), encoding="utf-8")
        second_candidate.write_text(json.dumps(candidate), encoding="utf-8")

        first_result = self.importer.validate(first, first_candidate, encoding="utf-8", dlc_version="dlc1-v1")
        second_result = self.importer.validate(second, second_candidate, encoding="utf-8", dlc_version="dlc1-v1")

        self.assertEqual(first_result.key, second_result.key)
        self.assertEqual(first_result.story_map.chapters[0].id, second_result.story_map.chapters[0].id)

    def _valid_candidate(self) -> dict:
        source_hash = self.story.source_sha256
        return {
            "source_hash": source_hash,
            "text_length": 13,
            "chapters": [
                {
                    "id": f"ch-{source_hash}-0001",
                    "index": 0,
                    "start_offset": 0,
                    "end_offset": 7,
                    "title": "第一章",
                    "summary": "开场介绍人物。",
                    "scenes": [
                        {"id": f"ch-{source_hash}-0001-sc-0001", "index": 0, "start_offset": 0, "end_offset": 4, "summary": "章节标题出现。", "pace": 1.0},
                        {"id": f"ch-{source_hash}-0001-sc-0002", "index": 1, "start_offset": 4, "end_offset": 7, "summary": "人物完成行动。", "pace": 1.2},
                    ],
                },
                {
                    "id": f"ch-{source_hash}-0002",
                    "index": 1,
                    "start_offset": 7,
                    "end_offset": 13,
                    "title": "第二章",
                    "summary": "后续事件推进。",
                    "scenes": [
                        {"id": f"ch-{source_hash}-0002-sc-0001", "index": 0, "start_offset": 7, "end_offset": 13, "summary": "新章节完成转折。", "pace": 0.8},
                    ],
                },
            ],
        }

    def _write_candidate(self, candidate: dict) -> None:
        self.candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
