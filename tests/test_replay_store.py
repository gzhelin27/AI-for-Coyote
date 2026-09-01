import hashlib
import io
import json
import tempfile
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from backend.timeline.replay_store import ReplayStore, ReplayStoreError
from tests.timeline_fakes import make_replay_bundle


def rewrite_members(path: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        members = [(info.filename, archive.read(info)) for info in archive.infolist()]
    temporary = path.with_suffix(".rewrite")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, replacements.get(name, payload))
    temporary.replace(path)


def rewrite_member_metadata(
    path: Path,
    member_name: str,
    *,
    encrypted: bool = False,
    compression: int | None = None,
    compressed_size: int | None = None,
) -> None:
    data = bytearray(path.read_bytes())
    layouts = (
        (b"PK\x03\x04", 6, 8, 18, 26, 30),
        (b"PK\x01\x02", 8, 10, 20, 28, 46),
    )
    matches = 0
    for signature, flags_at, method_at, size_at, name_length_at, name_at in layouts:
        offset = 0
        while (offset := data.find(signature, offset)) >= 0:
            name_length = int.from_bytes(
                data[offset + name_length_at : offset + name_length_at + 2],
                "little",
            )
            name = bytes(data[offset + name_at : offset + name_at + name_length])
            if name.decode("utf-8") == member_name:
                if encrypted:
                    flags = int.from_bytes(
                        data[offset + flags_at : offset + flags_at + 2],
                        "little",
                    )
                    data[offset + flags_at : offset + flags_at + 2] = (
                        flags | 1
                    ).to_bytes(2, "little")
                if compression is not None:
                    data[offset + method_at : offset + method_at + 2] = (
                        compression.to_bytes(2, "little")
                    )
                if compressed_size is not None:
                    data[offset + size_at : offset + size_at + 4] = (
                        compressed_size.to_bytes(4, "little")
                    )
                matches += 1
            offset += len(signature)
    if matches != 2:
        raise AssertionError(f"expected local and central metadata for {member_name}")
    path.write_bytes(data)


