import hashlib
import io
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
import zipfile

from docx import Document
import yaml

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

    def test_prefers_gb18030_for_ambiguous_chinese_bytes(self):
        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", bytes.fromhex("d2bb"))

        self.assertEqual(imported.text, "一")

    def test_rejects_repeated_ambiguous_gb18030_bytes(self):
        loader = StorySourceLoader(max_bytes=1024)

        for repeats in (2, 3, 8):
            with self.subTest(repeats=repeats), self.assertRaises(StorySourceError):
                loader.load("chapter.txt", bytes.fromhex("d2bb") * repeats)

    def test_preserves_real_utf8_chinese_when_gb18030_also_decodes(self):
        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", "中文".encode("utf-8"))

        self.assertEqual(imported.text, "中文")

    def test_preserves_utf8_emoji_when_gb18030_also_decodes(self):
        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", "😊".encode("utf-8"))

        self.assertEqual(imported.text, "😊")

    def test_preserves_multi_character_utf8_cyrillic_when_gb18030_also_decodes(self):
        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", "Привет".encode("utf-8"))

        self.assertEqual(imported.text, "Привет")

    def test_preserves_utf8_latin_accents_when_gb18030_also_decodes(self):
        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", "café".encode("utf-8"))

        self.assertEqual(imported.text, "café")

    def test_rejects_equally_plausible_conflicting_text_decodings(self):
        ambiguous = bytes.fromhex("d2bbceb1")

        with self.assertRaises(StorySourceError):
            StorySourceLoader(max_bytes=1024).load("chapter.txt", ambiguous)

    def test_utf8_bom_is_definitive_and_not_in_normalized_text(self):
        original = b"\xef\xbb\xbf" + "开篇".encode("utf-8")

        imported = StorySourceLoader(max_bytes=1024).load("chapter.txt", original)

        self.assertEqual(imported.text, "开篇")

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

    def test_accepts_exact_byte_limit_and_normalizes_uppercase_extension(self):
        imported = StorySourceLoader(max_bytes=4).load("NOVEL.TXT", b"text")

        self.assertEqual(imported.filename, "NOVEL.TXT")
        self.assertEqual(imported.extension, ".txt")

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

    def test_rejects_docx_with_excessive_compressed_expansion_before_extraction(self):
        original = self._generated_docx_bytes()
        payload = io.BytesIO(original)
        with zipfile.ZipFile(payload, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("padding.txt", b"A" * 1_000_000)

        with self.assertRaises(StorySourceError):
            StorySourceLoader(max_bytes=2 * 1024 * 1024).load("novel.docx", payload.getvalue())

    def test_rejects_docx_with_excessive_entry_count_before_extraction(self):
        payload = io.BytesIO(self._generated_docx_bytes())
        with zipfile.ZipFile(payload, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            for number in range(300):
                archive.writestr(f"padding/{number}.txt", b"x")

        with self.assertRaises(StorySourceError):
            StorySourceLoader(max_bytes=2 * 1024 * 1024).load("novel.docx", payload.getvalue())

    def test_rejects_encrypted_docx_before_parser(self):
        encrypted = self._mark_central_directory_encrypted(self._generated_docx_bytes())

        with self.assertRaisesRegex(StorySourceError, "encrypted"):
            StorySourceLoader(max_bytes=1024 * 1024).load("novel.docx", encrypted)

    def test_rejects_docx_with_unsafe_member_name(self):
        payload = io.BytesIO(self._generated_docx_bytes())
        with zipfile.ZipFile(payload, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../outside.txt", b"not allowed")

        with self.assertRaises(StorySourceError):
            StorySourceLoader(max_bytes=1024 * 1024).load("novel.docx", payload.getvalue())

    def test_rejects_malformed_docx(self):
        with self.assertRaises(StorySourceError):
            StorySourceLoader(max_bytes=1024).load("novel.docx", b"not a zip file")

    @staticmethod
    def _generated_docx_bytes() -> bytes:
        document = Document()
        document.add_paragraph("正常文档")
        payload = io.BytesIO()
        document.save(payload)
        return payload.getvalue()

    @staticmethod
    def _mark_central_directory_encrypted(payload: bytes) -> bytes:
        marked = bytearray(payload)
        position = 0
        while True:
            position = marked.find(b"PK\x01\x02", position)
            if position < 0:
                return bytes(marked)
            flags = int.from_bytes(marked[position + 8:position + 10], "little")
            marked[position + 8:position + 10] = (flags | 0x1).to_bytes(2, "little")
            position += 4


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

    def test_story_configuration_normalizes_valid_local_overrides(self):
        config = self._load_config(
            {
                "story": {
                    "import_dir": "data\\stories\\imports",
                    "analysis_dir": "data/analysis/cache",
                    "max_source_mb": 2.5,
                    "analysis_prompt_version": " faithful-v2 ",
                    "reading_speed_cpm": {"slow": 250.0, "standard": 401.5, "fast": 600.0},
                }
            }
        )

        self.assertEqual(config["story"]["import_dir"], "data/stories/imports")
        self.assertEqual(config["story"]["analysis_dir"], "data/analysis/cache")
        self.assertEqual(config["story"]["max_source_mb"], 2.5)
        self.assertEqual(config["story"]["analysis_prompt_version"], "faithful-v2")
        self.assertEqual(
            config["story"]["reading_speed_cpm"],
            {"slow": 250, "standard": 401.5, "fast": 600},
        )

    def test_story_configuration_rejects_invalid_values(self):
        invalid_stories = (
            {"max_source_mb": 0},
            {"max_source_mb": True},
            {"max_source_mb": -1},
            {"max_source_mb": float("nan")},
            {"reading_speed_cpm": [250, 400, 600]},
            {"reading_speed_cpm": {"slow": 250, "standard": 400}},
            {"reading_speed_cpm": {"slow": 250, "standard": 400, "fast": 600, "turbo": 800}},
            {"reading_speed_cpm": {"slow": 0, "standard": 400, "fast": 600}},
            {"reading_speed_cpm": {"slow": True, "standard": 400, "fast": 600}},
            {"reading_speed_cpm": {"slow": 250, "standard": float("inf"), "fast": 600}},
            {"import_dir": "../stories"},
            {"analysis_dir": "C:\\story-analysis"},
            {"import_dir": "stories/imports"},
            {"analysis_dir": "data"},
            {"import_dir": ""},
            {"analysis_prompt_version": "   "},
            {"unknown": "value"},
            {"reading_speed_cpm": {"slow": 400, "standard": 250, "fast": 600}},
            {"reading_speed_cpm": {"slow": 250, "standard": 600, "fast": 600}},
        )

        for story in invalid_stories:
            with self.subTest(story=story), self.assertRaises(ValueError):
                self._load_config({"story": story})

    @staticmethod
    def _load_config(overrides: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(yaml.safe_dump(overrides), encoding="utf-8")
            return load_config(config_path)


if __name__ == "__main__":
    unittest.main()
