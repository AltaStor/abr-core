# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool


class DurableWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_a_durable_write_replaces_the_file_whole_and_flushes_it_and_its_folder(self) -> None:
        target = self.root / "manifest.json"
        target.write_text("old", encoding="utf-8")
        synced: list[int] = []
        real_fsync = os.fsync

        def record(descriptor: int) -> None:
            synced.append(descriptor)
            real_fsync(descriptor)

        with patch.object(storage_tool.os, "fsync", side_effect=record):
            storage_tool.write_file_durably(target, '{"ok": true}')

        self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["manifest.json"])
        self.assertEqual(len(synced), 2)

    def test_a_write_that_fails_leaves_the_old_file_in_place(self) -> None:
        target = self.root / "manifest.json"
        target.write_text("old", encoding="utf-8")

        with patch.object(storage_tool.os, "fsync", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(OSError):
                storage_tool.write_file_durably(target, "new")

        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_every_file_and_folder_of_a_session_and_the_folders_above_it_are_flushed(self) -> None:
        repository = self.root / "repo"
        session = repository / "job-1" / "vm-1" / "task-1"
        session.mkdir(parents=True)
        for name in ("01-vm.vmdk.abrd", "01-vm.vmdk.abri", "manifest.abrm", "manifest.json"):
            (session / name).write_bytes(b"data")
        synced: list[Path] = []

        with patch.object(storage_tool, "_fsync_path", side_effect=synced.append):
            storage_tool.make_durable(session, up_to=repository)

        self.assertEqual(
            {path.name for path in synced if path.is_file()},
            {"01-vm.vmdk.abrd", "01-vm.vmdk.abri", "manifest.abrm", "manifest.json"},
        )
        folders = [path.resolve() for path in synced if path.is_dir()]
        self.assertEqual(
            folders,
            [session.resolve(), (repository / "job-1" / "vm-1").resolve(), (repository / "job-1").resolve(), repository.resolve()],
        )

    def test_nothing_above_the_repository_root_is_touched(self) -> None:
        repository = self.root / "repo"
        session = repository / "vm" / "task-1"
        session.mkdir(parents=True)
        synced: list[Path] = []

        with patch.object(storage_tool, "_fsync_path", side_effect=synced.append):
            storage_tool.make_durable(session, up_to=repository)

        self.assertNotIn(self.root.resolve(), [path.resolve() for path in synced])

    def test_a_missing_session_folder_is_left_alone(self) -> None:
        with patch.object(storage_tool, "_fsync_path") as synced:
            storage_tool.make_durable(self.root / "gone", up_to=self.root)
        synced.assert_not_called()


if __name__ == "__main__":
    unittest.main()
