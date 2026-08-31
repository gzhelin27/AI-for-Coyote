"""Atomic persistence for completed deterministic timeline replays."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, BinaryIO
import zipfile
import zlib

from .models import SCHEMA_VERSION, ReplayManifest, SessionStatus, Timeline


_REPLAY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ARCHIVE_SUFFIX = ".coyote-replay"
_FIXED_MEMBERS = frozenset(("manifest.json", "timeline.json", "scenes.json"))
_SOURCE_EXTENSIONS = frozenset(("txt", "md", "docx"))
_MAX_MEMBER_SIZES = {
    "manifest.json": 256 * 1024,
    "timeline.json": 16 * 1024 * 1024,
    "scenes.json": 16 * 1024 * 1024,
}
_MAX_SOURCE_SIZE = 32 * 1024 * 1024
_MAX_TOTAL_SIZE = 48 * 1024 * 1024
_MAX_ENTRY_COUNT = 4
_MAX_COMPRESSED_MEMBER_SIZES = {
    "manifest.json": 512 * 1024,
    "timeline.json": 17 * 1024 * 1024,
    "scenes.json": 17 * 1024 * 1024,
}
_MAX_COMPRESSED_SOURCE_SIZE = 33 * 1024 * 1024
_MAX_ARCHIVE_SIZE = 50 * 1024 * 1024
_ALLOWED_COMPRESSION_METHODS = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _opened_final_path(archive_file: BinaryIO) -> Path:
    """Resolve the final target from the opened handle, not its pathname."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_final_path = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(archive_file.fileno())
        capacity = 260
        while True:
            buffer = ctypes.create_unicode_buffer(capacity)
            length = get_final_path(handle, buffer, capacity, 0)
            if length == 0:
                error_code = ctypes.get_last_error()
                raise OSError(error_code, "could not resolve opened replay archive")
            if length < capacity:
                final_path = buffer.value
                if final_path.startswith("\\\\?\\UNC\\"):
                    final_path = "\\\\" + final_path[8:]
                elif final_path.startswith("\\\\?\\"):
                    final_path = final_path[4:]
                return Path(final_path)
            capacity = length + 1

    descriptor_link = Path("/proc/self/fd", str(archive_file.fileno()))
    try:
        return Path(os.path.realpath(descriptor_link, strict=True))
    except (OSError, TypeError) as exc:
        raise OSError("opened-handle path validation is unavailable") from exc


class ReplayStoreError(Exception):
    """A replay could not be safely persisted or loaded."""


@dataclass(frozen=True)
class ReplayBundle:
    manifest: ReplayManifest
    timeline: Timeline
    scenes: Any | None = None
    source: bytes | None = None
    source_extension: str | None = None


@dataclass(frozen=True)
class ReplaySummary:
    replay_id: str
    session_id: str
    seed: int
    status: SessionStatus
    mode: str
    created_at: str
    completed_at: str | None
    adjusted: bool
    app_commit: str
    model: str
    dlc_role: str
    dlc_profile: str
    dlc_version: str

    @classmethod
    def from_manifest(cls, manifest: ReplayManifest) -> ReplaySummary:
        return cls(
            replay_id=manifest.replay_id,
            session_id=manifest.session_id,
            seed=manifest.seed,
            status=manifest.status,
            mode=manifest.mode,
            created_at=manifest.created_at,
            completed_at=manifest.completed_at,
            adjusted=manifest.adjusted,
            app_commit=manifest.app_commit,
            model=manifest.model,
            dlc_role=manifest.dlc_role,
            dlc_profile=manifest.dlc_profile,
            dlc_version=manifest.dlc_version,
        )


class ReplayStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(
        self,
        manifest: ReplayManifest,
        timeline: Timeline,
        *,
        scenes: dict[str, Any] | None = None,
        source: bytes | None = None,
        source_extension: str | None = None,
    ) -> Path:
        if manifest.status is not SessionStatus.COMPLETED:
            raise ReplayStoreError("only completed sessions can be saved")
        self._validate_replay_id(manifest.replay_id)
        if timeline.session_id != manifest.session_id or timeline.seed != manifest.seed:
            raise ReplayStoreError("timeline does not match replay manifest")
        _validate_timeline(timeline)

        if (source is None) != (source_extension is None):
            raise ReplayStoreError("source bytes and extension must be provided together")
        if source is not None:
            if not isinstance(source, bytes):
                raise ReplayStoreError("replay source must be bytes")
            if source_extension not in _SOURCE_EXTENSIONS:
                raise ReplayStoreError("replay source extension is not allowed")
        if scenes is not None:
            _validate_scenes(scenes)

        timeline_bytes = _json_bytes(timeline.to_dict())
        payloads = {"timeline.json": timeline_bytes}
        if scenes is not None:
            payloads["scenes.json"] = _json_bytes(scenes)
        if source is not None and source_extension is not None:
            payloads[f"source.{source_extension}"] = source
        checksums = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        }
        stored_manifest = replace(
            manifest,
            source_hash=(
                hashlib.sha256(source).hexdigest()
                if source is not None
                else None
            ),
            checksums=checksums,
        )
        manifest_bytes = _json_bytes(stored_manifest.to_dict())
        archive_payloads = {"manifest.json": manifest_bytes, **payloads}
        _validate_payload_sizes(archive_payloads)

        self.root.mkdir(parents=True, exist_ok=True)
        final_path = self._path(manifest.replay_id)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix=f".{manifest.replay_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with zipfile.ZipFile(
                    temporary, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for name, payload in archive_payloads.items():
                        archive.writestr(name, payload)
            temporary_path.replace(final_path)
            return final_path
        except (OSError, zipfile.BadZipFile) as exc:
            raise ReplayStoreError("could not save replay archive") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load(self, replay_id: str) -> ReplayBundle:
        _, bundle = self._read_validated_snapshot(replay_id)
        return bundle

    def open_validated(self, replay_id: str) -> BinaryIO:
        """Return an in-memory handle over the exact validated snapshot."""
        return io.BytesIO(self.read_validated(replay_id))

    def read_validated(self, replay_id: str) -> bytes:
        """Return the same bounded immutable snapshot that passed validation."""
        payload, _ = self._read_validated_snapshot(replay_id)
        return payload

    def _read_validated_snapshot(
        self, replay_id: str
    ) -> tuple[bytes, ReplayBundle]:
        payload = self._read_contained_bytes(replay_id)
        with io.BytesIO(payload) as archive_file:
            bundle = self._load_opened(replay_id, archive_file)
        return payload, bundle

    def _read_contained_bytes(self, replay_id: str) -> bytes:
        archive_file = self._open_contained(replay_id)
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                remaining = _MAX_ARCHIVE_SIZE + 1 - total
                chunk = archive_file.read(min(64 * 1024, remaining))
                if chunk == b"":
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ReplayStoreError("could not load replay archive")
                total += len(chunk)
                if total > _MAX_ARCHIVE_SIZE:
                    raise ReplayStoreError("replay archive size exceeds limit")
                chunks.append(bytes(chunk))
        except ReplayStoreError:
            raise
        except OSError as exc:
            raise ReplayStoreError("could not load replay archive") from exc
        finally:
            archive_file.close()
        return b"".join(chunks)

    def _open_contained(self, replay_id: str) -> BinaryIO:
        self._validate_replay_id(replay_id)
        archive_path = self._path(replay_id)
        archive_file: BinaryIO | None = None
        try:
            resolved = archive_path.resolve(strict=True)
            root = self.root.resolve()
            if resolved.parent != root:
                raise ReplayStoreError("replay archive path is unsafe")
            before = resolved.stat()
            archive_file = resolved.open("rb")
            opened = os.fstat(archive_file.fileno())
            opened_path = _opened_final_path(archive_file)
            if _path_key(opened_path.parent) != _path_key(root):
                raise ReplayStoreError("replay archive path is unsafe")
        except ReplayStoreError:
            if archive_file is not None:
                archive_file.close()
            raise
        except OSError as exc:
            if archive_file is not None:
                archive_file.close()
            raise ReplayStoreError("could not load replay archive") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            archive_file.close()
            raise ReplayStoreError("replay archive changed while opening")
        if opened.st_size > _MAX_ARCHIVE_SIZE:
            archive_file.close()
            raise ReplayStoreError("replay archive size exceeds limit")
        return archive_file

    def _load_opened(
        self, replay_id: str, archive_file: BinaryIO
    ) -> ReplayBundle:
        try:
            archive_file.seek(0, os.SEEK_END)
            if archive_file.tell() > _MAX_ARCHIVE_SIZE:
                raise ReplayStoreError("replay archive size exceeds limit")
            archive_file.seek(0)
            with zipfile.ZipFile(archive_file, "r") as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_ENTRY_COUNT:
                    raise ReplayStoreError("replay archive has too many entries")
                for info in infos:
                    _validate_member_path(info.filename)
                    if not _is_allowed_member(info.filename):
                        raise ReplayStoreError("replay archive contains an unexpected member")
                    if info.flag_bits & 1:
                        raise ReplayStoreError("replay archive contains an encrypted member")
                    if info.compress_type not in _ALLOWED_COMPRESSION_METHODS:
                        raise ReplayStoreError(
                            "replay archive uses an unsupported compression method"
                        )
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise ReplayStoreError("replay archive contains a duplicate member")
                source_names = [name for name in names if name.startswith("source.")]
                if len(source_names) > 1:
                    raise ReplayStoreError("replay archive contains multiple source members")
                if "manifest.json" not in names or "timeline.json" not in names:
                    raise ReplayStoreError("replay archive is missing required members")
                _validate_member_sizes(infos)
                payloads = {info.filename: archive.read(info) for info in infos}
                manifest_data = json.loads(payloads["manifest.json"])
        except ReplayStoreError:
            raise
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            OverflowError,
            RuntimeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
        ) as exc:
            raise ReplayStoreError("could not load replay archive") from exc

        try:
            manifest = ReplayManifest.from_dict(manifest_data)
        except (TypeError, ValueError) as exc:
            raise ReplayStoreError("replay archive has invalid schema") from exc
        _validate_checksums(manifest.checksums, payloads)
        try:
            timeline = Timeline.from_dict(json.loads(payloads["timeline.json"]))
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayStoreError("replay archive has invalid schema") from exc
        if manifest.replay_id != replay_id:
            raise ReplayStoreError("replay manifest ID does not match archive")
        if manifest.status is not SessionStatus.COMPLETED:
            raise ReplayStoreError("replay archive is not a completed session")
        if timeline.session_id != manifest.session_id or timeline.seed != manifest.seed:
            raise ReplayStoreError("timeline does not match replay manifest")
        _validate_timeline(timeline)

        scenes = None
        if "scenes.json" in payloads:
            try:
                scenes = json.loads(payloads["scenes.json"])
                _validate_scenes(scenes)
            except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReplayStoreError("replay scenes have invalid schema") from exc

        source_name = source_names[0] if source_names else None
        source = payloads[source_name] if source_name is not None else None
        if source is None:
            if manifest.source_hash is not None:
                raise ReplayStoreError("replay source hash has no source member")
        else:
            source_hash = hashlib.sha256(source).hexdigest()
            if manifest.source_hash is None or not hmac.compare_digest(
                manifest.source_hash, source_hash
            ):
                raise ReplayStoreError("replay source hash mismatch")
        return ReplayBundle(
            manifest=manifest,
            timeline=timeline,
            scenes=scenes,
            source=source,
            source_extension=(source_name.partition(".")[2] if source_name else None),
        )

    def list(self) -> list[ReplaySummary]:
        if not self.root.exists():
            return []
        replay_ids = sorted(
            path.name.removesuffix(_ARCHIVE_SUFFIX)
            for path in self.root.glob(f"*{_ARCHIVE_SUFFIX}")
            if path.is_file()
        )
        return [ReplaySummary.from_manifest(self.load(replay_id).manifest) for replay_id in replay_ids]

    def delete(self, replay_id: str) -> None:
        self._validate_replay_id(replay_id)
        try:
            self._path(replay_id).unlink()
        except OSError as exc:
            raise ReplayStoreError("could not delete replay archive") from exc

    def _path(self, replay_id: str) -> Path:
        return self.root / f"{replay_id}{_ARCHIVE_SUFFIX}"

    @staticmethod
    def _validate_replay_id(replay_id: str) -> None:
        if not isinstance(replay_id, str) or _REPLAY_ID.fullmatch(replay_id) is None:
            raise ReplayStoreError("invalid replay ID")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_member_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != name
    ):
        raise ReplayStoreError("replay archive contains an unsafe member path")


