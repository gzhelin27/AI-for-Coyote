"""Atomic, schema-versioned local storage for faithful story analysis."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any, BinaryIO, Iterator, Literal

from backend.timeline.replay_store import _opened_final_path

from .models import AnalysisKey, StoryChapter, StoryMap, StoryScene


_SCHEMA_VERSION = 2
_DOCUMENT_KEYS = frozenset(("schema_version", "analysis_key", "story_map"))
_KEY_KEYS = frozenset(("source_hash", "model", "prompt_version", "dlc_version"))
_MAP_KEYS = frozenset(("source_hash", "text_length", "chapters"))
_CHAPTER_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "title", "summary", "scenes"))
_SCENE_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "summary", "pace"))
_MAX_CACHE_BYTES = 1024 * 1024


class AnalysisStoreError(OSError):
    """A local analysis cache entry could not be written safely."""


@dataclass(frozen=True, slots=True)
class AnalysisLookup:
    """One cache inspection result, including first-read corruption state."""

    status: Literal["ready", "missing", "invalid"]
    story_map: StoryMap | None


class _CacheValidationError(ValueError):
    """A local cache document cannot be trusted as a StoryMap."""


class _LockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.references = 0


_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[tuple[int, int, str], _LockEntry] = {}


@dataclass(frozen=True, slots=True)
class _DirectoryHandle:
    descriptor: int

    def fileno(self) -> int:
        return self.descriptor

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True, slots=True)
class _PinnedCacheRoot:
    handle: _DirectoryHandle
    identity: tuple[int, int]
    final_path: Path


@contextmanager
def _cache_key_lock(
    root_identity: tuple[int, int], child_name: str
) -> Iterator[None]:
    """Serialize one child of one pinned root across store instances."""

    key = (*root_identity, child_name)
    with _LOCK_REGISTRY_GUARD:
        entry = _LOCK_REGISTRY.get(key)
        if entry is None:
            entry = _LockEntry()
            _LOCK_REGISTRY[key] = entry
        entry.references += 1
    try:
        with entry.lock:
            yield
    finally:
        with _LOCK_REGISTRY_GUARD:
            entry.references -= 1
            if entry.references == 0 and _LOCK_REGISTRY.get(key) is entry:
                del _LOCK_REGISTRY[key]


class AnalysisStore:
    """Store source-hash entries beneath one local cache directory.

    Schema v2 is deliberately fail-closed: unknown fields, duplicate JSON keys,
    and unrecognized versions are quarantined. Future migrations must decode an
    older explicit schema before producing this exact v2 structure.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = _absolute_lexical_path(Path(directory))
        self._root: _PinnedCacheRoot | None = None
        self._closed = False
        self._pin_error: AnalysisStoreError | None = None
        try:
            self._root = self._pin_root()
        except AnalysisStoreError as exc:
            self._pin_error = exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root = self._root
        self._root = None
        if root is not None:
            root.handle.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def cache_path(self, key: AnalysisKey) -> Path:
        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        return self._directory / self._cache_name(key)

    def load(self, key: AnalysisKey) -> StoryMap | None:
        """Return a valid map, preserving the legacy ``None``-on-failure API."""

        return self.inspect(key).story_map

    def inspect(self, key: AnalysisKey) -> AnalysisLookup:
        """Read one bounded direct child through the pinned analysis root."""

        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        root = self._root
        if root is None or self._closed:
            return AnalysisLookup("missing", None)
        cache_name = self._cache_name(key)
        with _cache_key_lock(root.identity, cache_name):
            cache_file: BinaryIO | None = None
            try:
                self._verify_root()
                self._reclaim_stale_temporaries(cache_name)
                self._verify_root()
                cache_file = self._open_existing(cache_name)
                details = self._verify_opened_child(cache_file, cache_name)
                identity = details.st_dev, details.st_ino
                if details.st_size > _MAX_CACHE_BYTES:
                    raise _CacheValidationError(
                        "analysis cache exceeds the size limit"
                    )
                payload = _read_bounded(cache_file, _MAX_CACHE_BYTES)
                self._verify_root()
                self._verify_opened_child(cache_file, cache_name, identity)
                document = json.loads(
                    payload.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_object,
                )
                return AnalysisLookup("ready", self._decode_document(document, key))
            except FileNotFoundError:
                return AnalysisLookup("missing", None)
            except (
                json.JSONDecodeError,
                UnicodeError,
                RecursionError,
                OverflowError,
                ValueError,
                _CacheValidationError,
            ):
                if (
                    cache_file is not None
                    and "identity" in locals()
                    and self._quarantine(cache_name, cache_file, identity)
                ):
                    return AnalysisLookup("invalid", None)
                return AnalysisLookup("missing", None)
            except (AnalysisStoreError, OSError, RuntimeError):
                return AnalysisLookup("missing", None)
            finally:
                if cache_file is not None:
                    cache_file.close()

    def save(self, key: AnalysisKey, story_map: StoryMap) -> None:
        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        if not isinstance(story_map, StoryMap):
            raise TypeError("story_map must be a StoryMap")
        if story_map.source_hash != key.source_hash:
            raise ValueError("story map source hash does not match the analysis key")
        root = self._require_root()
        cache_name = self._cache_name(key)
        document = self._encode_document(key, story_map)
        with _cache_key_lock(root.identity, cache_name):
            self._verify_root()
            self._reclaim_stale_temporaries(cache_name)
            self._write_atomically(cache_name, document)

    @staticmethod
    def _cache_name(key: AnalysisKey) -> str:
        return f"{key.digest()}.json"

    def _require_root(self) -> _PinnedCacheRoot:
        if self._closed:
            raise AnalysisStoreError("analysis store is closed")
        if self._root is None:
            raise AnalysisStoreError("analysis directory is unsafe") from self._pin_error
        return self._root

    def _pin_root(self) -> _PinnedCacheRoot:
        handle: _DirectoryHandle | None = None
        try:
            if _contains_redirect(self._directory):
                raise AnalysisStoreError(
                    "analysis directory contains a filesystem redirect"
                )
            self._directory.mkdir(parents=True, exist_ok=True)
            if _contains_redirect(self._directory):
                raise AnalysisStoreError(
                    "analysis directory contains a filesystem redirect"
                )
            handle = _open_directory_without_redirect(self._directory)
            details = os.fstat(handle.fileno())
            final_path = _opened_final_path(handle)
            expected_path = self._directory.resolve(strict=True)
            if (
                not stat.S_ISDIR(details.st_mode)
                or _details_are_redirect(details)
                or _path_key(final_path) != _path_key(expected_path)
            ):
                raise AnalysisStoreError("analysis directory identity is unsafe")
            return _PinnedCacheRoot(
                handle=handle,
                identity=(details.st_dev, details.st_ino),
                final_path=final_path,
            )
        except AnalysisStoreError:
            if handle is not None:
                handle.close()
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if handle is not None:
                handle.close()
            raise AnalysisStoreError("could not pin analysis directory") from exc

    def _verify_root(self) -> None:
        root = self._require_root()
        try:
            details = os.fstat(root.handle.fileno())
            current_path = _opened_final_path(root.handle)
            named = os.stat(self._directory, follow_symlinks=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AnalysisStoreError("analysis directory could not be verified") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or _details_are_redirect(details)
            or (details.st_dev, details.st_ino) != root.identity
            or (named.st_dev, named.st_ino) != root.identity
            or _path_key(current_path) != _path_key(root.final_path)
            or _path_key(current_path) != _path_key(self._directory)
        ):
            raise AnalysisStoreError("analysis directory changed or is unsafe")

    def _open_existing(self, child_name: str) -> BinaryIO:
        descriptor = self._open_relative(
            child_name, create=False, writable=False
        )
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def _open_relative(
        self, child_name: str, *, create: bool, writable: bool
    ) -> int:
        if Path(child_name).name != child_name or child_name in ("", ".", ".."):
            raise AnalysisStoreError("analysis cache child name is unsafe")
        if os.name == "nt":
            return self._open_windows_relative(
                child_name, create=create, writable=writable
            )
        if os.open not in os.supports_dir_fd or not getattr(os, "O_NOFOLLOW", 0):
            raise AnalysisStoreError("safe relative analysis cache I/O is unavailable")
        flags = (
            (os.O_WRONLY if writable else os.O_RDONLY)
            | getattr(os, "O_BINARY", 0)
            | os.O_NOFOLLOW
        )
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        return os.open(
            child_name,
            flags,
            0o600,
            dir_fd=self._require_root().handle.fileno(),
        )

    def _verify_opened_child(
        self,
        child: BinaryIO,
        expected_name: str,
        expected_identity: tuple[int, int] | None = None,
    ):
        try:
            details = os.fstat(child.fileno())
            opened_path = _opened_final_path(child)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AnalysisStoreError("analysis cache child could not be verified") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or _details_are_redirect(details)
            or _path_key(opened_path.parent)
            != _path_key(self._require_root().final_path)
            or opened_path.name != expected_name
            or (
                expected_identity is not None
                and (details.st_dev, details.st_ino) != expected_identity
            )
        ):
            raise AnalysisStoreError("analysis cache child is unsafe")
        return details

    def _reclaim_stale_temporaries(self, cache_name: str) -> None:
        prefix = f".{cache_name}."
        root = self._require_root()
        try:
            names = (
                os.listdir(root.handle.fileno())
                if os.name != "nt"
                else _list_windows_directory(root.handle)
            )
        except OSError:
            return
        self._verify_root()
        for name in names:
            if name.startswith(prefix) and name.endswith(".tmp"):
                self._delete_relative_regular(name)

    def _delete_relative_regular(self, child_name: str) -> None:
        child: BinaryIO | None = None
        try:
            child = self._open_existing(child_name)
            details = self._verify_opened_child(child, child_name)
            identity = details.st_dev, details.st_ino
            if os.name == "nt":
                self._mark_windows_delete(child.fileno())
            else:
                self._unlink_relative_if_identity(child_name, identity)
        except (FileNotFoundError, AnalysisStoreError, OSError, RuntimeError):
            return
        finally:
            if child is not None:
                child.close()

    def _write_atomically(
        self, target_name: str, document: dict[str, object]
    ) -> None:
        temporary_name: str | None = None
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        renamed = False
        committed = False
        try:
            for _ in range(32):
                temporary_name = (
                    f".{target_name}.{secrets.token_hex(16)}.tmp"
                )
                try:
                    descriptor = self._open_relative(
                        temporary_name, create=True, writable=True
                    )
                    break
                except FileExistsError:
                    continue
            if descriptor is None or temporary_name is None:
                raise AnalysisStoreError(
                    "could not allocate analysis cache temporary file"
                )
            view = _DirectoryHandle(descriptor)
            details = self._verify_opened_child(view, temporary_name)
            identity = details.st_dev, details.st_ino
            writer = _BoundedUtf8Writer(descriptor, _MAX_CACHE_BYTES)
            json.dump(
                document,
                writer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            os.fsync(descriptor)
            self._verify_root()
            self._verify_opened_child(view, temporary_name, identity)
            self._replace_relative(descriptor, temporary_name, target_name)
            renamed = True
            self._verify_root()
            self._verify_opened_child(view, target_name, identity)
            self._fsync_directory()
            committed = True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, AnalysisStoreError):
                raise
            raise AnalysisStoreError("could not save analysis cache") from exc
        finally:
            if descriptor is not None and not committed:
                try:
                    if os.name == "nt":
                        self._mark_windows_delete(descriptor)
                    elif temporary_name is not None and identity is not None:
                        self._unlink_relative_if_identity(
                            target_name if renamed else temporary_name,
                            identity,
                        )
                except OSError:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _replace_relative(
        self, descriptor: int, temporary_name: str, target_name: str
    ) -> None:
        if os.name == "nt":
            self._rename_windows_handle(
                descriptor, target_name, replace_existing=True
            )
            return
        if os.replace not in os.supports_dir_fd:
            raise AnalysisStoreError(
                "safe relative analysis cache replacement is unavailable"
            )
        root_descriptor = self._require_root().handle.fileno()
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )

    def _quarantine(
        self,
        cache_name: str,
        cache_file: BinaryIO,
        expected_identity: tuple[int, int],
    ) -> bool:
        try:
            self._verify_root()
            self._verify_opened_child(
                cache_file, cache_name, expected_identity
            )
            for _ in range(32):
                invalid_name = (
                    f"{cache_name}.{secrets.token_hex(16)}.invalid"
                )
                try:
                    if os.name == "nt":
                        self._rename_windows_handle(
                            cache_file.fileno(),
                            invalid_name,
                            replace_existing=False,
                        )
                    else:
                        self._quarantine_posix(
                            cache_file.fileno(),
                            cache_name,
                            invalid_name,
                            expected_identity,
                        )
                    self._verify_root()
                    self._fsync_directory()
                    return True
                except FileExistsError:
                    continue
        except (AnalysisStoreError, OSError, RuntimeError, ValueError):
            return False
        return False

    def _quarantine_posix(
        self,
        descriptor: int,
        cache_name: str,
        invalid_name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        root_descriptor = self._require_root().handle.fileno()
        if os.link not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
            raise AnalysisStoreError(
                "safe relative analysis cache quarantine is unavailable"
            )
        os.link(
            cache_name,
            invalid_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        invalid = os.stat(
            invalid_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (invalid.st_dev, invalid.st_ino) != expected_identity:
            os.unlink(invalid_name, dir_fd=root_descriptor)
            raise AnalysisStoreError("analysis cache changed during quarantine")
        named = os.stat(
            cache_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (named.st_dev, named.st_ino) != expected_identity:
            os.unlink(invalid_name, dir_fd=root_descriptor)
            raise AnalysisStoreError("analysis cache changed during quarantine")
        os.unlink(cache_name, dir_fd=root_descriptor)

    def _unlink_relative_if_identity(
        self, child_name: str, expected_identity: tuple[int, int]
    ) -> None:
        root_descriptor = self._require_root().handle.fileno()
        if os.unlink not in os.supports_dir_fd:
            raise AnalysisStoreError(
                "safe relative analysis cache cleanup is unavailable"
            )
        try:
            details = os.stat(
                child_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(details.st_mode)
            or (details.st_dev, details.st_ino) != expected_identity
        ):
            return
        os.unlink(child_name, dir_fd=root_descriptor)

    def _fsync_directory(self) -> None:
        root = self._require_root().handle
        if os.name == "nt":
            _flush_windows_directory(root)
        else:
            os.fsync(root.fileno())

    def _open_windows_relative(
        self, child_name: str, *, create: bool, writable: bool
    ) -> int:
        return _open_windows_relative(
            self._require_root().handle,
            child_name,
            create=create,
            writable=writable,
        )

    def _rename_windows_handle(
        self, descriptor: int, target_name: str, *, replace_existing: bool
    ) -> None:
        _rename_windows_handle(
            descriptor,
            self._require_root().handle,
            target_name,
            replace_existing=replace_existing,
        )

    @staticmethod
    def _mark_windows_delete(descriptor: int) -> None:
        _mark_windows_delete(descriptor)

    @staticmethod
    def _encode_document(key: AnalysisKey, story_map: StoryMap) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "analysis_key": {"source_hash": key.source_hash, "model": key.model, "prompt_version": key.prompt_version, "dlc_version": key.dlc_version},
            "story_map": {
                "source_hash": story_map.source_hash,
                "text_length": story_map.text_length,
                "chapters": [
                    {"id": chapter.id, "index": chapter.index, "start_offset": chapter.start_offset, "end_offset": chapter.end_offset, "title": chapter.title, "summary": chapter.summary,
                     "scenes": [{"id": scene.id, "index": scene.index, "start_offset": scene.start_offset, "end_offset": scene.end_offset, "summary": scene.summary, "pace": scene.pace} for scene in chapter.scenes]}
                    for chapter in story_map.chapters
                ],
            },
        }

    def _decode_document(self, document: object, expected_key: AnalysisKey) -> StoryMap:
        raw_document = _exact_object(document, _DOCUMENT_KEYS, "cache document")
        schema_version = raw_document["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != _SCHEMA_VERSION:
            raise _CacheValidationError("cache schema version is unsupported")
        raw_key = _exact_object(raw_document["analysis_key"], _KEY_KEYS, "analysis key")
        key = AnalysisKey(source_hash=_required_string(raw_key["source_hash"], "key source hash"), model=_required_string(raw_key["model"], "key model"), prompt_version=_required_string(raw_key["prompt_version"], "key prompt version"), dlc_version=_required_string(raw_key["dlc_version"], "key DLC version"))
        if key != expected_key:
            raise _CacheValidationError("cache key does not match request")
        raw_map = _exact_object(raw_document["story_map"], _MAP_KEYS, "story map")
        source_hash = _required_string(raw_map["source_hash"], "story map source hash")
        if source_hash != expected_key.source_hash:
            raise _CacheValidationError("story map source hash does not match request")
        try:
            return StoryMap(source_hash=source_hash, text_length=_required_integer(raw_map["text_length"], "story text length"), chapters=tuple(self._decode_chapter(raw_chapter) for raw_chapter in _required_array(raw_map["chapters"], "story chapters")))
        except ValueError as exc:
            raise _CacheValidationError("story map is invalid") from exc

    @staticmethod
    def _decode_chapter(raw_chapter: object) -> StoryChapter:
        chapter = _exact_object(raw_chapter, _CHAPTER_KEYS, "chapter")
        try:
            return StoryChapter(id=_required_string(chapter["id"], "chapter id"), index=_required_integer(chapter["index"], "chapter index"), start_offset=_required_integer(chapter["start_offset"], "chapter start"), end_offset=_required_integer(chapter["end_offset"], "chapter end"), title=_required_string_or_empty(chapter["title"], "chapter title"), summary=_required_string(chapter["summary"], "chapter summary"), scenes=tuple(AnalysisStore._decode_scene(raw_scene) for raw_scene in _required_array(chapter["scenes"], "chapter scenes")))
        except ValueError as exc:
            raise _CacheValidationError("chapter is invalid") from exc

    @staticmethod
    def _decode_scene(raw_scene: object) -> StoryScene:
        scene = _exact_object(raw_scene, _SCENE_KEYS, "scene")
        try:
            return StoryScene(id=_required_string(scene["id"], "scene id"), index=_required_integer(scene["index"], "scene index"), start_offset=_required_integer(scene["start_offset"], "scene start"), end_offset=_required_integer(scene["end_offset"], "scene end"), summary=_required_string(scene["summary"], "scene summary"), pace=_required_number(scene["pace"], "scene pace"))
        except ValueError as exc:
            raise _CacheValidationError("scene is invalid") from exc


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _CacheValidationError("cache JSON contains a duplicate key")
        result[key] = value
    return result


class _BoundedUtf8Writer:
    def __init__(self, descriptor: int, maximum_size: int) -> None:
        self._descriptor = descriptor
        self._maximum_size = maximum_size
        self._written = 0

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8", "strict")
        if self._written + len(encoded) > self._maximum_size:
            raise AnalysisStoreError("analysis cache exceeds the size limit")
        view = memoryview(encoded)
        while view:
            written = os.write(self._descriptor, view)
            if written <= 0:
                raise OSError("analysis cache write made no progress")
            view = view[written:]
        self._written += len(encoded)
        return len(value)


def _read_bounded(handle, maximum_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = handle.read(min(64 * 1024, maximum_size + 1 - total))
        if chunk == b"":
            return b"".join(chunks)
        if not isinstance(chunk, (bytes, bytearray)):
            raise OSError("analysis cache read did not return bytes")
        total += len(chunk)
        if total > maximum_size:
            raise _CacheValidationError("analysis cache exceeds the size limit")
        chunks.append(bytes(chunk))


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _contains_redirect(path: Path) -> bool:
    absolute = path.absolute()
    return any(_is_redirect(candidate) for candidate in (absolute, *absolute.parents))


def _details_are_redirect(details) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_point)


def _is_redirect(path: Path) -> bool:
    """Recognize symlinks, Windows junctions, and detectable reparse points."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        details = os.lstat(path)
    except (FileNotFoundError, OSError):
        return False
    return _details_are_redirect(details)


def _open_directory_without_redirect(path: Path) -> _DirectoryHandle:
    if os.name == "nt":
        return _open_windows_directory_without_redirect(path)
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise OSError("safe directory opening is unavailable")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    return _DirectoryHandle(descriptor)


def _open_windows_directory_without_redirect(path: Path) -> _DirectoryHandle:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.fspath(path),
        generic_read | generic_write,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        try:
            close_handle(handle)
        except BaseException:
            pass
        raise
    return _DirectoryHandle(descriptor)


def _flush_windows_directory(root: _DirectoryHandle) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    flush = kernel32.FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    if not flush(msvcrt.get_osfhandle(root.fileno())):
        raise ctypes.WinError(ctypes.get_last_error())


def _list_windows_directory(root: _DirectoryHandle) -> list[str]:
    """Enumerate a pinned directory handle without reopening its pathname."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileIdBothDirectoryInfo(ctypes.Structure):
        _fields_ = (
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_ubyte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        )

    file_id_both_directory_info = 10
    file_id_both_directory_restart_info = 11
    error_no_more_files = 18
    buffer_size = 64 * 1024
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    native_handle = msvcrt.get_osfhandle(root.fileno())
    names: list[str] = []
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        if not get_information(
            native_handle,
            (
                file_id_both_directory_restart_info
                if restart
                else file_id_both_directory_info
            ),
            ctypes.byref(buffer),
            buffer_size,
        ):
            error = ctypes.get_last_error()
            if error == error_no_more_files:
                return names
            raise ctypes.WinError(error)
        restart = False
        offset = 0
        while True:
            if offset + FileIdBothDirectoryInfo.FileName.offset > buffer_size:
                raise OSError("invalid pinned directory enumeration result")
            entry = FileIdBothDirectoryInfo.from_buffer(buffer, offset)
            name_length = int(entry.FileNameLength)
            name_offset = offset + FileIdBothDirectoryInfo.FileName.offset
            if (
                name_length % 2
                or name_length < 0
                or name_offset + name_length > buffer_size
            ):
                raise OSError("invalid pinned directory entry name")
            raw_name = ctypes.string_at(
                ctypes.addressof(buffer) + name_offset, name_length
            )
            names.append(raw_name.decode("utf-16-le", "strict"))
            next_offset = int(entry.NextEntryOffset)
            if next_offset == 0:
                break
            if next_offset < FileIdBothDirectoryInfo.FileName.offset:
                raise OSError("invalid pinned directory entry offset")
            offset += next_offset


def _open_windows_relative(
    root: _DirectoryHandle,
    child_name: str,
    *,
    create: bool,
    writable: bool,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        )

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        )

    generic_read = 0x80000000
    generic_write = 0x40000000
    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    file_open = 1
    file_create = 2
    file_attribute_normal = 0x00000080
    file_open_reparse_point = 0x00200000
    file_non_directory_file = 0x00000040
    file_synchronous_io_nonalert = 0x00000020
    object_case_insensitive = 0x00000040

    name_buffer = ctypes.create_unicode_buffer(child_name)
    encoded_name = child_name.encode("utf-16-le")
    object_name = UnicodeString(
        Length=len(encoded_name),
        MaximumLength=len(encoded_name) + 2,
        Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        Length=ctypes.sizeof(ObjectAttributes),
        RootDirectory=msvcrt.get_osfhandle(root.fileno()),
        ObjectName=ctypes.pointer(object_name),
        Attributes=object_case_insensitive,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    status_block = IoStatusBlock()
    native_handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    create_file = ntdll.NtCreateFile
    create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    create_file.restype = ctypes.c_long
    status = create_file(
        ctypes.byref(native_handle),
        (generic_write if writable else generic_read)
        | delete_access
        | file_read_attributes
        | synchronize,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        file_attribute_normal,
        file_share_read | file_share_write | file_share_delete,
        file_create if create else file_open,
        file_open_reparse_point
        | file_non_directory_file
        | file_synchronous_io_nonalert,
        None,
        0,
    )
    if status < 0:
        to_dos_error = ntdll.RtlNtStatusToDosError
        to_dos_error.argtypes = (ctypes.c_long,)
        to_dos_error.restype = wintypes.ULONG
        error = int(to_dos_error(status))
        if create and error in (80, 183):
            raise FileExistsError(child_name)
        if not create and error in (2, 3):
            raise FileNotFoundError(child_name)
        raise OSError(error, "relative analysis cache open failed")
    try:
        return msvcrt.open_osfhandle(
            native_handle.value,
            (os.O_WRONLY if writable else os.O_RDONLY)
            | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(native_handle)
        raise


def _rename_windows_handle(
    descriptor: int,
    root: _DirectoryHandle,
    target_name: str,
    *,
    replace_existing: bool,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * len(target_name)),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        )

    info = FileRenameInfo()
    info.ReplaceIfExists = bool(replace_existing)
    info.RootDirectory = msvcrt.get_osfhandle(root.fileno())
    info.FileNameLength = len(target_name.encode("utf-16-le"))
    info.FileName = target_name
    status_block = IoStatusBlock()
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    )
    set_information.restype = ctypes.c_long
    status = set_information(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(status_block),
        ctypes.byref(info),
        ctypes.sizeof(info),
        10,
    )
    if status < 0:
        to_dos_error = ntdll.RtlNtStatusToDosError
        to_dos_error.argtypes = (ctypes.c_long,)
        to_dos_error.restype = wintypes.ULONG
        error = int(to_dos_error(status))
        if not replace_existing and error in (80, 183):
            raise FileExistsError(target_name)
        raise OSError(error, "relative analysis cache rename failed")


def _mark_windows_delete(descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = (("DeleteFile", ctypes.c_ubyte),)

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        )

    disposition = FileDispositionInfo(DeleteFile=True)
    status_block = IoStatusBlock()
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    )
    set_information.restype = ctypes.c_long
    status = set_information(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(status_block),
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
        13,
    )
    if status < 0:
        to_dos_error = ntdll.RtlNtStatusToDosError
        to_dos_error.argtypes = (ctypes.c_long,)
        to_dos_error.restype = wintypes.ULONG
        error = int(to_dos_error(status))
        raise OSError(error, "analysis cache handle cleanup failed")


def _exact_object(value: object, expected_keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise _CacheValidationError(f"{name} fields are invalid")
    return value


def _required_array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise _CacheValidationError(f"{name} must be an array")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _CacheValidationError(f"{name} must be a non-empty string")
    return value


def _required_string_or_empty(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _CacheValidationError(f"{name} must be a string")
    return value


def _required_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _CacheValidationError(f"{name} must be an integer")
    return value


def _required_number(value: object, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CacheValidationError(f"{name} must be a number")
    return value
