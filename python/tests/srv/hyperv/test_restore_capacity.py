# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import ClassVar

from python.srv.plugins.hyperv.hyperv import HyperVNode
from python.srv.plugins.hyperv.hyperv_restore import (
    restored_bytes,
    source_allocated_bytes,
)
from python.srv.services.synthetic_full import build_disk_merges

GIB = 1024**3


def _layers(*logical: int) -> list[dict]:
    return [{"logical_bytes": value} for value in logical]


class RestoredBytesTests(unittest.TestCase):
    def test_the_space_the_disk_took_at_the_backup_counts_however_often_its_blocks_were_rewritten(self) -> None:
        disk = {"capacity_bytes": 16 * GIB, "allocated_bytes": int(2.7 * GIB)}

        self.assertEqual(restored_bytes(disk, _layers(2 * GIB, 16 * GIB, 1 * GIB, 1 * GIB)), int(2.7 * GIB))

    def test_a_point_from_before_the_size_was_recorded_never_counts_more_than_the_disk_holds(self) -> None:
        disk = {"capacity_bytes": 16 * GIB}

        self.assertEqual(restored_bytes(disk, _layers(2 * GIB, 16 * GIB, 1 * GIB)), 16 * GIB)

    def test_a_short_chain_from_before_the_size_was_recorded_counts_its_data(self) -> None:
        disk = {"capacity_bytes": 16 * GIB}

        self.assertEqual(restored_bytes(disk, _layers(2 * GIB, 30 * 1024**2)), 2 * GIB + 30 * 1024**2)

    def test_a_point_from_before_the_size_was_recorded_takes_what_the_source_disk_takes_now(self) -> None:
        disk = {"capacity_bytes": 16 * GIB}

        self.assertEqual(restored_bytes(disk, _layers(2 * GIB, 16 * GIB, 1 * GIB), int(3.2 * GIB)), int(3.2 * GIB))

    def test_the_source_disk_counts_even_when_its_file_is_larger_than_the_data_it_holds(self) -> None:
        disk = {"capacity_bytes": 10 * GIB}

        self.assertEqual(restored_bytes(disk, _layers(int(1.5 * GIB)), int(2.8 * GIB)), int(2.8 * GIB))

    def test_the_recorded_size_wins_over_the_source_disk_now(self) -> None:
        disk = {"capacity_bytes": 16 * GIB, "allocated_bytes": 2 * GIB}

        self.assertEqual(restored_bytes(disk, _layers(2 * GIB), 9 * GIB), 2 * GIB)

    def test_without_any_size_the_capacity_is_taken(self) -> None:
        self.assertEqual(restored_bytes({"capacity_bytes": 8 * GIB}, _layers(0)), 8 * GIB)
        self.assertEqual(restored_bytes({"capacity_bytes": 8 * GIB, "allocated_bytes": 0}, []), 8 * GIB)


class SourceAllocatedBytesTests(unittest.TestCase):
    LIVE: ClassVar[list[dict]] = [
        {"slot": "SCSI0:0", "uuid": "AAAA-1", "allocated_bytes": 3 * GIB},
        {"slot": "SCSI0:1", "uuid": "BBBB-2", "allocated_bytes": 1 * GIB},
    ]

    def test_the_same_disk_is_found_by_its_identifier_wherever_it_sits(self) -> None:
        self.assertEqual(source_allocated_bytes({"slot": "SCSI0:5", "uuid": "bbbb-2"}, self.LIVE), 1 * GIB)

    def test_a_disk_that_was_replaced_since_gives_no_bound(self) -> None:
        self.assertEqual(source_allocated_bytes({"slot": "SCSI0:0", "uuid": "CCCC-3"}, self.LIVE), 0)

    def test_without_identifiers_the_slot_decides(self) -> None:
        self.assertEqual(source_allocated_bytes({"slot": "SCSI0:0"}, [{"slot": "SCSI0:0", "allocated_bytes": 5}]), 5)

    def test_no_source_vm_gives_no_bound(self) -> None:
        self.assertEqual(source_allocated_bytes({"slot": "SCSI0:0", "uuid": "AAAA-1"}, []), 0)


class AllocatedBytesRecordedTests(unittest.TestCase):
    def test_a_backed_up_disk_records_what_its_files_take_on_the_host(self) -> None:
        row = {
            "slot": "SCSI0:0",
            "supported": True,
            "path": r"C:\vm\disk_1A2B.avhdx",
            "chain": [
                {"path": r"C:\vm\disk_1A2B.avhdx", "type": "Differencing", "size": 16 * GIB, "file_size": 4 * 1024**2},
                {"path": r"C:\vm\disk.vhdx", "type": "Dynamic", "size": 16 * GIB, "file_size": 3 * GIB},
            ],
        }

        entry = HyperVNode._disk_entry(HyperVNode.__new__(HyperVNode), row)

        self.assertEqual(entry["allocated_bytes"], 3 * GIB + 4 * 1024**2)
        self.assertEqual(entry["capacity_bytes"], 16 * GIB)

    def test_a_merged_or_copied_point_takes_the_size_of_the_newest_point(self) -> None:
        def point(folder: str, allocated: int | None) -> dict:
            disk = {"slot": "SCSI0:0", "abr_path": f"/repo/vm/{folder}", "abr_backup_name": "01-disk.vhdx"}
            if allocated is not None:
                disk["allocated_bytes"] = allocated
            return {"artifact": {"disks": [disk]}}

        merges = build_disk_merges([point("full", 2 * GIB), point("inc-1", None), point("inc-2", 3 * GIB)])

        self.assertEqual(merges[0]["template"]["allocated_bytes"], 3 * GIB)


if __name__ == "__main__":
    unittest.main()
