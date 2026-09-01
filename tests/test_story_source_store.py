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

    def test_duplicate_opaque_id_never_overwrites_first_source(self):
        first_id = "A" * 24
        second_id = "B" * 24
        other = StorySourceLoader(max_bytes=1024).load(
            "other.txt", b"WXYZ", encoding="utf-8"
        )

        with patch(
            "backend.story.source_store.secrets.token_urlsafe",
            side_effect=(first_id, first_id, second_id),
        ):
            first = self.store.store(self.story)
            second = self.store.store(other)

        self.assertEqual(first.source_id, first_id)
        self.assertEqual(second.source_id, second_id)
        self.assertEqual(first.path.read_bytes(), b"ABCD")
        self.assertEqual(second.path.read_bytes(), b"WXYZ")
        self.assertEqual(
            sorted(path.name for path in self.directory.iterdir()),
            [f"{first_id}.txt", f"{second_id}.txt"],
        )

    def test_delete_removes_only_the_committed_opaque_source(self):
        kept_story = StorySourceLoader(max_bytes=1024).load(
            "kept.txt", b"WXYZ", encoding="utf-8"
        )
        removed = self.store.store(self.story)
        kept = self.store.store(kept_story)
        delete = getattr(self.store, "delete", None)
        self.assertIsNotNone(delete, "pinned source deletion API is missing")

        delete(removed)

        self.assertFalse(removed.path.exists())
        self.assertEqual(kept.path.read_bytes(), b"WXYZ")

    def test_posix_commit_fails_on_existing_final_instead_of_replacing_it(self):
        with (
            patch("backend.story.source_store.os.name", "posix"),
            patch(
                "backend.story.source_store.os.link",
                side_effect=FileExistsError("occupied"),
            ) as link,
            patch(
                "backend.story.source_store.os.replace",
                return_value=None,
            ) as replace,
        ):
            with (
                patch(
                    "backend.story.source_store.os.supports_dir_fd",
                    {os.link, os.unlink, os.rename, os.replace},
                ),
                patch(
                    "backend.story.source_store.os.supports_follow_symlinks",
                    {os.link},
                ),
                ):
                    with self.assertRaises(FileExistsError):
                        self.store._replace_relative(123, ".temporary", "opaque.txt")
        directory_descriptor = self.store._root.handle.fileno()
        link.assert_called_once_with(
            ".temporary",
            "opaque.txt",
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        replace.assert_not_called()

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
