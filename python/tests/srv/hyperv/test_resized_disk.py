# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.plugins.hyperv.hyperv_backup import _change_ids

GIB = 1 << 30


def _disk(slot: str, index: int, capacity: int) -> dict:
    return {"slot": slot, "datastore_path": f"C:\\VMs\\disk-{index}.vhdx", "uuid": f"uuid-{index}", "capacity_bytes": capacity}


def _parent(*entries: tuple[int, str, int | None]) -> dict:
    disks = []
    for index, change_id, capacity in entries:
        entry = {"index": index, "uuid": f"uuid-{index}", "datastore_path": f"C:\\VMs\\disk-{index}.vhdx", "change_id": change_id}
        if capacity is not None:
            entry["capacity_bytes"] = capacity
        disks.append(entry)
    return {"disks": disks}


class ResizedDiskTests(unittest.TestCase):
    def test_a_disk_of_the_same_size_chains_on_its_change_id(self) -> None:
        found, missing, resized = _change_ids([_disk("SCSI0:0", 1, GIB)], _parent((1, "rct-1", GIB)))

        self.assertEqual((found, missing, resized), ({"SCSI0:0": "rct-1"}, [], []))

    def test_a_grown_disk_does_not_chain_and_is_named_with_both_sizes(self) -> None:
        disks = [_disk("SCSI0:0", 1, 2 * GIB), _disk("SCSI0:1", 2, GIB)]

        found, missing, resized = _change_ids(disks, _parent((1, "rct-1", GIB), (2, "rct-2", GIB)))

        self.assertEqual(found, {"SCSI0:1": "rct-2"})
        self.assertEqual(missing, [])
        self.assertEqual(resized, [("SCSI0:0", GIB, 2 * GIB)])

    def test_a_shrunk_disk_does_not_chain_either(self) -> None:
        _found, _missing, resized = _change_ids([_disk("SCSI0:0", 1, GIB)], _parent((1, "rct-1", 2 * GIB)))

        self.assertEqual(resized, [("SCSI0:0", 2 * GIB, GIB)])

    def test_a_point_that_recorded_no_size_still_chains(self) -> None:
        found, _missing, resized = _change_ids([_disk("SCSI0:0", 1, GIB)], _parent((1, "rct-1", None)))

        self.assertEqual((found, resized), ({"SCSI0:0": "rct-1"}, []))

    def test_a_disk_without_a_change_id_is_still_missing(self) -> None:
        found, missing, resized = _change_ids([_disk("SCSI0:0", 1, GIB)], _parent((1, "", GIB)))

        self.assertEqual((found, missing, resized), ({}, ["SCSI0:0"], []))


if __name__ == "__main__":
    unittest.main()
