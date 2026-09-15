"""Bounded local media imports and atomic, content-bound CSV records.

Duration is browser metadata, not a claim that these bytes decode as video.
Pinned file primitives are shared with the existing local analysis store.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from backend.story.analysis_store import AnalysisStore, _DirectoryHandle
from .csv_timeline import parse_video_csv
from .models import VideoCsv

_CHUNK = 1024 * 1024
_RECORD_LIMIT = 20 * 1024 * 1024
_ID = re.compile(r'[0-9a-f]{32}')


@dataclass(frozen=True, slots=True)
class VideoSource:
    source_id: str
    filename: str
    sha256: str
    size: int
    duration_ms: int
    metadata: dict = field(default_factory=lambda: {'duration_origin': 'browser'})


class VideoSourceStore:
    def __init__(self, root: Path, max_bytes: int = 4 * 1024**3):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('invalid media size limit')
        self.max_bytes = max_bytes
        self._files = AnalysisStore(root)
        self._files._require_root()

    def close(self):
        self._files.close()

    @staticmethod
    def _id(source_id):
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
            raise ValueError('invalid video source ID')
        return source_id

    @staticmethod
    def _duration(value):
        if type(value) is not int or not 0 < value <= 7 * 24 * 60 * 60 * 1000:
            raise ValueError('duration must be positive integer milliseconds within seven days')

    def _verified(self, handle, name):
        details = self._files._verify_opened_child(handle, name)
        if details.st_nlink != 1:
            raise OSError('video storage hardlink is unsafe')
        return details

    async def import_stream(self, original_name: str, chunks: AsyncIterable[bytes],
                            duration_ms: int) -> VideoSource:
        self._duration(duration_ms)
        if not isinstance(original_name, str):
            raise ValueError('invalid filename')
        filename = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_',
                          original_name.replace('\\', '/').split('/')[-1]).strip(' .')[:240]
        filename = filename or 'video'
        source_id = secrets.token_hex(16)
        name = source_id + '.media'
        temporary = '.' + source_id + '.tmp'
        files = self._files
        descriptor = None
        installed = False
        committed = False
        total, digest = 0, hashlib.sha256()

        async def disk(operation):
            task = asyncio.create_task(asyncio.to_thread(operation))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError()
            return result

        def create():
            nonlocal descriptor
            files._verify_root()
            descriptor = files._open_relative(temporary, create=True, writable=True)
            self._verified(_DirectoryHandle(descriptor), temporary)

        def write(part):
            digest.update(part)
            while part:
                written = os.write(descriptor, part)
                if written <= 0:
                    raise OSError('video write made no progress')
                part = part[written:]

        def install(source):
            nonlocal installed
            os.fsync(descriptor)
            files._verify_root()
            handle = _DirectoryHandle(descriptor)
            self._verified(handle, temporary)
            files._install_relative(descriptor, temporary, name)
            installed = True
            self._verified(handle, name)
            files._write_atomically(source_id + '.json',
                                    {'version': 1, 'source': asdict(source), 'csv': None},
                                    maximum_size=_RECORD_LIMIT, create_only=True)

        def cleanup():
            if descriptor is None:
                return
            try:
                if not committed and os.name == 'nt':
                    files._mark_windows_delete(descriptor)
            finally:
                os.close(descriptor)
            if not committed:
                files._delete_relative_regular(name if installed else temporary)
                files._delete_relative_regular(source_id + '.json')

        try:
            await disk(create)
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ValueError('video stream must yield bytes')
                total += len(chunk)
                if total > self.max_bytes:
                    raise ValueError('video exceeds size limit')
                view = memoryview(chunk)
                for offset in range(0, len(view), _CHUNK):
                    part = view[offset:offset + _CHUNK]
                    await disk(lambda: write(part))
            if total == 0:
                raise ValueError('empty video')
            source = VideoSource(source_id, filename, digest.hexdigest(), total, duration_ms)
            await disk(lambda: install(source))
            committed = True
            return source
        finally:
            await disk(cleanup)

    def _record(self, source_id):
        source_id = self._id(source_id)
        files, name = self._files, source_id + '.json'
        files._verify_root()
        with files._open_existing(name) as handle:
            before = self._verified(handle, name)
            if before.st_size > _RECORD_LIMIT:
                raise ValueError('video record exceeds size limit')
            payload = handle.read(_RECORD_LIMIT + 1)
            after = self._verified(handle, name)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('video record changed during read')
        document = json.loads(payload)
        if not isinstance(document, dict) or document.get('version') != 1:
            raise ValueError('invalid video record')
        try:
            source = VideoSource(**document['source'])
        except (KeyError, TypeError) as exc:
            raise ValueError('invalid video source metadata') from exc
        self._duration(source.duration_ms)
        if (not isinstance(source.filename, str) or not 1 <= len(source.filename) <= 240
                or re.search(r'[\\/\x00-\x1f\x7f<>:"|?*]', source.filename)
                or source.filename != source.filename.strip(' .')
                or source.metadata != {'duration_origin': 'browser'}):
            raise ValueError('invalid video display metadata')
        if (source.source_id != source_id or type(source.size) is not int
                or not 0 < source.size <= self.max_bytes
                or not isinstance(source.sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', source.sha256)):
            raise ValueError('invalid video source identity')
        media_name = source_id + '.media'
        with files._open_existing(media_name) as handle:
            before = self._verified(handle, media_name)
            if before.st_size != source.size:
                raise ValueError('video source size changed')
            digest = hashlib.sha256()
            remaining = source.size
            while remaining:
                part = handle.read(min(_CHUNK, remaining))
                if not part:
                    raise ValueError('video source truncated')
                remaining -= len(part)
                digest.update(part)
            after = self._verified(handle, media_name)
            if (digest.hexdigest() != source.sha256 or
                    (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise ValueError('video source content changed')
        files._verify_root()
        return source, document

    def get(self, source_id: str) -> VideoSource:
        return self._record(source_id)[0]

    def media_path(self, source_id: str) -> Path:
        self.get(source_id)
        return self._files._require_root().final_path / (source_id + '.media')

    def bind_csv(self, source_id: str, payload: bytes) -> VideoCsv:
        source, document = self._record(source_id)
        timeline = parse_video_csv(payload, duration_ms=source.duration_ms)
        document['csv'] = {'source_sha256': source.sha256, 'sha256': timeline.sha256,
                           'payload': payload.decode('utf-8-sig')}
        self._files._write_atomically(source_id + '.json', document,
                                     maximum_size=_RECORD_LIMIT, commit_on_replace=True)
        return timeline

    def bound_csv(self, source_id: str) -> VideoCsv | None:
        source, document = self._record(source_id)
        binding = document.get('csv')
        if binding is None:
            return None
        if not isinstance(binding, dict) or binding.get('source_sha256') != source.sha256:
            raise ValueError('CSV video identity mismatch')
        if not isinstance(binding.get('payload'), str):
            raise ValueError('invalid saved CSV')
        timeline = parse_video_csv(binding['payload'].encode('utf-8'), duration_ms=source.duration_ms)
        if timeline.sha256 != binding.get('sha256'):
            raise ValueError('CSV identity mismatch')
        return timeline
