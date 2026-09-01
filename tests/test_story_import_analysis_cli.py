from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.provenance import dlc_provenance
from backend.story.import_analysis import main


class OfflineAnalysisCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name)
        self.source_path = self.project_root / "novel.txt"
        self.candidate_directory = self.project_root / "data" / "story_candidates"
        self.candidate_directory.mkdir(parents=True)
        self.candidate_path = self.candidate_directory / "candidate.json"
        self.source_text = "Alpha\nBeta"
        self.source_path.write_text(self.source_text, encoding="utf-8")
        source_hash = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        self.candidate_path.write_text(
            json.dumps(
                {
                    "source_hash": source_hash,
                    "text_length": 10,
                    "chapters": [
                        {
                            "id": f"ch-{source_hash}-0001",
                            "index": 0,
                            "start_offset": 0,
                            "end_offset": 10,
                            "title": "Opening",
                            "summary": "The opening is introduced.",
                            "scenes": [
                                {
                                    "id": f"ch-{source_hash}-0001-sc-0001",
                                    "index": 0,
                                    "start_offset": 0,
                                    "end_offset": 10,
                                    "summary": "The event begins.",
                                    "pace": 1.0,
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.cfg = {
            "story": {
                "max_source_mb": 1,
                "analysis_dir": "data/story_analysis",
                "candidate_dir": "data/story_candidates",
            },
            "character": {"role": "role", "profile": "profile", "prompt": "local only"},
            "timeline": {"waveform_policy": "all_allowed"},
        }

    def test_validate_uses_configured_provenance_for_every_supported_encoding(self):
        expected_dlc = dlc_provenance(self.cfg, project_root=self.project_root)
        for encoding in ("auto", "utf-8", "gb18030"):
            with self.subTest(encoding=encoding):
                status, stdout, stderr = self._run(
                    "validate", "--source", str(self.source_path), "--map", str(self.candidate_path), "--encoding", encoding
                )
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertIn("validated", stdout)
                self.assertIn(expected_dlc, stdout)
                self.assertIn("faithful-offline-v1", stdout)
                self.assertNotIn(self.source_text, stdout)

    def test_import_saves_to_the_current_local_analysis_directory(self):
        status, stdout, stderr = self._run(
            "import", "--source", str(self.source_path), "--map", str(self.candidate_path), "--encoding", "auto"
        )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("imported", stdout)
        self.assertEqual(len(list((self.project_root / "data" / "story_analysis").glob("*.json"))), 1)

    def test_errors_are_nonzero_redacted_and_do_not_accept_free_dlc_identity(self):
        self.source_path.write_text("PRIVATE-NOVEL-EXCERPT", encoding="utf-8")
        cases = (
            ("missing arguments", ("validate",), "usage:"),
            ("unknown free identity", ("validate", "--source", str(self.source_path), "--map", str(self.candidate_path), "--dlc-version", "forged"), "unrecognized arguments"),
            ("ambiguous source", ("validate", "--source", str(self.source_path), "--map", str(self.candidate_path), "--encoding", "auto"), "offline analysis failed"),
        )
        self.source_path.write_bytes(bytes.fromhex("d2bb"))
        for name, argv, expected in cases:
            with self.subTest(name=name):
                status, stdout, stderr = self._run(*argv)
                self.assertNotEqual(status, 0)
                self.assertIn(expected, stderr)
                self.assertNotIn("PRIVATE-NOVEL-EXCERPT", stdout + stderr)

    def test_cli_rejects_candidate_outside_local_candidate_directory(self):
        outside = self.project_root / "outside.json"
        outside.write_text(
            self.candidate_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

        status, stdout, stderr = self._run(
            "validate", "--source", str(self.source_path), "--map", str(outside)
        )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "offline analysis failed\n")

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("backend.story.import_analysis.load_config", return_value=self.cfg), patch(
            "backend.story.import_analysis.PROJECT_ROOT", self.project_root
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(argv))
        return status, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
