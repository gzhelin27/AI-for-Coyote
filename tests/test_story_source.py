import hashlib
import io
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest

from docx import Document

from backend.config import DEFAULTS, load_config
from backend.story.models import ImportedStory
from backend.story.source import StorySourceError, StorySourceLoader


class StorySourceTests(unittest.TestCase):
    def test_txt_normalizes_newlines_and_hashes_original_bytes(self):
        original = "甲\r\n乙".encode("utf-8")

        imported = StorySourceLoader(max_bytes=1024).load("novel.txt", original)

        self.assertEqual(imported.text, "甲\n乙")
        self.assertEqual(imported.filename, "novel.txt")
        self.assertEqual(imported.extension, ".txt")
        self.assertEqual(
            imported.source_sha256,
            hashlib.sha256("甲\r\n乙".encode("utf-8")).hexdigest(),
        )
        self.assertEqual(imported.original_bytes, original)
        with self.assertRaises(FrozenInstanceError):
            imported.text = "changed"

    def test_markdown_uses_gb18030_after_utf8_fails_and_strips_text_nuls(self):
        original = "第一章\x00\r第二章".encode("gb18030")

        imported = StorySourceLoader(max_bytes=1024).load("chapter.md", original)

        self.assertEqual(imported.text, "第一章\n第二章")
        self.assertEqual(
            imported.source_sha256,
            hashlib.sha256(original).hexdigest(),
        )

    def test_docx_preserves_heading_order_and_paragraph_text(self):
        document = Document()
        document.add_heading("第一章", level=1)
        document.add_paragraph("雨落在窗前。")
        document.add_heading("第二章", level=1)
        document.add_paragraph("门在黎明前打开。")
        payload = io.BytesIO()
        document.save(payload)
        original = payload.getvalue()

        imported = StorySourceLoader(max_bytes=1024 * 1024).load("novel.docx", original)

        self.assertEqual(imported.text, "第一章\n雨落在窗前。\n第二章\n门在黎明前打开。")
        self.assertEqual(imported.extension, ".docx")
        self.assertEqual(imported.source_sha256, hashlib.sha256(original).hexdigest())

    def test_rejects_unsupported_extension_and_oversize_input(self):
        loader = StorySourceLoader(max_bytes=3)

        with self.assertRaises(StorySourceError):
            loader.load("novel.pdf", b"abc")
        with self.assertRaises(StorySourceError):
            loader.load("novel.txt", b"abcd")

    def test_sanitizes_filename_without_using_parent_path(self):
        imported = StorySourceLoader(max_bytes=1024).load(
            str(Path("parent") / ".." / "novel.txt"), b"safe text"
        )

        self.assertEqual(imported.filename, "novel.txt")

    def test_rejects_invalid_filename_bytes_encoding_and_empty_text(self):
        loader = StorySourceLoader(max_bytes=1024)
        invalid_cases = (
            ("", b"text"),
            ("story\x00.txt", b"text"),
            ("story.txt", "not bytes"),
            ("story.txt", b"\xff"),
            ("story.txt", b"\x00\r\n\x00"),
        )

        for filename, payload in invalid_cases:
            with self.subTest(filename=filename, payload=payload), self.assertRaises(StorySourceError):
                loader.load(filename, payload)


class StoryConfigurationTests(unittest.TestCase):
    def test_story_defaults_merge_with_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("app:\n  port: 8765\nstory:\n  max_source_mb: 2\n", encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config["app"]["port"], 8765)
        self.assertEqual(
            config["story"],
            {
                "import_dir": "data/stories",
                "analysis_dir": "data/story_analysis",
                "max_source_mb": 2,
                "analysis_prompt_version": "faithful-v1",
                "reading_speed_cpm": {"slow": 250, "standard": 400, "fast": 600},
            },
        )
        self.assertEqual(DEFAULTS["story"]["max_source_mb"], 20)


if __name__ == "__main__":
    unittest.main()
