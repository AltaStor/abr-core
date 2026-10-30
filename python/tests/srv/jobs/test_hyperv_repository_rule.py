# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

FULL = {"type": "weekly", "days": ["sunday"], "time": "02:00"}


class HyperVRepositoryRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        registrable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        registrable.start()
        self.addCleanup(registrable.stop)
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)])
        REPOSITORIES["local"] = {"id": "local", "name": "Local", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["default"] = {
            "id": "default",
            "name": "Default Repo",
            "type": "local",
            "path": tempfile.mkdtemp(),
            "metadata": {"default_repository": True, "support_hyperv": False},
        }
        REPOSITORIES["s3"] = {"id": "s3", "name": "Bucket", "type": "s3", "s3": {"bucket": "bucket", "region": "us-east-1"}}
        REPOSITORIES["smb"] = {
            "id": "smb",
            "name": "Share",
            "type": "smb",
            "path": tempfile.mkdtemp(),
            "metadata": {"server": "nas.example", "share": "backups"},
        }
        REPOSITORIES["smb-no-share"] = {"id": "smb-no-share", "name": "Half share", "type": "smb", "path": tempfile.mkdtemp()}
        self.hyperv = self._inventory("hyper-v", "Hyper-V", "10.0.0.1", "vm-hv", "hv-01")
        self.vcenter = self._inventory("vsphere", "vCenter", "10.0.0.2", "vm-vc", "vc-01")

    def _inventory(self, kind: str, name: str, address: str, vm_id: str, vm_name: str) -> dict:
        inventory = abr_v41.create_inventory_record(
            {"name": name, "type": kind, "address": address, "username": "administrator", "password": "secret"}
        )
        RESOURCES_BY_HV[str(inventory["id"])] = {
            ("virtual_machine", vm_id): {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": vm_name,
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        return inventory

    def _create(self, name: str, inventory: dict, vm: str, repo_id: str) -> dict:
        return abr_v41.create_backup_job(
            {"name": name, "vms": [vm], "inventory_id": inventory["id"], "repo_id": repo_id, "active_full_schedule": FULL}
        )

    def _refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception

    def test_a_hyperv_job_on_a_local_repository_is_refused_and_not_made(self) -> None:
        error = self._refused(lambda: self._create("hv-local", self.hyperv, "hv-01", "local"))

        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.code, "hyperv_repository_unreachable")
        self.assertIn("Hyper-V VMs need an S3 or SMB repository", error.message)
        self.assertIn("cannot reach Local, a folder of this appliance", error.message)
        self.assertEqual(JOBS, {})

    def test_an_smb_repository_without_its_share_is_refused_like_a_local_one(self) -> None:
        error = self._refused(lambda: self._create("hv-half", self.hyperv, "hv-01", "smb-no-share"))

        self.assertEqual(error.code, "hyperv_repository_unreachable")

    def test_the_default_repo_with_its_switch_off_points_at_the_switch(self) -> None:
        error = self._refused(lambda: self._create("hv-default", self.hyperv, "hv-01", "default"))

        self.assertIn("abr repo default --support-hyperv", error.message)

    def test_hyperv_jobs_on_s3_and_smb_repositories_are_made(self) -> None:
        for repo_id in ("s3", "smb"):
            with self.subTest(repo_id=repo_id):
                self.assertEqual(self._create(f"hv-{repo_id}", self.hyperv, "hv-01", repo_id)["name"], f"hv-{repo_id}")

    def test_a_vcenter_job_on_a_local_repository_is_still_made(self) -> None:
        self.assertEqual(self._create("vc-local", self.vcenter, "vc-01", "local")["name"], "vc-local")

    def test_a_hyperv_job_edited_onto_a_local_repository_is_refused_and_kept(self) -> None:
        job = self._create("hv-edit", self.hyperv, "hv-01", "smb")

        error = self._refused(lambda: abr_v41.edit_job_record(job["id"], {"repo_id": "local"}))

        self.assertEqual(error.code, "hyperv_repository_unreachable")
        self.assertEqual(JOBS[job["id"]]["repo_id"], "smb")

    def test_a_local_job_moved_to_a_hyperv_inventory_is_refused(self) -> None:
        job = self._create("vc-move", self.vcenter, "vc-01", "local")

        error = self._refused(
            lambda: abr_v41.edit_job_record(job["id"], {"inventory_id": self.hyperv["id"], "vms": ["hv-01"]})
        )

        self.assertEqual(error.code, "hyperv_repository_unreachable")
        self.assertEqual(JOBS[job["id"]]["inventory_id"], self.vcenter["id"])

    def test_a_hyperv_job_can_move_between_repositories_its_hosts_reach(self) -> None:
        job = self._create("hv-move", self.hyperv, "hv-01", "smb")

        abr_v41.edit_job_record(job["id"], {"repo_id": "s3"})

        self.assertEqual(JOBS[job["id"]]["repo_id"], "s3")

    def test_a_hyperv_synthetic_full_into_a_local_repository_is_refused(self) -> None:
        job = self._create("hv-synth", self.hyperv, "hv-01", "smb")

        error = self._refused(
            lambda: abr_v41.start_job_record(job["id"], force_synthetic=True, target_repository_id="local")
        )

        self.assertEqual(error.code, "hyperv_repository_unreachable")
        self.assertEqual(TASKS, {})

    def test_a_hyperv_synthetic_full_into_a_reachable_repository_passes_this_check(self) -> None:
        job = self._create("hv-synth-s3", self.hyperv, "hv-01", "smb")

        with self.assertRaises(ApiError) as caught:
            abr_v41.start_job_record(job["id"], force_synthetic=True, target_repository_id="s3")

        self.assertNotEqual(caught.exception.code, "hyperv_repository_unreachable")

    def test_a_vcenter_synthetic_full_into_a_local_repository_passes_this_check(self) -> None:
        job = self._create("vc-synth", self.vcenter, "vc-01", "smb")

        with self.assertRaises(ApiError) as caught:
            abr_v41.start_job_record(job["id"], force_synthetic=True, target_repository_id="local")

        self.assertNotEqual(caught.exception.code, "hyperv_repository_unreachable")

    def test_an_edit_that_leaves_the_repository_alone_is_not_checked(self) -> None:
        job = self._create("hv-old", self.hyperv, "hv-01", "smb")
        JOBS[job["id"]] = dict(JOBS[job["id"]], repo_id="local", repo_name="Local")

        edited = abr_v41.edit_job_record(job["id"], {"name": "hv-old-renamed", "retention_days": 9})

        self.assertEqual(edited["name"], "hv-old-renamed")


if __name__ == "__main__":
    unittest.main()
