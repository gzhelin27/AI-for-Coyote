"""Atomic, schema-versioned local storage for faithful story analysis."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any, Iterator

from .models import AnalysisKey, StoryChapter, StoryMap, StoryScene


_SCHEMA_VERSION = 2
_DOCUMENT_KEYS = frozenset(("schema_version", "analysis_key", "story_map"))
_KEY_KEYS = frozenset(("source_hash", "model", "prompt_version", "dlc_version"))
_MAP_KEYS = frozenset(("source_hash", "text_length", "chapters"))
_CHAPTER_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "title", "summary", "scenes"))
_SCENE_KEYS = frozenset(("id", "index", "start_offset", "end_offset", "summary", "pace"))


class AnalysisStoreError(OSError):
    """A local analysis cache entry could not be written safely."""


class _CacheValidationError(ValueError):
    """A local cache document cannot be trusted as a StoryMap."""


class _LockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.references = 0


_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[str, _LockEntry] = {}


@contextmanager
def _cache_path_lock(path: Path) -> Iterator[None]:
    """Serialize one resolved cache path across all in-process store instances."""

    key = str(path.resolve(strict=False))
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
        self._directory = Path(directory)

    def cache_path(self, key: AnalysisKey) -> Path:
        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        directory = self._resolved_directory()
        return (directory if directory is not None else self._directory) / f"{key.digest()}.json"

    def load(self, key: AnalysisKey) -> StoryMap | None:
        cache_path = self.cache_path(key)
        if self._resolved_directory() is None:
            return None
        with _cache_path_lock(cache_path):
            self._reclaim_stale_temporaries(cache_path)
            if self._resolved_directory() is None or _is_redirect(cache_path):
                return None
            identity = _regular_file_identity(cache_path)
            if identity is None:
                return None
            try:
                document = json.loads(
                    cache_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_reject_duplicate_object,
                )
                return self._decode_document(document, key)
            except (json.JSONDecodeError, UnicodeDecodeError, _CacheValidationError):
                self._quarantine(cache_path, identity)
                return None
            except OSError:
                return None

    def save(self, key: AnalysisKey, story_map: StoryMap) -> None:
        if not isinstance(key, AnalysisKey):
            raise TypeError("key must be an AnalysisKey")
        if not isinstance(story_map, StoryMap):
            raise TypeError("story_map must be a StoryMap")
        if story_map.source_hash != key.source_hash:
            raise ValueError("story map source hash does not match the analysis key")
        directory = self._prepare_directory()
        target = directory / f"{key.digest()}.json"
        document = self._encode_document(key, story_map)
        with _cache_path_lock(target):
            self._reclaim_stale_temporaries(target)
            if self._resolved_directory() != directory:
                raise AnalysisStoreError("analysis directory changed or is unsafe")
            if _is_redirect(target):
                raise AnalysisStoreError("analysis cache target is a filesystem redirect")
            self._write_atomically(directory, target, document)

    @staticmethod
    def _reclaim_stale_temporaries(cache_path: Path) -> None:
        """Remove only regular abandoned temp files for this generated cache key."""

        prefix = f".{cache_path.name}."
        try:
            candidates = tuple(cache_path.parent.iterdir())
        except OSError:
            return
        for candidate in candidates:
            if not (
                candidate.name.startswith(prefix)
                and candidate.name.endswith(".tmp")
            ):
                continue
            identity = _regular_file_identity(candidate)
            if identity is not None:
                _unlink_if_identity_matches(candidate, identity)

    def _prepare_directory(self) -> Path:
        if self._directory_has_redirect():
            raise AnalysisStoreError("analysis directory contains a filesystem redirect")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AnalysisStoreError("could not create analysis directory") from exc
        directory = self._resolved_directory()
        if directory is None:
            raise AnalysisStoreError("analysis directory is unsafe")
        return directory

    def _resolved_directory(self) -> Path | None:
        if self._directory_has_redirect():
            return None
        try:
            return self._directory.resolve(strict=False)
        except OSError:
            return None

    def _directory_has_redirect(self) -> bool:
        path = self._directory.absolute()
        for candidate in (path, *path.parents):
            try:
                if _is_redirect(candidate):
                    return True
            except OSError:
                return True
        return False

    def _write_atomically(self, directory: Path, target: Path, document: dict[str, object]) -> None:
        temporary_path: Path | None = None
        temporary_identity: tuple[int, int] | None = None
        descriptor: int | None = None
        try:
            temporary_path, descriptor = self._open_temporary_file(directory, target.name)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise AnalysisStoreError("could not verify analysis cache temporary file")
            temporary_identity = details.st_dev, details.st_ino
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with handle:
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(target)
            self._fsync_directory(directory)
        except (OSError, TypeError, ValueError) as exc:
            if isinstance(exc, AnalysisStoreError):
                raise
            raise AnalysisStoreError("could not save analysis cache") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None and temporary_identity is not None:
                _unlink_if_identity_matches(temporary_path, temporary_identity)
            elif temporary_path is not None:
                _unlink_generated_regular_temp(temporary_path)

    @staticmethod
    def _open_temporary_file(directory: Path, target_name: str) -> tuple[Path, int]:
        for _ in range(32):
            temporary_path = directory / f".{target_name}.{secrets.token_hex(16)}.tmp"
            try:
                return temporary_path, os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                raise AnalysisStoreError("could not create analysis cache temporary file") from exc
        raise AnalysisStoreError("could not allocate analysis cache temporary file")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Fsync metadata where directory descriptors exist.

        Windows has no portable stdlib directory-fsync handle; the temporary
        file itself is fsynced before its atomic replacement on that platform.
        """

        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _quarantine(self, cache_path: Path, expected_identity: tuple[int, int]) -> None:
        if self._resolved_directory() is None or _is_redirect(cache_path):
            return
        if _regular_file_identity(cache_path) != expected_identity:
            return
        for _ in range(32):
            invalid_path = cache_path.with_name(f"{cache_path.name}.{secrets.token_hex(16)}.invalid")
            try:
                if invalid_path.exists() or _is_redirect(invalid_path):
                    continue
                cache_path.rename(invalid_path)
                return
            except FileNotFoundError:
                return
            except FileExistsError:
                continue
            except OSError:
                return

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


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    if _is_redirect(path):
        return None
    try:
        details = os.lstat(path)
    except (FileNotFoundError, OSError):
        return None
    if not stat.S_ISREG(details.st_mode):
        return None
    return details.st_dev, details.st_ino


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
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_point)


def _unlink_if_identity_matches(path: Path, expected_identity: tuple[int, int]) -> None:
    if _regular_file_identity(path) != expected_identity:
        return
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _unlink_generated_regular_temp(path: Path) -> None:
    """Clean a just-created temp after fstat failed without following links."""

    try:
        details = os.lstat(path)
    except (FileNotFoundError, OSError):
        return
    if not stat.S_ISREG(details.st_mode):
        return
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


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
