# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

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

FULL = {"type": "periodic", "every": "3650d"}
STORES = (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)


class SyntheticFullFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        registrable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        registrable.start()
        self.addCleanup(registrable.stop)
        for store in STORES:
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in STORES])
        REPOSITORIES["local"] = {"id": "local", "name": "Local", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["copies"] = {"id": "copies", "name": "Copies", "type": "local", "path": tempfile.mkdtemp()}
        self.inventory = abr_v41.create_inventory_record(
            {"name": "vCenter", "type": "vsphere", "address": "10.0.0.2", "username": "administrator", "password": "secret"}
        )
        RESOURCES_BY_HV[str(self.inventory["id"])] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "vc-01",
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }

    def _create(self, name: str, synthetic: dict | None) -> dict:
        payload = {"name": name, "vms": ["vc-01"], "inventory_id": self.inventory["id"], "repo_id": "local", "active_full_schedule": FULL}
        if synthetic is not None:
            payload["synthetic_full_schedule"] = synthetic
        return abr_v41.create_backup_job(payload)

    def _refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception

    def test_a_synthetic_full_more_often_than_daily_is_refused_and_not_made(self) -> None:
        for every in ("5m", "30m", "23h"):
            with self.subTest(every=every):
                error = self._refused(lambda every=every: self._create(f"syn-{every}", {"type": "periodic", "every": every}))

                self.assertEqual((error.status_code, error.code), (422, "invalid_schedule"))
                self.assertIn(f"every must be at least 1d, not {every}", error.message)
                self.assertNotIn("--", error.message)
        self.assertEqual(list(JOBS.values()), [])

    def test_daily_or_rarer_periodic_and_weekly_synthetic_fulls_are_taken(self) -> None:
        for name, synthetic in (
            ("syn-1d", {"type": "periodic", "every": "1d"}),
            ("syn-24h", {"type": "periodic", "every": "24h"}),
            ("syn-7d", {"type": "periodic", "every": "7d"}),
            ("syn-weekly", {"type": "weekly", "days": ["sunday"], "time": "03:00"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self._create(name, synthetic)["name"], name)

    def test_job_edit_refuses_a_shorter_interval_and_keeps_the_schedule(self) -> None:
        job = self._create("syn", {"type": "periodic", "every": "1d"})

        error = self._refused(lambda: abr_v41.edit_job_record(job["id"], {"synthetic_full_schedule": {"type": "periodic", "every": "30m"}}))

        self.assertEqual(error.status_code, 422)
        self.assertEqual(JOBS[job["id"]]["synthetic_full_schedule"]["every"], "1d")

    def test_the_active_full_and_the_regular_schedule_keep_their_own_rules(self) -> None:
        job = self._create("plain", None)

        edited = abr_v41.edit_job_record(
            job["id"], {"active_full_schedule": {"type": "periodic", "every": "12h"}, "schedule": {"type": "periodic", "every": "15m"}}
        )

        self.assertEqual((edited["active_full_schedule"]["every"], edited["schedule"]["every"]), ("12h", "15m"))

    def test_a_backup_copy_job_has_the_same_floor(self) -> None:
        self._create("Nightly", None)

        error = self._refused(
            lambda: abr_v41.create_backup_copy_job(
                {
                    "name": "Copy",
                    "source_jobs": ["Nightly"],
                    "repo": "Copies",
                    "active_full_schedule": FULL,
                    "synthetic_full_schedule": {"type": "periodic", "every": "5m"},
                }
            )
        )

        self.assertEqual(error.code, "invalid_schedule")


if __name__ == "__main__":
    unittest.main()
