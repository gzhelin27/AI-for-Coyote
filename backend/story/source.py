"""Safe in-memory import for supported local novel sources."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from docx import Document

from .models import ImportedStory


_ALLOWED_EXTENSIONS = frozenset((".txt", ".md", ".docx"))


class StorySourceError(ValueError):
    """Raised when a local story source cannot be safely imported."""


class StorySourceLoader:
    """Validate, extract, and normalize a supported story source in memory."""

    def __init__(self, max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes

    def load(self, filename: str, original_bytes: bytes) -> ImportedStory:
        safe_name, extension = self._source_name(filename)
        self._validate_bytes(original_bytes)
        text = self._extract_text(extension, original_bytes)
        text = _normalize_text(text)
        if not text.strip():
            raise StorySourceError("story source contains no text")
        return ImportedStory(
            filename=safe_name,
            extension=extension,
            original_bytes=original_bytes,
            text=text,
            source_sha256=hashlib.sha256(original_bytes).hexdigest(),
        )

    def _source_name(self, filename: str) -> tuple[str, str]:
        if not isinstance(filename, str) or "\x00" in filename:
            raise StorySourceError("story filename is invalid")
        safe_name = Path(filename).name
        if not safe_name or safe_name in (".", ".."):
            raise StorySourceError("story filename is invalid")
        extension = Path(safe_name).suffix.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise StorySourceError("story source extension is not supported")
        return safe_name, extension

    def _validate_bytes(self, original_bytes: bytes) -> None:
        if not isinstance(original_bytes, bytes):
            raise StorySourceError("story source must be bytes")
        if len(original_bytes) > self._max_bytes:
            raise StorySourceError("story source exceeds the size limit")

    @staticmethod
    def _extract_text(extension: str, original_bytes: bytes) -> str:
        if extension == ".docx":
            try:
                document = Document(io.BytesIO(original_bytes))
            except Exception as exc:  # python-docx exposes several parser exceptions.
                raise StorySourceError("story DOCX could not be read") from exc
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        try:
            return original_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                return original_bytes.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise StorySourceError("story text encoding is not supported") from exc


def _normalize_text(text: str) -> str:
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
