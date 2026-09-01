"""Safe in-memory import for supported local novel sources."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Literal
import zipfile

from docx import Document

from .models import ImportedStory


_ALLOWED_EXTENSIONS = frozenset((".txt", ".md", ".docx"))
_DOCX_REQUIRED_MEMBERS = frozenset(("[Content_Types].xml", "_rels/.rels", "word/document.xml"))
_DOCX_MAX_ENTRIES = 256
_DOCX_MAX_EXPANSION_RATIO = 100
_DOCX_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_DOCX_UNCOMPRESSED_MULTIPLIER = 20

StorySourceEncoding = Literal["auto", "utf-8", "gb18030"]
_STORY_SOURCE_ENCODINGS = ("auto", "utf-8", "gb18030")


class StorySourceError(ValueError):
    """Raised when a local story source cannot be safely imported."""


class StorySourceLoader:
    """Validate, extract, and normalize a supported story source in memory."""

    def __init__(self, max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes

    def load(
        self,
        filename: str,
        original_bytes: bytes,
        *,
        encoding: StorySourceEncoding = "auto",
    ) -> ImportedStory:
        """Import a source using ``auto``, ``utf-8``, or ``gb18030`` text decoding.

        ``auto`` accepts a UTF-8 BOM, a single successful strict decoder, or
        identical UTF-8/GB18030 text. Conflicting valid decodings are rejected
        so the caller can retry with an explicit encoding. DOCX sources are
        parsed as OOXML and therefore reject any encoding other than ``auto``.
        """
        safe_name, extension = self._source_name(filename)
        self._validate_bytes(original_bytes)
        self._validate_encoding(extension, encoding)
        text = self._extract_text(extension, original_bytes, encoding)
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
    def _validate_encoding(extension: str, encoding: object) -> None:
        if encoding not in _STORY_SOURCE_ENCODINGS:
            raise StorySourceError("story text encoding must be one of: auto, utf-8, gb18030")
        if extension == ".docx" and encoding != "auto":
            raise StorySourceError("story DOCX does not accept a text encoding override")

    def _extract_text(
        self,
        extension: str,
        original_bytes: bytes,
        encoding: StorySourceEncoding,
    ) -> str:
        if extension == ".docx":
            self._inspect_docx(original_bytes)
            try:
                document = Document(io.BytesIO(original_bytes))
            except Exception as exc:  # python-docx exposes several parser exceptions.
                raise StorySourceError("story DOCX could not be read") from exc
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        return _decode_plain_text(original_bytes, encoding)

    def _inspect_docx(self, original_bytes: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(original_bytes)) as archive:
                members = archive.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise StorySourceError("story DOCX archive is malformed") from exc
        if len(members) > _DOCX_MAX_ENTRIES:
            raise StorySourceError("story DOCX has too many entries")
        names = {member.filename for member in members}
        if not _DOCX_REQUIRED_MEMBERS.issubset(names):
            raise StorySourceError("story DOCX is missing required OOXML members")

        uncompressed_limit = min(
            self._max_bytes * _DOCX_UNCOMPRESSED_MULTIPLIER,
            _DOCX_MAX_UNCOMPRESSED_BYTES,
        )
        total_uncompressed = 0
        total_compressed = 0
        for member in members:
            if member.flag_bits & 0x1:
                raise StorySourceError("story DOCX archive is encrypted")
            if _unsafe_docx_member_name(member.filename):
                raise StorySourceError("story DOCX contains an unsafe member name")
            if member.file_size > uncompressed_limit:
                raise StorySourceError("story DOCX entry exceeds the expansion limit")
            if member.file_size and (
                not member.compress_size
                or member.file_size > member.compress_size * _DOCX_MAX_EXPANSION_RATIO
            ):
                raise StorySourceError("story DOCX entry exceeds the compression ratio limit")
            total_uncompressed += member.file_size
            total_compressed += member.compress_size
            if total_uncompressed > uncompressed_limit:
                raise StorySourceError("story DOCX exceeds the expansion limit")
        if total_uncompressed and (
            not total_compressed
            or total_uncompressed > total_compressed * _DOCX_MAX_EXPANSION_RATIO
        ):
            raise StorySourceError("story DOCX exceeds the compression ratio limit")


def _normalize_text(text: str) -> str:
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _decode_plain_text(original_bytes: bytes, encoding: StorySourceEncoding) -> str:
    if encoding != "auto":
        codec = "utf-8-sig" if encoding == "utf-8" else encoding
        try:
            return original_bytes.decode(codec)
        except UnicodeDecodeError as exc:
            raise StorySourceError(f"story text is not valid {encoding}") from exc

    if original_bytes.startswith(b"\xef\xbb\xbf"):
        try:
            return original_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StorySourceError("story UTF-8 BOM text is not valid utf-8") from exc

    candidates: dict[str, str] = {}
    for candidate_encoding in ("utf-8", "gb18030"):
        try:
            candidates[candidate_encoding] = original_bytes.decode(candidate_encoding)
        except UnicodeDecodeError:
            pass
    if not candidates:
        raise StorySourceError("story text encoding is not supported")
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    utf8 = candidates["utf-8"]
    gb18030 = candidates["gb18030"]
    if utf8 == gb18030:
        return utf8
    raise StorySourceError(
        "story text encoding is ambiguous; choose utf-8 or gb18030 explicitly"
    )


def _unsafe_docx_member_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    return ".." in PurePosixPath(name).parts