def _is_allowed_member(name: str) -> bool:
    if name in _FIXED_MEMBERS:
        return True
    prefix, separator, extension = name.partition(".")
    return prefix == "source" and separator == "." and extension in _SOURCE_EXTENSIONS


def _validate_checksums(
    checksums: object,
    payloads: dict[str, bytes],
) -> None:
    expected_names = set(payloads) - {"manifest.json"}
    if not isinstance(checksums, dict) or set(checksums) != expected_names:
        raise ReplayStoreError("replay archive checksum inventory does not match members")
    for name in expected_names:
        expected = checksums[name]
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ReplayStoreError("replay archive contains an invalid checksum")
        actual = hashlib.sha256(payloads[name]).hexdigest()
        if not hmac.compare_digest(expected, actual):
            raise ReplayStoreError("replay archive checksum mismatch")


def _member_size_limit(name: str) -> int:
    if name.startswith("source."):
        return _MAX_SOURCE_SIZE
    return _MAX_MEMBER_SIZES[name]


def _compressed_member_size_limit(name: str) -> int:
    if name.startswith("source."):
        return _MAX_COMPRESSED_SOURCE_SIZE
    return _MAX_COMPRESSED_MEMBER_SIZES[name]


def _validate_member_sizes(infos: list[zipfile.ZipInfo]) -> None:
    total = 0
    for info in infos:
        if (
            info.compress_size < 0
            or info.compress_size > _compressed_member_size_limit(info.filename)
        ):
            raise ReplayStoreError("replay archive member exceeds compressed size limit")
        if info.file_size < 0 or info.file_size > _member_size_limit(info.filename):
            raise ReplayStoreError("replay archive member exceeds size limit")
        total += info.file_size
    if total > _MAX_TOTAL_SIZE:
        raise ReplayStoreError("replay archive exceeds total size limit")


def _validate_payload_sizes(payloads: dict[str, bytes]) -> None:
    if any(len(payload) > _member_size_limit(name) for name, payload in payloads.items()):
        raise ReplayStoreError("replay archive member exceeds size limit")
    if sum(map(len, payloads.values())) > _MAX_TOTAL_SIZE:
        raise ReplayStoreError("replay archive exceeds total size limit")


def _validate_scenes(scenes: object) -> None:
    if not isinstance(scenes, dict) or scenes.get("schema_version") != SCHEMA_VERSION:
        raise ReplayStoreError("replay scenes have invalid schema")


def _validate_timeline(timeline: Timeline) -> None:
    cycle_keys: set[tuple[str, int]] = set()
    for cycle in timeline.cycles:
        key = (cycle.channel, cycle.cycle_index)
        if key in cycle_keys:
            raise ReplayStoreError("timeline contains a duplicate cycle record")
        cycle_keys.add(key)
