# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
)
from python.tests.srv.jobs.test_replica_update import (
    JOB_ID,
    OUR_SNAPSHOT,
    _layer,
    _UpdateCase,
)

UPDATE = "python.srv.services.internal.replication.replica_update"
RESTORE = "python.srv.plugins.vcenter.vcenter_restore"


def _point(point_id: str, day: str, kind: str, parent: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": point_id,
        "job_id": "src",
        "vm_id": "vm-1",
        "status": "available",
        "repository_id": "repo",
        "created_at": f"2026-11-{day}T01:00:00+00:00",
        "execution_type": kind,
        "parent_point_id": parent,
        **extra,
    }


def _disks(change: str) -> list[dict[str, Any]]:
    return [{"index": 1, "change_id": f"{change}-1"}, {"index": 2, "change_id": f"{change}-2"}]


class _SyntheticCase(_UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        BACKUPS.clear()
        for point in (
            _point("rp-1", "01", "ActiveFull", None),
            _point("rp-2", "02", "Incremental", "rp-1"),
            _point("rp-3", "03", "Incremental", "rp-2"),
            _point("sf-4", "04", "SyntheticFull", None),
            _point("rp-5", "05", "Incremental", "sf-4"),
        ):
            BACKUPS[point["id"]] = point
        self.manifests: dict[str, dict[str, Any]] = {
            "rp-1": {"disks": _disks("c1")},
            "rp-2": {"disks": _disks("c2")},
            "rp-3": {"disks": _disks("c3")},
            "sf-4": {"synthetic_full": True, "merged_restore_point_ids": ["rp-1", "rp-2", "rp-3"], "disks": _disks("c3")},
            "rp-5": {"disks": _disks("c5")},
        }
        self.keys: dict[str, str] = {}
        for target, value in (
            (f"{RESTORE}._resolve_abr_restore_layers", self._layers),
            (f"{RESTORE}._load_manifest_for_point", lambda point: self.manifests[str(point["id"])]),
            (
                f"{RESTORE}._resolve_chain_disk_entry",
                lambda manifest, *, selected_disk, disk_index: next(
                    (disk for disk in manifest.get("disks") or [] if disk["index"] == disk_index), None
                ),
            ),
            ("python.srv.services.abr_v41._resolve_restore_encryption_key", lambda point: self.keys.get(str(point["id"]), "")),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _layers(self, *, selected_point, selected_disk, disk_index, repository) -> list[dict[str, Any]]:
        chain = []
        current: dict[str, Any] | None = selected_point
        while current is not None:
            chain.append(current)
            parent = current.get("parent_point_id")
            current = BACKUPS.get(parent) if parent else None
        layers = []
        for point in reversed(chain):
            if point["execution_type"] != "Incremental":
                layers = []
            layers.append(_layer(point["id"], "incremental" if point["execution_type"] == "Incremental" else "full"))
        return layers

    def _replica_at(self, point_id: str) -> None:
        JOBS[JOB_ID]["replica"]["point_id"] = point_id

    def _writes(self) -> list[list[str]]:
        return [call[2] for call in self.platform.calls if call[0] == "write"]

    def _assert_rebuild(self) -> None:
        with patch(f"{UPDATE}._update_mode_two", side_effect=ReplicationError("rebuild chosen")), self.assertRaisesRegex(
            ReplicationError, "rebuild chosen"
        ):
            self._run()
        self.assertEqual(self.platform.calls, [])


class SyntheticFullTests(_SyntheticCase):
    def test_a_replica_inside_the_merged_chain_takes_the_old_and_the_new_incrementals(self) -> None:
        self._replica_at("rp-2")

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(self._writes(), [["rp-3", "rp-5"], ["rp-3", "rp-5"]])
        self.assertEqual(self.platform.calls[0], ("snapshot", OUR_SNAPSHOT))
        self.assertEqual(self._replica()["point_id"], "rp-5")

    def test_a_replica_at_the_newest_merged_point_takes_only_what_came_after_the_synthetic_full(self) -> None:
        self._replica_at("rp-3")

        self._run()

        self.assertEqual(self._writes(), [["rp-5"], ["rp-5"]])

    def test_a_replica_at_the_full_takes_every_incremental_of_both_chains(self) -> None:
        self._replica_at("rp-1")

        self._run()

        self.assertEqual(self._writes(), [["rp-2", "rp-3", "rp-5"], ["rp-2", "rp-3", "rp-5"]])

    def test_a_synthetic_full_with_the_data_the_replica_already_has_only_moves_the_point(self) -> None:
        self._replica_at("rp-3")
        BACKUPS.pop("rp-5")

        result = self._run()

        self.assertEqual(result["mode"], "up_to_date")
        self.assertEqual(self.platform.calls, [])
        self.assertEqual(self._replica()["point_id"], "sf-4")
        self.assertEqual(self._replica()["point_created_at"], "2026-11-04T01:00:00+00:00")
        self.assertEqual(self._replica()["fingerprint"], "cv-1")

    def test_a_synthetic_full_merged_from_another_synthetic_full_chains_through_both(self) -> None:
        self._replica_at("rp-2")
        for point in (_point("sf-6", "06", "SyntheticFull", None), _point("rp-7", "07", "Incremental", "sf-6")):
            BACKUPS[point["id"]] = point
        self.manifests["sf-6"] = {"synthetic_full": True, "merged_restore_point_ids": ["sf-4", "rp-5"], "disks": _disks("c5")}
        self.manifests["rp-7"] = {"disks": _disks("c7")}

        self._run()

        self.assertEqual(self._writes(), [["rp-3", "rp-5", "rp-7"], ["rp-3", "rp-5", "rp-7"]])

    def test_cases_the_merged_chain_cannot_serve_go_to_the_rebuild(self) -> None:
        cases = {
            "an active full, not a synthetic one": lambda: self.manifests["sf-4"].pop("synthetic_full"),
            "a synthetic full that records no merged points": lambda: self.manifests["sf-4"].pop("merged_restore_point_ids"),
            "the newest merged point is being deleted": lambda: BACKUPS["rp-3"].__setitem__("status", "deleting"),
            "the newest merged point is gone": lambda: BACKUPS.pop("rp-3"),
            "the old chain lost a point": lambda: BACKUPS.pop("rp-2"),
            "the old chain is in another repository": lambda: BACKUPS["rp-3"].__setitem__("repository_id", "other"),
            "the old chain has another key": lambda: self.keys.__setitem__("rp-3", "old-key"),
            "a disk change id differs": lambda: self.manifests["sf-4"]["disks"][1].__setitem__("change_id", "other"),
            "a disk has no change id": lambda: [disk.pop("change_id") for disk in self.manifests["sf-4"]["disks"]],
            "the replica is not in the merged chain": lambda: self._replica_at("rp-0"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                self._replica_at("rp-2")
                change()
                self._assert_rebuild()

    def test_a_deleting_point_in_the_middle_of_the_old_tail_goes_to_the_rebuild(self) -> None:
        self._replica_at("rp-1")
        BACKUPS["rp-2"]["status"] = "deleting"

        self._assert_rebuild()

    def test_an_unchanged_chain_without_a_synthetic_full_is_planned_as_before(self) -> None:
        self._replica_at("rp-1")
        BACKUPS.pop("sf-4")
        BACKUPS.pop("rp-5")

        self._run()

        self.assertEqual(self._writes(), [["rp-2", "rp-3"], ["rp-2", "rp-3"]])


if __name__ == "__main__":
    unittest.main()
