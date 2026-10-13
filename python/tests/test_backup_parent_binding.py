# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from python.srv.core import task_pool as tp_mod
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.services import abr_v41, backup_catalog

VM_ID = "vm-1"
REPO_ID = "repo-1"


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "job_type": "backup",
        "repo_id": REPO_ID,
        "retention_days": 7,
        "active_full_days": [],
        "synthetic_full_days": [],
    }


def _point(point_id: str, job_id: str, created_at: str, *, full: bool, parent: str | None = None) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": job_id,
            "vm_id": VM_ID,
            "vm_name": "vm",
            "repository_id": REPO_ID,
            "status": "available",
            "full_backup": full,
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent,
            "created_at": created_at,
            "origin": {"task_id": f"task-{point_id}", "job_id": job_id, "created_at": created_at},
            "artifact": {"manifest_path": f"/srv/backups/vm/task-{point_id}/manifest.json"},
        }
    )


def _task(job_id: str, execution_type: str, **extra: object) -> dict:
    return {
        "id": "task-now",
        "job_id": job_id,
        "task_type": "backup",
        "resource_type": "virtual_machine",
        "resource_external_id": VM_ID,
        "status": "running",
        "parameters": {"resolved_backup_type": execution_type, "incremental": execution_type == "Incremental", **extra},
        "result": {},
        "logs": [],
    }


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        JOBS["job-x"] = _job("job-x")
        JOBS["job-y"] = _job("job-y")
        BACKUPS["x1"] = _point("x1", "job-x", "2026-10-09T10:00:00+00:00", full=True)
        BACKUPS["y1"] = _point("y1", "job-y", "2026-10-09T11:00:00+00:00", full=True)
        persist = patch.object(abr_v41, "persist_task")
        persist.start()
        self.addCleanup(persist.stop)


class BindBackupParentTests(_Catalog):
    def test_an_incremental_is_bound_to_its_own_jobs_last_point_not_the_vms_newest(self) -> None:
        task = abr_v41.bind_backup_parent(_task("job-x", "Incremental"))

        parameters = task["parameters"]
        self.assertEqual(parameters["parent_point_id"], "x1")
        self.assertEqual(parameters["parent_task_id"], "task-x1")
        self.assertEqual(parameters["parent_manifest_hint"], "/srv/backups/vm/task-x1/manifest.json")
        self.assertIn("restore point x1", task["logs"][-1]["message"])

    def test_a_parent_manifest_missing_on_disk_is_fetched_back_before_the_backup_starts(self) -> None:
        with patch.object(backup_catalog, "_load_backup_manifest_payload", return_value={}) as hydrate:
            abr_v41.bind_backup_parent(_task("job-x", "Incremental"))

        hydrate.assert_called_once()
        point, artifact = hydrate.call_args.args
        self.assertEqual(point["id"], "x1")
        self.assertEqual(artifact["manifest_path"], "/srv/backups/vm/task-x1/manifest.json")

    def test_the_binding_follows_the_jobs_chain_as_it_grows(self) -> None:
        BACKUPS["x2"] = _point("x2", "job-x", "2026-10-09T12:00:00+00:00", full=False, parent="x1")
        BACKUPS["y2"] = _point("y2", "job-y", "2026-10-09T13:00:00+00:00", full=False, parent="y1")

        self.assertEqual(abr_v41.bind_backup_parent(_task("job-x", "Incremental"))["parameters"]["parent_point_id"], "x2")
        self.assertEqual(abr_v41.bind_backup_parent(_task("job-y", "Incremental"))["parameters"]["parent_point_id"], "y2")

    def test_a_full_carries_no_parent_even_when_it_was_queued_as_an_incremental(self) -> None:
        queued = _task("job-x", "ActiveFull", parent_point_id="x1", parent_task_id="task-x1", parent_manifest_hint="/old")

        parameters = abr_v41.bind_backup_parent(queued)["parameters"]

        self.assertEqual(
            {key: parameters[key] for key in ("parent_point_id", "parent_task_id", "parent_manifest_hint")},
            {"parent_point_id": None, "parent_task_id": None, "parent_manifest_hint": None},
        )

    def test_copy_jobs_and_tasks_without_a_job_are_left_alone(self) -> None:
        for task in (_task("job-x", "Incremental", copy_job=True), _task("", "Incremental"), {**_task("job-x", "Incremental"), "task_type": "restore"}):
            with self.subTest(task=task["parameters"]):
                self.assertNotIn("parent_task_id", abr_v41.bind_backup_parent(task)["parameters"])


