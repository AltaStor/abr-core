# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv_restore
from python.srv.plugins.hyperv.hyperv_restore import HyperVRestoreError

SHARE = r"\\server\share"
READ_LOST = "Error reading file, expected 1052430 bytes, got 0 bytes!; Failed to read from data file!; failed to load data range 804"


def ok() -> dict:
    return {"exit_code": 0, "result": {"status": "ok"}, "lines": []}


def failed(error: str) -> dict:
    return {"exit_code": 1, "result": {"status": "error", "error": error}, "lines": []}


class _Node:
    def __init__(self, *results: dict) -> None:
        self.results = list(results)
        self.commands: list[list[str]] = []
        self.removed: list[list[str]] = []

    def run_disk_tool(self, node_name, command, **_kwargs):
        self.commands.append(command)
        return self.results.pop(0)

    def remove_files(self, node_name, paths):
        self.removed.append(list(paths))
        return []


class RestoreShareLossTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notes: list[dict] = []
        for target, value in (
            ("_storage_opens", ""),
            ("env_int", 3),
            ("reconnect_seconds", 300),
        ):
            patcher = patch.object(hyperv_restore, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        invocation = patch.object(
            hyperv_restore.vhdx_tool, "storage_invocation", return_value=(["--path", r"\\server\share\job"], {"ABR_SMB_SHARE": SHARE})
        )
        invocation.start()
        self.addCleanup(invocation.stop)

    def _restore(self, node: _Node, layers: int = 1) -> None:
        runs = [{"abr_path": f"/repo/s{n}", "abr_backup_name": f"01-disk-{n}.vhdx"} for n in range(layers)]
        hyperv_restore._restore_disk_layers(
            node,
            "HV-01",
            runs,
            r"D:\Hyper-V\vm\disk.vhdx",
            repository={"type": "local", "name": "Repo"},
            encryption_key="",
            task_id="task-1",
            slot="SCSI0:0",
            vm_name="vm",
            label="Restoring disk 1/1",
            progress=12,
            span=70,
            timeout=600,
            progress_callback=self.notes.append,
        )

    def _messages(self) -> list[str]:
        return [str(note.get("message")) for note in self.notes]

    def test_a_disk_whose_share_dropped_is_restored_again_from_the_start(self) -> None:
        node = _Node(failed(READ_LOST), ok())

        self._restore(node)

        self.assertEqual(len(node.commands), 2)
        self.assertEqual(node.removed, [[r"D:\Hyper-V\vm\disk.vhdx"]])
        self.assertTrue(any(f"HV-01 lost the repository share {SHARE} while reading disk SCSI0:0" in m and "(attempt 2 of 3)" in m for m in self._messages()))

    def test_a_chain_starts_again_from_its_full_backup(self) -> None:
        node = _Node(ok(), failed(READ_LOST), ok(), ok())

        self._restore(node, layers=2)

        names = [command[command.index("--name") + 1] for command in node.commands]
        self.assertEqual(names, ["01-disk-0.vhdx", "01-disk-1.vhdx", "01-disk-0.vhdx", "01-disk-1.vhdx"])
        self.assertEqual(["--force" in command for command in node.commands], [False, True, False, True])

    def test_a_chain_read_in_one_run_is_restored_again_too(self) -> None:
        node = _Node(failed(READ_LOST), ok())
        runs = [{"abr_path": "/repo", "abr_backup_name": "01-disk-0.vhdx and 1 incremental(s) on it", "source_names": ["s0\\01-disk-0.vhdx", "s1\\01-disk-1.vhdx"]}]

        hyperv_restore._restore_disk_layers(
            node,
            "HV-01",
            runs,
            r"D:\Hyper-V\vm\disk.vhdx",
            repository={"type": "local", "name": "Repo"},
            encryption_key="",
            task_id="task-1",
            slot="SCSI0:0",
            vm_name="vm",
            label="Restoring disk 1/1",
            progress=12,
            span=70,
            timeout=600,
            progress_callback=self.notes.append,
        )

        self.assertEqual([command[command.index("--source-names") + 1] for command in node.commands], ["s0\\01-disk-0.vhdx,s1\\01-disk-1.vhdx"] * 2)
        self.assertEqual(node.removed, [[r"D:\Hyper-V\vm\disk.vhdx"]])
        self.assertTrue(any("while reading disk SCSI0:0 from 01-disk-0.vhdx and 1 incremental(s) on it" in m and "(attempt 2 of 3)" in m for m in self._messages()))

    def test_a_failure_that_is_not_the_share_ends_the_restore_at_once(self) -> None:
        node = _Node(failed("The encryption key does not match"))

        with self.assertRaises(HyperVRestoreError) as caught:
            self._restore(node)

        self.assertEqual(len(node.commands), 1)
        self.assertIn("The encryption key does not match", str(caught.exception))
        self.assertEqual(node.removed, [])

    def test_a_share_that_does_not_come_back_in_time_ends_the_restore_naming_the_wait(self) -> None:
        node = _Node(failed(READ_LOST))

        with patch.object(hyperv_restore, "_wait_for_storage", return_value=None), self.assertRaises(HyperVRestoreError) as caught:
            self._restore(node)

        self.assertIn(f"lost the repository share {SHARE}", str(caught.exception))
        self.assertIn("could not open it again within 300 s", str(caught.exception))

    def test_a_disk_that_fails_on_every_attempt_says_the_share_came_back_each_time(self) -> None:
        node = _Node(failed(READ_LOST), failed(READ_LOST), failed(READ_LOST))

        with self.assertRaises(HyperVRestoreError) as caught:
            self._restore(node)

        self.assertEqual(len(node.commands), 3)
        self.assertIn("could not be read on any of 3 attempts", str(caught.exception))
        self.assertIn("Error reading file", str(caught.exception))

    def test_a_lost_network_name_is_waited_out_too(self) -> None:
        node = _Node(failed("The specified network name is no longer available."), ok())

        self._restore(node)

        self.assertEqual(len(node.commands), 2)


if __name__ == "__main__":
    unittest.main()
