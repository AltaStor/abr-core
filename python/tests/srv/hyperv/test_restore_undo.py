# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import unittest
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv_restore


class FakeNode:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resolve_node(self, name):
        self.calls.append(("resolve_node", name))
        return "HV-NODE-02"

    def destroy_vm(self, vm_id):
        self.calls.append(("destroy_vm", vm_id))

    def replace_vm_disks(self, vm_id, disks):
        self.calls.append(("replace_vm_disks", vm_id, [disk["path"] for disk in disks]))

    def remove_files(self, node, paths):
        self.calls.append(("remove_files", node, list(paths)))
        return []

    def remove_empty_folders(self, node, folders):
        self.calls.append(("remove_empty_folders", node, list(folders)))
        return []


class RestoreUndoTests(unittest.TestCase):
    def _undo(self, **overrides) -> FakeNode:
        node = FakeNode()
        arguments = {
            "connection": {"host": "hv.example.test"},
            "node_name": "hv-node-02",
            "created_vm_id": None,
            "swapped": [],
            "written_files": [],
            "created_folders": [],
            "target_vm_id": "",
            "progress_callback": None,
        }
        arguments.update(overrides)
        with patch.object(hyperv_restore, "open_node", return_value=contextlib.nullcontext(node)):
            hyperv_restore._undo(**arguments)
        return node

    def test_undo_reaches_the_target_node_and_removes_what_the_restore_created(self) -> None:
        node = self._undo(
            written_files=[r"C:\Hyper-V\vm-r\Virtual Hard Disks\disk1.vhdx"],
            created_folders=[r"C:\Hyper-V\vm-r\Virtual Hard Disks", r"C:\Hyper-V\vm-r", r"C:\Hyper-V"],
        )

        self.assertEqual(
            node.calls,
            [
                ("resolve_node", "hv-node-02"),
                ("remove_files", "HV-NODE-02", [r"C:\Hyper-V\vm-r\Virtual Hard Disks\disk1.vhdx"]),
                (
                    "remove_empty_folders",
                    "HV-NODE-02",
                    [r"C:\Hyper-V\vm-r\Virtual Hard Disks", r"C:\Hyper-V\vm-r", r"C:\Hyper-V"],
                ),
            ],
        )

    def test_folders_go_deepest_first_whatever_order_they_were_created_in(self) -> None:
        node = self._undo(
            created_folders=[r"C:\Hyper-V\vm-r\a", r"C:\Hyper-V\vm-r", r"C:\Hyper-V\vm-r\b\c"],
        )

        self.assertEqual(
            node.calls[-1],
            ("remove_empty_folders", "HV-NODE-02", [r"C:\Hyper-V\vm-r\b\c", r"C:\Hyper-V\vm-r\a", r"C:\Hyper-V\vm-r"]),
        )

    def test_nothing_created_means_no_connection(self) -> None:
        with patch.object(hyperv_restore, "open_node") as open_node:
            hyperv_restore._undo(
                connection={},
                node_name="hv-node-02",
                created_vm_id=None,
                swapped=[],
                written_files=[],
                created_folders=[],
                target_vm_id="",
                progress_callback=None,
            )
        open_node.assert_not_called()


class RestoreUndoAfterStopTests(unittest.TestCase):
    def test_a_stop_request_does_not_cut_the_rollback_short(self) -> None:
        node = FakeNode()

        def stop(*args, **kwargs):
            raise hyperv_restore.TaskStopRequested("Session stop requested")

        with patch.object(hyperv_restore, "open_node", return_value=contextlib.nullcontext(node)), patch.object(
            hyperv_restore, "_emit", side_effect=stop
        ), patch.object(hyperv_restore, "_warn", side_effect=stop):
            hyperv_restore._undo(
                connection={},
                node_name="hv-node-02",
                created_vm_id=None,
                swapped=[
                    {
                        "controller_type": "SCSI",
                        "controller_number": 0,
                        "controller_location": 0,
                        "previous_path": r"C:\vms\vm\disk1.vhdx",
                    }
                ],
                written_files=[r"C:\vms\vm\disk1-restored.vhdx"],
                created_folders=[r"C:\vms\vm\new"],
                target_vm_id="vm-1",
                progress_callback=None,
            )

        self.assertEqual(
            [call[0] for call in node.calls],
            ["resolve_node", "replace_vm_disks", "remove_files", "remove_empty_folders"],
        )


if __name__ == "__main__":
    unittest.main()
