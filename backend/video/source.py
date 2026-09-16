"""Browser-local registrations and backward-compatible uploaded media records.

Duration is browser metadata, not a claim that these bytes decode as video.
Browser-local identity hashes describe registrations, never video content.
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
from .models import VideoCsv, VideoPlan

_CHUNK = 1024 * 1024
_RECORD_LIMIT = 20 * 1024 * 1024
_ID = re.compile(r'[0-9a-f]{32}')
_SAFE_INTEGER = 2**53 - 1


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

    @staticmethod
    def _filename(original_name):
        if not isinstance(original_name, str):
            raise ValueError('invalid filename')
        filename = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_',
                          original_name.replace('\\', '/').split('/')[-1]).strip(' .')[:240]
        return filename or 'video'

    @staticmethod
    def _registration_digest(source_id, filename, size, duration_ms, last_modified):
        metadata = {'source_id': source_id, 'filename': filename, 'size': size,
                    'duration_ms': duration_ms, 'last_modified': last_modified}
        canonical = json.dumps(metadata, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(b'AI-for-Coyote:browser-local-registration:v1\0' + canonical).hexdigest()

    def register_local(self, filename, size, duration_ms, last_modified) -> VideoSource:
        """Register browser metadata without receiving or opening the video.

        Every selection gets its own identity. This synchronous method writes
        only a small JSON record and should be called from a worker thread.
        """
        self._duration(duration_ms)
        if type(size) is not int or not 0 < size <= _SAFE_INTEGER:
            raise ValueError('size must be a positive safe integer')
        if type(last_modified) is not int or not 0 <= last_modified <= _SAFE_INTEGER:
            raise ValueError('last_modified must be a nonnegative safe integer')
        filename = self._filename(filename)
        source_id = secrets.token_hex(16)
        source = VideoSource(source_id, filename,
            self._registration_digest(source_id, filename, size, duration_ms, last_modified),
            size, duration_ms, {'source_kind': 'browser_local',
                'identity_origin': 'registration_metadata', 'duration_origin': 'browser',
                'last_modified': last_modified})
        self._files._verify_root()
        self._files._write_atomically(source_id + '.json',
            {'version': 2, 'source': asdict(source), 'csv': None},
            maximum_size=4096, create_only=True)
        return source

    async def import_stream(self, original_name: str, chunks: AsyncIterable[bytes],
                            duration_ms: int) -> VideoSource:
        self._duration(duration_ms)
        filename = self._filename(original_name)
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
        if (not isinstance(document, dict) or type(document.get('version')) is not int
                or document['version'] not in (1, 2)):
            raise ValueError('invalid video record')
        local = document['version'] == 2
        if local and (set(document) != {'version', 'source', 'csv'}
                or not isinstance(document['source'], dict)
                or set(document['source']) != {'source_id', 'filename', 'sha256', 'size', 'duration_ms', 'metadata'}):
            raise ValueError('invalid browser-local record fields')
        try:
            source = VideoSource(**document['source'])
        except (KeyError, TypeError) as exc:
            raise ValueError('invalid video source metadata') from exc
        self._duration(source.duration_ms)
        if (not isinstance(source.filename, str) or not 1 <= len(source.filename) <= 240
                or re.search(r'[\\/\x00-\x1f\x7f<>:"|?*]', source.filename)
                or source.filename != source.filename.strip(' .')):
            raise ValueError('invalid video display metadata')
        if (source.source_id != source_id or type(source.size) is not int
                or not 0 < source.size <= (_SAFE_INTEGER if local else self.max_bytes)
                or not isinstance(source.sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', source.sha256)):
            raise ValueError('invalid video source identity')
        if local:
            metadata = source.metadata
            if (not isinstance(metadata, dict)
                    or set(metadata) != {'source_kind', 'identity_origin', 'duration_origin', 'last_modified'}
                    or metadata['source_kind'] != 'browser_local'
                    or metadata['identity_origin'] != 'registration_metadata'
                    or metadata['duration_origin'] != 'browser'
                    or type(metadata['last_modified']) is not int
                    or not 0 <= metadata['last_modified'] <= _SAFE_INTEGER
                    or source.sha256 != self._registration_digest(source_id, source.filename,
                        source.size, source.duration_ms, metadata['last_modified'])):
                raise ValueError('invalid browser-local registration identity')
            files._verify_root()
            return source, document
        if source.metadata != {'duration_origin': 'browser'}:
            raise ValueError('invalid uploaded video metadata')
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
        source = self.get(source_id)
        if source.metadata.get('source_kind') == 'browser_local':
            raise ValueError('browser-local video has no server media path')
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
        return self._binding(source, document)

    @staticmethod
    def _binding(source, document):
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

    def save_plan(self, source_id: str, plan: VideoPlan) -> str:
        """Atomically retain resolved values before a session can own output.

        This synchronous disk operation belongs in a worker thread. Each plan
        receives a new opaque ID, so a failed save cannot replace prior evidence.
        """
        source, document = self._record(source_id)
        timeline = self._binding(source, document)
        if (not isinstance(plan, VideoPlan) or timeline is None
                or plan.timeline_sha256 != timeline.sha256
                or type(plan.seed) is not int
                or not isinstance(plan.library_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', plan.library_sha256)
                or not isinstance(plan.blocks, tuple) or len(plan.blocks) > 100000):
            raise ValueError('resolved plan does not match the video binding')
        index = 0
        for row in timeline.intervals:
            for start in range(row.start_ms, row.end_ms, 30000):
                if index >= len(plan.blocks):
                    raise ValueError('resolved plan has missing blocks')
                block = plan.blocks[index]
                if ((block.row_id, block.index, block.start_ms, block.end_ms,
                     block.a_target, block.b_target) !=
                        (row.row_id, index, start, min(start + 30000, row.end_ms),
                         row.a_target, row.b_target)):
                    raise ValueError('resolved plan has inconsistent blocks')
                for target, pattern in ((block.a_target, block.a_pattern),
                                        (block.b_target, block.b_pattern)):
                    if ((target == 0 and pattern is not None) or
                            (target > 0 and (not isinstance(pattern, str) or not pattern))):
                        raise ValueError('resolved plan has invalid waveforms')
                index += 1
        if index != len(plan.blocks):
            raise ValueError('resolved plan has extra blocks')
        plan_id = secrets.token_hex(16)
        record = {'version': 1, 'plan_id': plan_id, 'source': asdict(source),
                  'csv_sha256': timeline.sha256, 'seed': plan.seed,
                  'library_sha256': plan.library_sha256,
                  'blocks': [asdict(block) for block in plan.blocks]}
        self._files._write_atomically(plan_id + '.plan.json', record,
                                     maximum_size=_RECORD_LIMIT, create_only=True)
        return plan_id
