"""Pinned, atomic persistence for validated imported story bytes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat

from backend.timeline.replay_store import _opened_final_path

from .models import ImportedStory
from .offline_analysis import (
    _DirectoryHandle,
    _absolute_lexical_path,
    _contains_redirect,
    _open_directory_without_redirect,
    _path_key,
)


_SOURCE_EXTENSIONS = frozenset((".txt", ".md", ".docx"))
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


class StorySourceStorageError(OSError):
    """Validated source bytes could not be committed inside the trusted root."""


@dataclass(frozen=True, slots=True)
class StoredStorySource:
    source_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class _PinnedRoot:
    handle: _DirectoryHandle
    identity: tuple[int, int]
    final_path: Path


class PinnedStorySourceStore:
    """Own one pinned flat directory and atomically commit opaque basenames."""

    def __init__(self, directory: Path, *, project_root: Path) -> None:
        configured = _absolute_lexical_path(Path(directory))
        root = _absolute_lexical_path(Path(project_root))
        try:
            configured.relative_to(root)
        except ValueError as exc:
            raise StorySourceStorageError(
                "story source directory is outside the project root"
            ) from exc
        if configured == root or _contains_redirect(root) or _contains_redirect(configured):
            raise StorySourceStorageError("story source directory is unsafe")
        try:
            configured.mkdir(parents=True, exist_ok=True)
            if _contains_redirect(configured):
                raise StorySourceStorageError("story source directory is unsafe")
            project_final = root.resolve(strict=True)
            handle = _open_directory_without_redirect(configured)
            details = os.fstat(handle.fileno())
            final_path = _opened_final_path(handle)
            if not stat.S_ISDIR(details.st_mode):
                raise StorySourceStorageError("story source directory is not a directory")
            final_path.relative_to(project_final)
            if _path_key(final_path) != _path_key(configured.resolve(strict=True)):
                raise StorySourceStorageError("story source directory identity is unsafe")
        except StorySourceStorageError:
            if "handle" in locals():
                handle.close()
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            if "handle" in locals():
                handle.close()
            raise StorySourceStorageError(
                "story source directory could not be pinned"
            ) from exc
        self._root = _PinnedRoot(
            handle=handle,
            identity=(details.st_dev, details.st_ino),
            final_path=final_path,
        )
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._root.handle.close()

    def delete(self, stored: StoredStorySource) -> None:
        """Remove one committed source without resolving a caller-owned path."""

        if not isinstance(stored, StoredStorySource):
            raise TypeError("stored source must be a StoredStorySource")
        suffix = stored.path.suffix.lower()
        name = f"{stored.source_id}{suffix}"
        expected = self._root.final_path / name
        if (
            not _OPAQUE_ID.fullmatch(stored.source_id)
            or suffix not in _SOURCE_EXTENSIONS
            or stored.path.name != name
            or _path_key(stored.path) != _path_key(expected)
        ):
            raise StorySourceStorageError("stored story source identity is unsafe")
        self._verify_root()
        try:
            if os.name == "nt":
                descriptor = self._open_windows_existing(name)
                try:
                    self._verify_opened_child(descriptor)
                    self._mark_windows_delete(descriptor)
                finally:
                    os.close(descriptor)
            elif os.unlink in os.supports_dir_fd:
                os.unlink(name, dir_fd=self._root.handle.fileno())
            else:
                raise StorySourceStorageError(
                    "safe relative story source deletion is unavailable"
                )
        except FileNotFoundError:
            return
        except StorySourceStorageError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise StorySourceStorageError(
                "story source could not be deleted"
            ) from exc
        self._verify_root()
        self._fsync_directory()

    def store(self, story: ImportedStory) -> StoredStorySource:
        if not isinstance(story, ImportedStory):
            raise TypeError("story must be an ImportedStory")
        if story.extension not in _SOURCE_EXTENSIONS:
            raise StorySourceStorageError("story source extension is unsupported")
        if self._closed:
            raise StorySourceStorageError("story source store is closed")
        for _ in range(32):
            source_id = secrets.token_urlsafe(18)
            if not _OPAQUE_ID.fullmatch(source_id):
                continue
            final_name = f"{source_id}{story.extension}"
            try:
                self._commit(final_name, story.original_bytes)
            except FileExistsError:
                continue
            return StoredStorySource(
                source_id=source_id,
                path=self._root.final_path / final_name,
            )
        raise StorySourceStorageError("could not allocate a story source id")

    def _commit(self, final_name: str, payload: bytes) -> None:
        temporary_name = f".{final_name}.{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        renamed = False
        committed = False
        try:
            self._verify_root()
            descriptor = self._open_temporary(temporary_name)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise StorySourceStorageError(
                    "story source temporary is not a regular file"
                )
            self._verify_opened_child(descriptor)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("story source write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            self._verify_root()
            self._replace_relative(descriptor, temporary_name, final_name)
            renamed = True
            self._verify_root()
            self._fsync_directory()
            committed = True
        except FileExistsError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, StorySourceStorageError):
                raise
            raise StorySourceStorageError("story source could not be committed") from exc
        finally:
            cleanup_error: OSError | None = None
            if os.name == "nt" and descriptor is not None and not committed:
                try:
                    self._mark_windows_delete(descriptor)
                except OSError as exc:
                    cleanup_error = exc
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if os.name != "nt" and renamed and not committed:
                self._unlink_relative(final_name)
                try:
                    self._fsync_directory()
                except OSError:
                    pass
            elif os.name != "nt" and not renamed:
                self._unlink_relative(temporary_name)
            if cleanup_error is not None:
                raise StorySourceStorageError(
                    "story source failure cleanup could not be committed"
                ) from cleanup_error

    def _open_temporary(self, temporary_name: str) -> int:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if os.name == "nt":
            return self._open_windows_temporary(temporary_name)
        if os.open not in os.supports_dir_fd or not getattr(os, "O_NOFOLLOW", 0):
            raise StorySourceStorageError(
                "safe relative story source creation is unavailable"
            )
        return os.open(
            temporary_name, flags, 0o600, dir_fd=self._root.handle.fileno()
        )

    def _open_windows_temporary(self, temporary_name: str) -> int:
        return self._open_windows_relative(
            temporary_name, create=True, writable=True
        )

    def _open_windows_existing(self, name: str) -> int:
        return self._open_windows_relative(name, create=False, writable=False)

    def _open_windows_relative(
        self, name: str, *, create: bool, writable: bool
    ) -> int:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        if Path(name).name != name:
            raise StorySourceStorageError("story source child name is unsafe")

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

        generic_write = 0x40000000
        delete_access = 0x00010000
        file_read_attributes = 0x00000080
        synchronize = 0x00100000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_create = 2
        file_attribute_normal = 0x00000080
        file_open_reparse_point = 0x00200000
        file_non_directory_file = 0x00000040
        file_synchronous_io_nonalert = 0x00000020
        object_case_insensitive = 0x00000040

        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_name = name.encode("utf-16-le")
        object_name = UnicodeString(
            Length=len(encoded_name),
            MaximumLength=len(encoded_name) + 2,
            Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = ObjectAttributes(
            Length=ctypes.sizeof(ObjectAttributes),
            RootDirectory=msvcrt.get_osfhandle(self._root.handle.fileno()),
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
            (generic_write if writable else 0)
            | delete_access
            | file_read_attributes
            | synchronize,
            ctypes.byref(attributes),
            ctypes.byref(status_block),
            None,
            file_attribute_normal,
            file_share_read | file_share_write,
            file_create if create else 1,
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
                raise FileExistsError(name)
            if not create and error in (2, 3):
                raise FileNotFoundError(name)
            raise OSError(error, "relative story source create failed")
        try:
            return msvcrt.open_osfhandle(
                native_handle.value,
                (os.O_WRONLY if writable else os.O_RDONLY)
                | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(native_handle)
            raise

    def _replace_relative(
        self, descriptor: int, temporary_name: str, final_name: str
    ) -> None:
        if os.name == "nt":
            self._rename_windows_handle(descriptor, final_name)
            return
        if (
            os.link not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
            or os.link not in os.supports_follow_symlinks
        ):
            raise StorySourceStorageError(
                "safe relative story source replacement is unavailable"
            )
        directory_descriptor = self._root.handle.fileno()
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except OSError as exc:
            try:
                os.unlink(final_name, dir_fd=directory_descriptor)
            except OSError as cleanup_exc:
                raise StorySourceStorageError(
                    "story source no-replace rollback failed"
                ) from cleanup_exc
            raise exc

    def _rename_windows_handle(self, descriptor: int, final_name: str) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileRenameInfo(ctypes.Structure):
            _fields_ = (
                ("ReplaceIfExists", ctypes.c_ubyte),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * len(final_name)),
            )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t),
            )

        info = FileRenameInfo()
        info.ReplaceIfExists = False
        info.RootDirectory = msvcrt.get_osfhandle(self._root.handle.fileno())
        encoded_name = final_name.encode("utf-16-le")
        info.FileNameLength = len(encoded_name)
        info.FileName = final_name
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
        status_block = IoStatusBlock()
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
            if error in (80, 183):
                raise FileExistsError(final_name)
            raise OSError(error, "relative story source rename failed")

    @staticmethod
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
            raise OSError(error, "story source handle cleanup failed")

    def _verify_root(self) -> None:
        if self._closed:
            raise StorySourceStorageError("story source store is closed")
        try:
            details = os.fstat(self._root.handle.fileno())
            final_path = _opened_final_path(self._root.handle)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StorySourceStorageError(
                "story source directory could not be verified"
            ) from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or (details.st_dev, details.st_ino) != self._root.identity
            or _path_key(final_path) != _path_key(self._root.final_path)
        ):
            raise StorySourceStorageError("story source directory changed")

    def _verify_opened_child(self, descriptor: int) -> None:
        try:
            with os.fdopen(os.dup(descriptor), "rb") as duplicate:
                opened = _opened_final_path(duplicate)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StorySourceStorageError(
                "story source temporary could not be verified"
            ) from exc
        if _path_key(opened.parent) != _path_key(self._root.final_path):
            raise StorySourceStorageError("story source temporary escaped its root")

    def _unlink_relative(self, name: str) -> None:
        try:
            if os.name == "nt":
                raise OSError("Windows cleanup requires the opened child handle")
            if os.unlink in os.supports_dir_fd:
                os.unlink(name, dir_fd=self._root.handle.fileno())
            else:
                raise OSError("safe relative cleanup is unavailable")
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _fsync_directory(self) -> None:
        if os.name != "nt":
            os.fsync(self._root.handle.fileno())
