# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import threading
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS
from python.srv.plugins.hyperv import hyperv_restore
from python.srv.plugins.hyperv.hyperv import HyperVNode
from python.srv.plugins.hyperv.hyperv_restore import (
    later_allocated_bytes,
    restored_bytes,
    restored_estimate,
    source_allocated_bytes,
    source_vm_disks,
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


class RestoredEstimateTests(unittest.TestCase):
    def test_the_sources_of_the_size_come_in_order_and_say_what_they_are(self) -> None:
        layers = _layers(2 * GIB, 16 * GIB)
        disk = {"capacity_bytes": 16 * GIB}

        self.assertEqual(
            restored_estimate({**disk, "allocated_bytes": 3 * GIB}, layers, later_allocated=4 * GIB, source_allocated=5 * GIB),
            (3 * GIB, "what the disk took when this backup was made"),
        )
        self.assertEqual(
            restored_estimate(disk, layers, later_allocated=4 * GIB, source_allocated=5 * GIB),
            (4 * GIB, "what the disk took at a later backup of it"),
        )
        self.assertEqual(
            restored_estimate(disk, layers, source_allocated=5 * GIB), (5 * GIB, "what the source VM's disk takes now")
        )
        self.assertEqual(
            restored_estimate(disk, layers), (16 * GIB, "the data of the chain's backups, at most the disk's capacity")
        )


class LaterAllocatedBytesTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        self.addCleanup(BACKUPS.clear)

    def _point(self, point_id: str, created_at: str, vm_id: str = "vm-1", **disk: object) -> dict:
        BACKUPS[point_id] = {"id": point_id, "vm_id": vm_id, "created_at": created_at, "artifact": {"disks": [disk]}}
        return BACKUPS[point_id]

    def test_the_nearest_later_backup_of_the_same_disk_gives_the_size(self) -> None:
        old = self._point("p1", "2026-10-01T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1")
        self._point("p0", "2026-09-01T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1", allocated_bytes=1 * GIB)
        self._point("p3", "2026-11-03T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1", allocated_bytes=4 * GIB)
        self._point("p2", "2026-11-01T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1", allocated_bytes=3 * GIB)
        self._point("x1", "2026-11-02T00:00:00Z", vm_id="vm-2", slot="SCSI0:0", uuid="AAAA-1", allocated_bytes=9 * GIB)

        self.assertEqual(later_allocated_bytes(old, {"slot": "SCSI0:0", "uuid": "aaaa-1"}), 3 * GIB)

    def test_a_disk_replaced_since_or_no_later_size_gives_no_bound(self) -> None:
        old = self._point("p1", "2026-10-01T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1")
        self._point("p2", "2026-11-01T00:00:00Z", slot="SCSI0:0", uuid="BBBB-2", allocated_bytes=3 * GIB)
        self._point("p3", "2026-11-02T00:00:00Z", slot="SCSI0:0", uuid="AAAA-1")

        self.assertEqual(later_allocated_bytes(old, {"slot": "SCSI0:0", "uuid": "AAAA-1"}), 0)
        self.assertEqual(later_allocated_bytes(None, {"slot": "SCSI0:0"}), 0)


class SourceVmDisksTests(unittest.TestCase):
    LIVE: ClassVar[list[dict]] = [{"slot": "SCSI0:0", "uuid": "AAAA-1", "allocated_bytes": 3 * GIB}]

    def setUp(self) -> None:
        HYPERVISORS.clear()
        self.addCleanup(HYPERVISORS.clear)
        HYPERVISORS["inv-src"] = {"id": "inv-src", "name": "source", "kind": "hyper-v", "metadata": {"host": "hv-a"}}
        HYPERVISORS["inv-vc"] = {"id": "inv-vc", "name": "vcenter", "kind": "vcenter", "metadata": {"host": "vc"}}

    def _target(self) -> object:
        class _Target:
            asked: ClassVar[list] = []

            def get_vm_disks(self, vm_id):
                self.asked.append(vm_id)
                raise hyperv_restore.HyperVError(f"VM {vm_id} is not on this host")

        return _Target()

    def test_the_same_inventory_is_asked_through_the_restores_own_connection(self) -> None:
        target = self._target()

        self.assertEqual(source_vm_disks({"inventory_id": "inv-tgt"}, "vm-1", "inv-tgt", target), [])
        self.assertEqual(target.asked, ["vm-1"])

    def test_another_inventory_is_asked_through_its_own_connection(self) -> None:
        source = SimpleNamespace(get_vm_disks=lambda vm_id: list(self.LIVE))
        with patch.object(hyperv_restore, "connection_settings", return_value={"host": "hv-a"}) as settings, patch.object(
            hyperv_restore, "open_node", return_value=contextlib.nullcontext(source)
        ):
            disks = source_vm_disks({"inventory_id": "inv-src"}, "vm-1", "inv-tgt", self._target())

        self.assertEqual(disks, self.LIVE)
        self.assertEqual(settings.call_args.args[0]["id"], "inv-src")

    def test_an_unreachable_slow_or_other_kind_of_source_gives_nothing(self) -> None:
        def unreachable(connection):
            raise hyperv_restore.HyperVError("did not answer")

        release = threading.Event()
        self.addCleanup(release.set)

        def slow(connection):
            release.wait(10)
            return contextlib.nullcontext(SimpleNamespace(get_vm_disks=lambda vm_id: list(self.LIVE)))

        with patch.object(hyperv_restore, "connection_settings", return_value={"host": "hv-a"}), patch.dict(
            "os.environ", {"ALTASTOR_HYPERV_SOURCE_SIZE_SECONDS": "1"}
        ):
            with patch.object(hyperv_restore, "open_node", side_effect=unreachable), self.assertLogs(hyperv_restore.logger, "INFO"):
                self.assertEqual(source_vm_disks({"inventory_id": "inv-src"}, "vm-1", "inv-tgt", self._target()), [])
            with patch.object(hyperv_restore, "open_node", side_effect=slow):
                self.assertEqual(source_vm_disks({"inventory_id": "inv-src"}, "vm-1", "inv-tgt", self._target()), [])
        self.assertEqual(source_vm_disks({"inventory_id": "inv-vc"}, "vm-1", "inv-tgt", self._target()), [])
