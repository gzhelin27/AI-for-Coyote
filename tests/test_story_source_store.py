from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.story.source import StorySourceLoader
from backend.story.source_store import PinnedStorySourceStore, StorySourceStorageError


class PinnedStorySourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "data" / "stories"
        self.store = PinnedStorySourceStore(
            self.directory, project_root=self.root
        )
        self.addCleanup(self.store.close)
        self.story = StorySourceLoader(max_bytes=1024).load(
            "private-name.txt", b"ABCD", encoding="utf-8"
        )

    def test_success_writes_only_one_opaque_final_file(self):
        stored = self.store.store(self.story)

        self.assertRegex(stored.source_id, r"^[A-Za-z0-9_-]{20,}$")
        self.assertNotIn("private-name", stored.source_id)
        self.assertEqual(stored.path.read_bytes(), b"ABCD")
        self.assertEqual(
            [path.name for path in self.directory.iterdir()],
            [f"{stored.source_id}.txt"],
        )
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.directory.iterdir()))

    def test_root_replacement_cannot_redirect_a_write(self):
        moved = self.root / "moved-stories"
        outside = self.root / "outside"
        outside.mkdir()
        try:
            self.directory.rename(moved)
        except PermissionError:
            # Windows pins the directory without FILE_SHARE_DELETE, so the
            # replacement itself is rejected by the kernel.
            stored = self.store.store(self.story)
            self.assertEqual(stored.path.parent, self.directory)
            self.assertEqual(list(outside.iterdir()), [])
            return
        try:
            os.symlink(outside, self.directory, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory replacement links are unavailable: {exc}")

        with self.assertRaises(StorySourceStorageError):
            self.store.store(self.story)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_file_fsync_failure_leaves_no_temporary_or_final_file(self):
        with patch("backend.story.source_store.os.fsync", side_effect=OSError("crash")):
            with self.assertRaises(StorySourceStorageError):
                self.store.store(self.story)

        self.assertEqual(list(self.directory.iterdir()), [])

    def test_post_rename_directory_fsync_failure_removes_final_file(self):
        with patch.object(
            self.store, "_fsync_directory", side_effect=OSError("dir-crash")
        ):
            with self.assertRaises(StorySourceStorageError):
                self.store.store(self.story)

        self.assertEqual(list(self.directory.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows handle-relative cleanup")
    def test_windows_failure_cleanup_does_not_reopen_an_absolute_path(self):
        with (
            patch.object(
                self.store, "_fsync_directory", side_effect=OSError("dir-crash")
            ),
            patch.object(
                Path,
                "unlink",
                side_effect=AssertionError("absolute cleanup path was reopened"),
            ),
        ):
            with self.assertRaises(StorySourceStorageError):
                self.store.store(self.story)

        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
