import hashlib
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


class ReplayStoreTests(unittest.TestCase):
    def test_round_trip_preserves_cycle_gap_tenths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(Path(tmp))
            bundle = make_replay_bundle(gap_tenths=[0, 7, 20], status="completed")
            store.save(bundle.manifest, bundle.timeline)
            self.assertEqual(
                store.load(bundle.manifest.replay_id).timeline.cycles,
                bundle.timeline.cycles,
            )

    def test_rejects_path_unsafe_archive_before_reading_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "evil.coyote-replay")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaises(ReplayStoreError):
                ReplayStore(Path(tmp)).load("evil")

    def test_incomplete_session_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ReplayStoreError, "completed"):
                bundle = make_replay_bundle(gap_tenths=[], status="paused")
                ReplayStore(Path(tmp)).save(bundle.manifest, bundle.timeline)

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


if __name__ == "__main__":
    unittest.main()
