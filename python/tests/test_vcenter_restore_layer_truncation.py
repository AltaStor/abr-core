# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from python.srv.core.mongo_db_wrapper import BACKUPS
from python.srv.plugins.vcenter.vcenter_restore import _resolve_abr_restore_layers


class ResolveAbrRestoreLayersCapacityChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        BACKUPS.clear()

    def _write_manifest(self, name: str, *, capacity_gb: int, store_mode: str | None) -> str:
        disk_entry = {
            "index": 1,
            "uuid": "disk-1",
            "datastore_path": "[ds] vm/vm.vmdk",
            "capacity_gb": capacity_gb,
            "abr_path": f"/repo/{name}",
            "abr_backup_name": "01-vm.vmdk",
        }
        if store_mode is not None:
            disk_entry["store_mode"] = store_mode
        path = pathlib.Path(self._tmp.name) / f"{name}.json"
        path.write_text(json.dumps({"disks": [disk_entry]}), encoding="utf-8")
        return str(path)

    def _register_point(
        self,
        point_id: str,
        *,
        parent_point_id: str | None,
        manifest_path: str,
    ) -> None:
        BACKUPS[point_id] = {
            "id": point_id,
            "parent_point_id": parent_point_id,
            "artifact": {"manifest_path": manifest_path},
        }

    def test_a_disk_re_baselined_after_a_capacity_change_discards_stale_ancestors(self) -> None:
        # full(16GB) -> incremental(18GB, broken) -> full-capacity-changed(18GB)
        full_manifest = self._write_manifest("full", capacity_gb=16, store_mode=None)
        inc_manifest = self._write_manifest("inc", capacity_gb=18, store_mode="incremental")
        rebaseline_manifest = self._write_manifest(
            "rebaseline", capacity_gb=18, store_mode="full-capacity-changed"
        )

        self._register_point("full-1", parent_point_id=None, manifest_path=full_manifest)
        self._register_point("inc-1", parent_point_id="full-1", manifest_path=inc_manifest)
        self._register_point("rebaseline-1", parent_point_id="inc-1", manifest_path=rebaseline_manifest)

        selected_point = BACKUPS["rebaseline-1"]
        selected_disk = {"uuid": "disk-1", "datastore_path": "[ds] vm/vm.vmdk"}

        layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk=selected_disk,
            disk_index=1,
            repository={"type": "local"},
        )

        self.assertEqual([layer["point_id"] for layer in layers], ["rebaseline-1"])

    def test_a_forced_full_still_wins_even_though_the_catalog_kept_its_parent_pointer(self) -> None:
        full_manifest = self._write_manifest("full", capacity_gb=16, store_mode=None)
        inc_manifest = self._write_manifest("inc", capacity_gb=18, store_mode="incremental")
        forced_full_manifest = self._write_manifest("forced-full", capacity_gb=18, store_mode=None)

        self._register_point("full-1", parent_point_id=None, manifest_path=full_manifest)
        self._register_point("inc-1", parent_point_id="full-1", manifest_path=inc_manifest)
        self._register_point("forced-full-1", parent_point_id="inc-1", manifest_path=forced_full_manifest)

        selected_point = BACKUPS["forced-full-1"]
        selected_disk = {"uuid": "disk-1", "datastore_path": "[ds] vm/vm.vmdk"}

        layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk=selected_disk,
            disk_index=1,
            repository={"type": "local"},
        )

        self.assertEqual([layer["point_id"] for layer in layers], ["forced-full-1"])

    def test_the_truncation_also_applies_to_an_s3_repository(self) -> None:
        full_manifest = self._write_manifest("full", capacity_gb=16, store_mode=None)
        inc_manifest = self._write_manifest("inc", capacity_gb=18, store_mode="incremental")
        rebaseline_manifest = self._write_manifest(
            "rebaseline", capacity_gb=18, store_mode="full-capacity-changed"
        )

        self._register_point("full-1", parent_point_id=None, manifest_path=full_manifest)
        self._register_point("inc-1", parent_point_id="full-1", manifest_path=inc_manifest)
        self._register_point("rebaseline-1", parent_point_id="inc-1", manifest_path=rebaseline_manifest)

        selected_point = BACKUPS["rebaseline-1"]
        selected_disk = {"uuid": "disk-1", "datastore_path": "[ds] vm/vm.vmdk"}

        layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk=selected_disk,
            disk_index=1,
            repository={"type": "s3", "s3": {"bucket": "altastor-backups"}},
        )

        self.assertEqual([layer["point_id"] for layer in layers], ["rebaseline-1"])
        self.assertEqual(layers[0]["source"], "s3://altastor-backups/repo/rebaseline/01-vm.vmdk")

    def _write_manifest_multi(self, name: str, *, disks: list[dict[str, object]]) -> str:
        path = pathlib.Path(self._tmp.name) / f"{name}.json"
        path.write_text(json.dumps({"disks": disks}), encoding="utf-8")
        return str(path)

    def test_restoring_a_disk_added_partway_through_the_chain_skips_older_points_that_predate_it(self) -> None:
        def disk_entry(index: int, name: str, store_mode: str | None) -> dict[str, object]:
            return {
                "index": index,
                "uuid": f"disk-{index}",
                "datastore_path": f"[ds] vm/vm-{index}.vmdk",
                "capacity_gb": 16,
                "abr_path": f"/repo/{name}",
                "abr_backup_name": f"0{index}-vm.vmdk",
                **({"store_mode": store_mode} if store_mode else {}),
            }

        full_manifest = self._write_manifest_multi(
            "full", disks=[disk_entry(1, "full", None), disk_entry(2, "full", None)]
        )
        inc_manifest = self._write_manifest_multi(
            "inc", disks=[disk_entry(1, "inc", "incremental"), disk_entry(2, "inc", "incremental")]
        )
        disk_added_manifest = self._write_manifest_multi(
            "disk-added",
            disks=[
                disk_entry(1, "disk-added", "incremental"),
                disk_entry(2, "disk-added", "incremental"),
                disk_entry(3, "disk-added", "full-new-disk"),
            ],
        )
        inc2_manifest = self._write_manifest_multi(
            "inc2",
            disks=[
                disk_entry(1, "inc2", "incremental"),
                disk_entry(2, "inc2", "incremental"),
                disk_entry(3, "inc2", "incremental"),
            ],
        )

        self._register_point("full-1", parent_point_id=None, manifest_path=full_manifest)
        self._register_point("inc-1", parent_point_id="full-1", manifest_path=inc_manifest)
        self._register_point("disk-added-1", parent_point_id="inc-1", manifest_path=disk_added_manifest)
        self._register_point("inc-2", parent_point_id="disk-added-1", manifest_path=inc2_manifest)

        selected_point = BACKUPS["inc-2"]

        new_disk_layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk={"uuid": "disk-3", "datastore_path": "[ds] vm/vm-3.vmdk"},
            disk_index=3,
            repository={"type": "local"},
        )
        self.assertEqual([layer["point_id"] for layer in new_disk_layers], ["disk-added-1", "inc-2"])

        original_disk_layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk={"uuid": "disk-1", "datastore_path": "[ds] vm/vm-1.vmdk"},
            disk_index=1,
            repository={"type": "local"},
        )
        self.assertEqual(
            [layer["point_id"] for layer in original_disk_layers],
            ["full-1", "inc-1", "disk-added-1", "inc-2"],
        )

    def test_an_ordinary_chain_with_no_geometry_change_is_untouched(self) -> None:
        full_manifest = self._write_manifest("full", capacity_gb=16, store_mode=None)
        inc1_manifest = self._write_manifest("inc1", capacity_gb=16, store_mode="incremental")
        inc2_manifest = self._write_manifest("inc2", capacity_gb=16, store_mode="incremental")

        self._register_point("full-1", parent_point_id=None, manifest_path=full_manifest)
        self._register_point("inc-1", parent_point_id="full-1", manifest_path=inc1_manifest)
        self._register_point("inc-2", parent_point_id="inc-1", manifest_path=inc2_manifest)

        selected_point = BACKUPS["inc-2"]
        selected_disk = {"uuid": "disk-1", "datastore_path": "[ds] vm/vm.vmdk"}

        layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk=selected_disk,
            disk_index=1,
            repository={"type": "local"},
        )

        self.assertEqual([layer["point_id"] for layer in layers], ["full-1", "inc-1", "inc-2"])


if __name__ == "__main__":
    unittest.main()