class ReplayStoreTests(unittest.TestCase):
    @staticmethod
    def novel_metadata() -> dict[str, str]:
        return {
            "analysis_version": "faithful-offline-v1",
            "chapter_id": "chapter-1",
            "content_type": "novel",
            "dlc_version": "dlc-v1",
            "source_encoding": "utf-8",
            "source_text_hash": "a" * 64,
            "speed": "standard",
        }

    def test_save_writes_zip_through_open_exclusive_temp_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            real_zip_file = zipfile.ZipFile

            def require_open_file_object(target, *args, **kwargs):
                if kwargs.get("mode", args[0] if args else "r") == "w":
                    if not hasattr(target, "write") or target.closed:
                        raise AssertionError("ZIP writer reopened the temporary path")
                return real_zip_file(target, *args, **kwargs)

            with patch(
                "backend.timeline.replay_store.zipfile.ZipFile",
                side_effect=require_open_file_object,
            ):
                path = store.save(bundle.manifest, bundle.timeline)

            self.assertEqual(store.load(bundle.manifest.replay_id).timeline, bundle.timeline)
            self.assertEqual(path, Path(tmp, "replay-1.coyote-replay"))

    def test_round_trip_preserves_cycle_gap_tenths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[0, 7, 20], status="completed")
            store.save(bundle.manifest, bundle.timeline)
            self.assertEqual(
                store.load(bundle.manifest.replay_id).timeline.cycles,
                bundle.timeline.cycles,
            )

    def test_open_validated_rejects_identity_swap_between_stat_and_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            archive_path = store.save(bundle.manifest, bundle.timeline)
            replacement = Path(tmp, "replacement.tmp")
            replacement.write_bytes(b"replacement archive bytes")
            real_path_open = Path.open
            swapped = False

            def swap_before_open(path, *args, **kwargs):
                nonlocal swapped
                if Path(path) == archive_path and not swapped:
                    swapped = True
                    archive_path.unlink()
                    replacement.replace(archive_path)
                return real_path_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=swap_before_open):
                with self.assertRaisesRegex(ReplayStoreError, "changed"):
                    store.open_validated(bundle.manifest.replay_id)

            self.assertTrue(swapped)

    def test_open_validated_rejects_handle_redirected_outside_replay_root(self):
        with (
            tempfile.TemporaryDirectory() as replay_tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            store = ReplayStore(Path(replay_tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            archive_path = store.save(bundle.manifest, bundle.timeline)
            outside_path = Path(outside_tmp, archive_path.name)
            outside_path.write_bytes(archive_path.read_bytes())
            real_path_stat = Path.stat
            real_path_open = Path.open

            def redirected_stat(path, *args, **kwargs):
                if Path(path) == archive_path:
                    return real_path_stat(outside_path, *args, **kwargs)
                return real_path_stat(path, *args, **kwargs)

            def redirected_open(path, *args, **kwargs):
                if Path(path) == archive_path:
                    return real_path_open(outside_path, *args, **kwargs)
                return real_path_open(path, *args, **kwargs)

            with (
                patch.object(Path, "stat", new=redirected_stat),
                patch.object(Path, "open", new=redirected_open),
                self.assertRaisesRegex(ReplayStoreError, "unsafe"),
            ):
                store.open_validated(bundle.manifest.replay_id)

    def test_read_validated_returns_the_same_bytes_it_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            archive_path = store.save(bundle.manifest, bundle.timeline)
            original_bytes = archive_path.read_bytes()
            unvalidated_bytes = b"UNVALIDATED_AFTER_SCHEMA_CHECK"
            source = io.BytesIO(original_bytes)
            load_opened = store._load_opened

            def validate_then_mutate(replay_id, archive_file):
                result = load_opened(replay_id, archive_file)
                if archive_file is source:
                    source.seek(0)
                    source.truncate()
                    source.write(unvalidated_bytes)
                    source.seek(0)
                return result

            with (
                patch.object(store, "_open_contained", return_value=source),
                patch.object(
                    store,
                    "_load_opened",
                    side_effect=validate_then_mutate,
                ),
            ):
                downloaded_bytes = store.read_validated(
                    bundle.manifest.replay_id
                )

            self.assertEqual(downloaded_bytes, original_bytes)
            self.assertNotEqual(downloaded_bytes, unvalidated_bytes)
            self.assertTrue(source.closed)

    def test_read_validated_accumulates_short_reads_until_eof(self):
        class ArtificialShortReader:
            def __init__(self, payload: bytes) -> None:
                self.buffer = io.BytesIO(payload)
                self.validation_mode = False
                self.short_read_calls = 0

            def read(self, size: int = -1) -> bytes:
                if not self.validation_mode:
                    self.short_read_calls += 1
                    size = 7 if size < 0 else min(size, 7)
                return self.buffer.read(size)

            def __getattr__(self, name):
                return getattr(self.buffer, name)

        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            archive_path = store.save(bundle.manifest, bundle.timeline)
            original_bytes = archive_path.read_bytes()
            source = ArtificialShortReader(original_bytes)
            load_opened = store._load_opened

            def validate_with_regular_reads(replay_id, archive_file):
                if archive_file is source:
                    source.validation_mode = True
                    try:
                        return load_opened(replay_id, archive_file)
                    finally:
                        source.validation_mode = False
                return load_opened(replay_id, archive_file)

            with (
                patch.object(store, "_open_contained", return_value=source),
                patch.object(
                    store,
                    "_load_opened",
                    side_effect=validate_with_regular_reads,
                ),
            ):
                downloaded_bytes = store.read_validated(
                    bundle.manifest.replay_id
                )

            self.assertEqual(downloaded_bytes, original_bytes)
            self.assertGreater(source.short_read_calls, 1)
            self.assertTrue(source.closed)

    def test_rejects_path_unsafe_archive_before_reading_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "evil.coyote-replay")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with (
                patch.object(
                    zipfile.ZipFile,
                    "read",
                    side_effect=AssertionError("unsafe member payload was read"),
                ),
                self.assertRaises(ReplayStoreError),
            ):
                ReplayStore(Path(tmp)).load("evil")

    def test_incomplete_session_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ReplayStoreError, "completed"):
                bundle = make_replay_bundle(gap_tenths=[], status="paused")
                ReplayStore(root).save(bundle.manifest, bundle.timeline)
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_unexpected_archive_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("notes.txt", "not allowed")

            with self.assertRaisesRegex(ReplayStoreError, "unexpected"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_duplicate_archive_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "a") as archive:
                    archive.writestr("timeline.json", b"{}")

            with self.assertRaisesRegex(ReplayStoreError, "duplicate"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_timeline_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with zipfile.ZipFile(path, "r") as archive:
                timeline = json.loads(archive.read("timeline.json"))
            timeline["cycles"][0]["gap_tenths"] = 8
            rewrite_members(
                path,
                {"timeline.json": json.dumps(timeline).encode("utf-8")},
            )

            with self.assertRaisesRegex(ReplayStoreError, "checksum"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_oversized_uncompressed_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "oversized.coyote-replay")
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("manifest.json", b"{}")
                archive.writestr("timeline.json", b"x" * (16 * 1024 * 1024 + 1))

            with self.assertRaisesRegex(ReplayStoreError, "size"):
                ReplayStore(Path(tmp)).load("oversized")

    def test_round_trip_preserves_optional_scenes_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            scenes = {"schema_version": 1, "scenes": [{"scene_id": "sc-1"}]}

            store.save(
                bundle.manifest,
                bundle.timeline,
                scenes=scenes,
                source=b"# Original story",
                source_extension="md",
            )
            loaded = store.load(bundle.manifest.replay_id)

            self.assertEqual(loaded.scenes, scenes)
            self.assertEqual(loaded.source, b"# Original story")
            self.assertEqual(loaded.source_extension, "md")
            self.assertEqual(
                set(loaded.manifest.checksums or {}),
                {"timeline.json", "scenes.json", "source.md"},
            )
            self.assertIsNotNone(loaded.manifest.source_hash)

    def test_novel_archive_requires_source_scenes_and_exact_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            manifest = replace(
                bundle.manifest,
                mode="novel",
                dlc_version="dlc-v1",
                metadata=self.novel_metadata(),
            )
            scenes = {"schema_version": 1, "source_hash": "a" * 64, "chapters": []}

            for missing in ("source", "scenes"):
                kwargs = {
                    "scenes": scenes,
                    "source": b"original",
                    "source_extension": "txt",
                }
                if missing == "source":
                    kwargs.update(source=None, source_extension=None)
                else:
                    kwargs["scenes"] = None
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    ReplayStoreError, "novel"
                ):
                    store.save(manifest, bundle.timeline, **kwargs)

            path = store.save(
                manifest,
                bundle.timeline,
                scenes=scenes,
                source=b"original",
                source_extension="txt",
            )
            loaded = store.load(manifest.replay_id)

            self.assertTrue(path.is_file())
            self.assertEqual(loaded.manifest.metadata, self.novel_metadata())
            self.assertEqual(
                set(loaded.manifest.checksums or {}),
                {"timeline.json", "scenes.json", "source.txt"},
            )

    def test_rejects_novel_claim_with_incomplete_or_mismatched_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            scenes = {"schema_version": 1, "source_hash": "a" * 64, "chapters": []}
            invalid_values = (
                {key: value for key, value in self.novel_metadata().items() if key != "speed"},
                {**self.novel_metadata(), "speed": "warp"},
                {**self.novel_metadata(), "dlc_version": "other-dlc"},
            )

            for metadata in invalid_values:
                manifest = replace(
                    bundle.manifest,
                    mode="novel",
                    dlc_version="dlc-v1",
                    metadata=metadata,
                )
                with self.subTest(metadata=metadata), self.assertRaisesRegex(
                    ReplayStoreError, "metadata"
                ):
                    store.save(
                        manifest,
                        bundle.timeline,
                        scenes=scenes,
                        source=b"original",
                        source_extension="txt",
                    )

    def test_rejects_conflicting_duplicate_cycle_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            first = bundle.timeline.cycles[0]
            conflicting = replace(first, pattern="渐变")
            timeline = replace(bundle.timeline, cycles=(first, conflicting))

            with self.assertRaisesRegex(ReplayStoreError, "duplicate cycle"):
                store.save(bundle.manifest, timeline)

    def test_list_returns_summaries_and_delete_removes_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            store.save(bundle.manifest, bundle.timeline)
            second_manifest = replace(bundle.manifest, replay_id="replay-2")
            store.save(second_manifest, bundle.timeline)

            summaries = store.list()
            self.assertEqual(
                [summary.replay_id for summary in summaries],
                ["replay-1", "replay-2"],
            )
            self.assertTrue(
                all(summary.status.value == "completed" for summary in summaries)
            )

            store.delete("replay-1")
            self.assertEqual(
                [summary.replay_id for summary in store.list()],
                ["replay-2"],
            )
            with self.assertRaises(ReplayStoreError):
                store.load("replay-1")

    def test_list_derives_safe_title_and_cycle_count_from_validated_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[0, 7, 20], status="completed")
            manifest = replace(
                bundle.manifest,
                dlc_role="测试角色",
                dlc_profile="测试档",
            )
            store.save(manifest, bundle.timeline)

            summary = store.list()[0]

            self.assertEqual(summary.title, "测试角色 · 测试档")
            self.assertEqual(summary.cycle_count, 3)

    def test_list_uses_deterministic_fallback_title_for_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            store.save(bundle.manifest, bundle.timeline)

            summary = store.list()[0]

            self.assertEqual(summary.title, "回放 replay-1")
            self.assertEqual(summary.cycle_count, 0)

    def test_failed_atomic_replace_leaves_store_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ReplayStore(root)
            original = make_replay_bundle(gap_tenths=[0], status="completed")
            replacement = make_replay_bundle(gap_tenths=[20], status="completed")
            store.save(original.manifest, original.timeline)

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(ReplayStoreError):
                    store.save(replacement.manifest, replacement.timeline)

            self.assertEqual(
                store.load(original.manifest.replay_id).timeline.cycles,
                original.timeline.cycles,
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_rejects_malformed_replay_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            invalid_ids = ("", "../escape", "has.dot", "with/slash", "é", "x" * 129)
            for replay_id in invalid_ids:
                with self.subTest(replay_id=replay_id), self.assertRaises(
                    ReplayStoreError
                ):
                    store.load(replay_id)

    def test_rejects_unsupported_manifest_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with zipfile.ZipFile(path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            manifest["schema_version"] = 999
            rewrite_members(
                path,
                {"manifest.json": json.dumps(manifest).encode("utf-8")},
            )

            with self.assertRaisesRegex(ReplayStoreError, "schema"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_disallowed_source_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")

            with self.assertRaisesRegex(ReplayStoreError, "extension"):
                store.save(
                    bundle.manifest,
                    bundle.timeline,
                    source=b"payload",
                    source_extension="exe",
                )

    def test_rejects_multiple_source_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("source.txt", b"one")
                archive.writestr("source.md", b"two")

            with self.assertRaisesRegex(ReplayStoreError, "multiple source"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_duplicate_cycle_records_when_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[7], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with zipfile.ZipFile(path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                timeline = json.loads(archive.read("timeline.json"))
            duplicate = dict(timeline["cycles"][0])
            duplicate["pattern"] = "渐变"
            timeline["cycles"].append(duplicate)
            timeline_bytes = json.dumps(timeline).encode("utf-8")
            manifest["checksums"]["timeline.json"] = hashlib.sha256(
                timeline_bytes
            ).hexdigest()
            rewrite_members(
                path,
                {
                    "manifest.json": json.dumps(manifest).encode("utf-8"),
                    "timeline.json": timeline_bytes,
                },
            )

            with self.assertRaisesRegex(ReplayStoreError, "duplicate cycle"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_encrypted_allowed_member_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            rewrite_member_metadata(path, "timeline.json", encrypted=True)

            with self.assertRaisesRegex(ReplayStoreError, "encrypted"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_unsupported_compression_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            rewrite_member_metadata(path, "timeline.json", compression=99)

            with self.assertRaisesRegex(ReplayStoreError, "compression"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_unreasonable_compressed_member_size_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            rewrite_member_metadata(
                path,
                "timeline.json",
                compressed_size=18 * 1024 * 1024,
            )

            with self.assertRaisesRegex(ReplayStoreError, "compressed size"):
                store.load(bundle.manifest.replay_id)

    def test_rejects_unreasonable_outer_archive_size_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            path = store.save(bundle.manifest, bundle.timeline)
            with path.open("r+b") as archive_file:
                archive_file.truncate(50 * 1024 * 1024 + 1)

            with self.assertRaisesRegex(ReplayStoreError, "archive size"):
                store.load(bundle.manifest.replay_id)

    def test_normalizes_zip_read_runtime_and_unsupported_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[], status="completed")
            store.save(bundle.manifest, bundle.timeline)

            for error in (
                RuntimeError("encrypted payload"),
                NotImplementedError("unsupported compression"),
            ):
                with self.subTest(error=type(error).__name__), patch.object(
                    zipfile.ZipFile,
                    "read",
                    side_effect=error,
                ), self.assertRaises(ReplayStoreError):
                    store.load(bundle.manifest.replay_id)


if __name__ == "__main__":
    unittest.main()