class PoolBindingTests(_Catalog):
    def test_a_failing_binding_leaves_the_incremental_unable_to_guess_a_parent(self) -> None:
        task = _task("job-x", "Incremental")
        with patch("python.srv.services.abr_v41.bind_backup_parent", side_effect=RuntimeError("catalog down")), patch(
            "python.srv.services.abr_v41.refresh_queued_backup_decision", side_effect=lambda claimed: claimed
        ), self.assertLogs(tp_mod.logger, level="ERROR"):
            claimed = tp_mod._refresh_backup_decision(task)

        self.assertIn("parent_task_id", claimed["parameters"])
        self.assertIsNone(claimed["parameters"]["parent_task_id"])


class CatalogParentTests(_Catalog):
    def _completed(self, job_id: str, **result: object) -> dict:
        return {
            "id": "task-new",
            "task_type": "backup",
            "status": "completed",
            "hypervisor_id": "",
            "resource_type": "virtual_machine",
            "resource_external_id": VM_ID,
            "job_id": job_id,
            "updated_at": "2026-10-09T14:00:00+00:00",
            "result": {
                "vm_name": "vm",
                "vm_external_id": VM_ID,
                "repository_id": REPO_ID,
                "backup_mode": "incremental",
                "resolved_backup_type": "Incremental",
                "resolved_backup_reason": "StandardIncremental",
                "finished_at": "2026-10-09T14:00:00+00:00",
                **result,
            },
        }

    def test_the_catalog_records_the_point_the_backup_continued_from(self) -> None:
        BACKUPS["x2"] = _point("x2", "job-x", "2026-10-09T12:00:00+00:00", full=False, parent="x1")

        record = backup_catalog.register_backup_from_completed_task(self._completed("job-x", parent_point_id="x1"))

        self.assertEqual(record["parent_point_id"], "x1")

    def test_without_a_reported_base_the_jobs_last_point_is_the_parent(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(self._completed("job-x"))

        self.assertEqual(record["parent_point_id"], "x1")

    def test_a_reported_base_from_another_job_is_not_taken(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(self._completed("job-x", parent_point_id="y1"))

        self.assertEqual(record["parent_point_id"], "x1")


class ChainBaseMismatchTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._root = pathlib.Path(tempfile.mkdtemp(prefix="abr-chain-base-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._root, ignore_errors=True))
        self._write(BACKUPS["x1"], [{"index": 1, "uuid": "d1", "change_id": "cid/10"}, {"index": 2, "uuid": "d2", "change_id": "cid/11"}])
        self._write(BACKUPS["y1"], [{"index": 1, "uuid": "d1", "change_id": "cid/12"}, {"index": 2, "uuid": "d2", "change_id": "cid/13"}])

    def _write(self, point: dict, disks: list[dict]) -> None:
        path = self._root / f"{point['id']}.json"
        path.write_text(json.dumps({"operation": "vm-backup", "disks": disks}), encoding="utf-8")
        point["artifact"] = {**point.get("artifact", {}), "manifest_path": str(path)}

    def _incremental(self, point_id: str, parent: str, created_at: str, bases: list[str]) -> None:
        BACKUPS[point_id] = _point(point_id, "job-x", created_at, full=False, parent=parent)
        self._write(
            BACKUPS[point_id],
            [{"index": index, "uuid": f"d{index}", "previous_change_id": base, "change_id": f"cid/{20 + index}"} for index, base in enumerate(bases, start=1)],
        )

    def test_an_incremental_taken_on_another_jobs_base_is_found(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])

        self.assertEqual(abr_v41._chain_base_mismatches("job-x", VM_ID, REPO_ID), ["x2"])

    def test_a_chain_on_its_own_bases_is_clean(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/10", "cid/11"])

        self.assertEqual(abr_v41._chain_base_mismatches("job-x", VM_ID, REPO_ID), [])

    def test_a_disk_read_in_full_or_a_manifest_that_cannot_be_read_is_not_a_mismatch(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["*", "cid/11"])
        BACKUPS["x3"] = _point("x3", "job-x", "2026-10-09T13:00:00+00:00", full=False, parent="x2")
        BACKUPS["x3"]["artifact"] = {"manifest_path": str(self._root / "gone.json")}

        self.assertEqual(abr_v41._chain_base_mismatches("job-x", VM_ID, REPO_ID), [])

    def test_a_base_the_parent_recorded_as_its_next_change_id_is_the_parents_own(self) -> None:
        self._write(BACKUPS["x1"], [{"index": 1, "uuid": "d1", "change_id": "", "next_change_id": "cid/10"}, {"index": 2, "uuid": "d2", "change_id": "cid/11"}])
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/10", "cid/11"])

        self.assertEqual(abr_v41._chain_base_mismatches("job-x", VM_ID, REPO_ID), [])

    def test_a_new_full_leaves_the_damaged_segment_behind(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])
        BACKUPS["x3"] = _point("x3", "job-x", "2026-10-09T13:00:00+00:00", full=True)

        self.assertEqual(abr_v41._chain_base_mismatches("job-x", VM_ID, REPO_ID), [])

    def test_the_next_run_of_a_damaged_chain_is_an_active_full(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])

        self.assertEqual(
            abr_v41.resolve_job_execution(JOBS["job-x"], vm_id=VM_ID, force_full=False, scheduled_type="Incremental"),
            ("ActiveFull", "ChainBaseMismatch"),
        )
        self.assertEqual(
            abr_v41.resolve_job_execution(JOBS["job-x"], vm_id=VM_ID, force_full=False, scheduled_type="SyntheticFull"),
            ("ActiveFull", "ChainBaseMismatch"),
        )
        self.assertEqual(
            abr_v41.resolve_job_execution(JOBS["job-y"], vm_id=VM_ID, force_full=False, scheduled_type="Incremental"),
            ("Incremental", "StandardIncremental"),
        )

    def test_a_queued_incremental_becomes_an_active_full_at_start_and_warns_once(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])
        task = _task("job-x", "Incremental", resolved_backup_reason="StandardIncremental")

        with self.assertLogs(abr_v41.LOGGER, level="WARNING") as logs:
            task = abr_v41.refresh_queued_backup_decision(task)
            task = abr_v41.refresh_queued_backup_decision(task)

        self.assertEqual(
            (task["parameters"]["resolved_backup_type"], task["parameters"]["resolved_backup_reason"]),
            ("ActiveFull", "ChainBaseMismatch"),
        )
        self.assertFalse(task["parameters"]["incremental"])
        self.assertEqual([item["restore_point_ids"] for item in task["warnings"]], [["x2"]])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("restore points x2", logs.output[0])
        self.assertIn("restore points x2", task["logs"][0]["message"])

    def test_a_run_queued_as_a_chain_repair_still_names_the_damaged_points_when_it_starts(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])
        task = _task("job-x", "ActiveFull", resolved_backup_reason="ChainBaseMismatch")

        with self.assertLogs(abr_v41.LOGGER, level="WARNING"):
            task = abr_v41.refresh_queued_backup_decision(task)

        self.assertEqual(task["warnings"][0]["restore_point_ids"], ["x2"])

    def test_a_healthy_chain_starts_without_a_warning(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/10", "cid/11"])
        task = abr_v41.refresh_queued_backup_decision(_task("job-x", "Incremental", resolved_backup_reason="StandardIncremental"))

        self.assertEqual(task["parameters"]["resolved_backup_type"], "Incremental")
        self.assertNotIn("warnings", task)

    def test_a_manual_synthetic_full_of_a_damaged_chain_is_refused(self) -> None:
        self._incremental("x2", "x1", "2026-10-09T12:00:00+00:00", ["cid/12", "cid/13"])
        for point_id in ("x1", "x2"):
            BACKUPS[point_id]["artifact"]["disks"] = [{"index": 1, "abr_path": "/srv/backups/vm", "abr_backup_name": "01.vmdk"}]
        JOBS["job-x"] = {**JOBS["job-x"], "vm_ids": [VM_ID], "vm_names": ["vm"]}

        with patch("python.srv.services.synthetic_full.validate_chain"), self.assertRaises(abr_v41.ApiError) as ctx:
            abr_v41._ensure_chain_can_be_merged(JOBS["job-x"])

        self.assertEqual(ctx.exception.code, "chain_cannot_be_merged")
        self.assertIn("restore points x2", ctx.exception.message)
        self.assertIn("abr job start --full", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
