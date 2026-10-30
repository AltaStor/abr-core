# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

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

SURROUNDING_WHITESPACE_NAMES = [
    " aqajob",
    "aqajob ",
    "  aqajob  ",
    "\taqajob",
    "aqajob\t",
    "\naqajob",
    "aqajob\n",
    "\xa0aqajob",
    "aqajob\xa0",
]
BLANK_NAMES = ["", "   ", "\t\n"]
ACCEPTED_NAMES = ["aqajob", "aqa job", "a.b_c-d 1", "9start"]


class NameWhitespaceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        registrable = patch(
            "python.srv.services.abr_v41.assert_inventory_registrable", return_value=None
        )
        registrable.start()
        self.addCleanup(registrable.stop)
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "Repo One",
            "type": "smb",
            "metadata": {"server": "nas.example", "share": "backups"},
            "path": tempfile.mkdtemp(),
        }
        self.inventory = abr_v41.create_inventory_record(
            {
                "name": "vcenter-main",
                "type": "hyper-v",
                "address": "192.168.10.10",
                "username": "administrator",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[str(self.inventory["id"])] = {
            ("virtual_machine", "vm-101"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-101",
                "name": "web-01",
                "payload": {"path": "/Production/web-01"},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        self._address = iter(f"10.0.0.{octet}" for octet in range(1, 250))

    def _create_inventory(self, name: str) -> dict[str, object]:
        return abr_v41.create_inventory_record(
            {
                "name": name,
                "type": "hyper-v",
                "address": next(self._address),
                "username": "administrator",
                "password": "secret",
            }
        )

    def _create_repository(self, name: str) -> dict[str, object]:
        return abr_v41.create_repository_record(
            {"name": name, "type": "local", "path": tempfile.mkdtemp()}
        )

    def _create_job(self, name: str) -> dict[str, object]:
        return abr_v41.create_backup_job(
            {
                "name": name,
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            }
        )

    def _creators(self) -> dict[str, tuple[object, str]]:
        return {
            "inventory": (self._create_inventory, "invalid_inventory"),
            "repository": (self._create_repository, "invalid_repository"),
            "job": (self._create_job, "invalid_job"),
        }

    def test_surrounding_whitespace_names_are_rejected_on_create(self) -> None:
        for kind, (create, code) in self._creators().items():
            for name in SURROUNDING_WHITESPACE_NAMES:
                with self.subTest(kind=kind, name=name):
                    with self.assertRaises(ApiError) as ctx:
                        create(name)
                    self.assertEqual(ctx.exception.code, code)
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_rejected_name_creates_no_record(self) -> None:
        for kind, (create, _code) in self._creators().items():
            with self.subTest(kind=kind):
                before = (len(HYPERVISORS), len(REPOSITORIES), len(JOBS))
                with self.assertRaises(ApiError):
                    create(" aqajob")
                self.assertEqual(
                    (len(HYPERVISORS), len(REPOSITORIES), len(JOBS)), before
                )

    def test_blank_names_are_still_reported_as_missing(self) -> None:
        for kind, (create, code) in self._creators().items():
            for name in BLANK_NAMES:
                with self.subTest(kind=kind, name=name):
                    with self.assertRaises(ApiError) as ctx:
                        create(name)
                    self.assertEqual(ctx.exception.code, code)
                    self.assertEqual(ctx.exception.status_code, 422)

    def test_accepted_names_are_stored_verbatim(self) -> None:
        for kind, (create, _code) in self._creators().items():
            for index, name in enumerate(ACCEPTED_NAMES):
                unique = f"{name}{index}"
                with self.subTest(kind=kind, name=unique):
                    record = create(unique)
                    self.assertEqual(record["name"], unique)

    def test_copy_job_rejects_surrounding_whitespace(self) -> None:
        source = self._create_job("source-job")
        for name in SURROUNDING_WHITESPACE_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(ApiError) as ctx:
                    abr_v41.create_copy_job(
                        {
                            "name": name,
                            "source_job_id": source["id"],
                            "repo_id": "repo-1",
                            "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                        }
                    )
                self.assertEqual(ctx.exception.code, "invalid_job")
                self.assertEqual(ctx.exception.status_code, 400)

    def test_rename_rejects_surrounding_whitespace(self) -> None:
        inventory = self._create_inventory("inventory-base")
        repository = self._create_repository("repository-base")
        job = self._create_job("job-base")
        editors = {
            "inventory": (
                lambda name: abr_v41.edit_inventory_record(
                    inventory["id"], {"name": name}
                ),
                "invalid_inventory",
            ),
            "repository": (
                lambda name: abr_v41.edit_repository_record(
                    repository["id"], {"name": name}
                ),
                "invalid_repository",
            ),
            "job": (
                lambda name: abr_v41.edit_job_record(job["id"], {"name": name}),
                "invalid_job",
            ),
        }
        for kind, (edit, code) in editors.items():
            for name in SURROUNDING_WHITESPACE_NAMES:
                with self.subTest(kind=kind, name=name):
                    with self.assertRaises(ApiError) as ctx:
                        edit(name)
                    self.assertEqual(ctx.exception.code, code)
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_rename_to_a_valid_name_still_works(self) -> None:
        inventory = self._create_inventory("inventory-base")
        repository = self._create_repository("repository-base")
        job = self._create_job("job-base")
        self.assertEqual(
            abr_v41.edit_inventory_record(inventory["id"], {"name": "inventory next"})[
                "name"
            ],
            "inventory next",
        )
        self.assertEqual(
            abr_v41.edit_repository_record(
                repository["id"], {"name": "repository next"}
            )["name"],
            "repository next",
        )
        self.assertEqual(
            abr_v41.edit_job_record(job["id"], {"name": "job next"})["name"],
            "job next",
        )

    def test_leading_whitespace_cannot_bypass_duplicate_name_check(self) -> None:
        self._create_job("dup-job")
        with self.assertRaises(ApiError) as ctx:
            self._create_job(" dup-job")
        self.assertEqual(ctx.exception.code, "invalid_job")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
