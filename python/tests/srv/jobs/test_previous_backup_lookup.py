# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import BACKUPS
from python.srv.services.backup_mode import (
    encryption_changed_since_last_backup,
    has_previous_successful_backup,
)

VM = "vm-1"
SOURCE = {"hypervisor_id": "vc", "resource_type": "virtual_machine", "resource_external_id": VM}


def _point(point_id: str, job_id: str, *, vm_id: str = VM, created_at: str, **extra) -> None:
    BACKUPS[point_id] = {
        "id": point_id,
        "status": "available",
        "job_id": job_id,
        "vm_id": vm_id,
        "origin": {"job_id": job_id},
        "source": dict(SOURCE, resource_external_id=vm_id),
        "created_at": created_at,
        **extra,
    }


def _task(job_id: str | None) -> dict:
    return {"job_id": job_id, **SOURCE}


class PreviousBackupLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        self.addCleanup(BACKUPS.clear)

    def test_a_newer_encrypted_point_of_another_job_does_not_make_this_job_a_full(self) -> None:
        _point("own", "nightly", created_at="2026-11-01T01:00:00+00:00")
        _point(
            "other",
            "offsite",
            created_at="2026-11-02T01:00:00+00:00",
            encryption_enabled=True,
            encryption_key="placeholder-key-1234",
        )

        self.assertFalse(encryption_changed_since_last_backup(_task("nightly"), {}))

    def test_a_point_of_another_job_is_not_a_previous_backup_of_this_one(self) -> None:
        _point("other", "offsite", created_at="2026-11-02T01:00:00+00:00")

        self.assertFalse(has_previous_successful_backup(_task("nightly")))

    def test_another_vm_of_the_same_job_is_not_this_vm_chain(self) -> None:
        _point("f1", "nightly", vm_id="vm-9", created_at="2026-11-01T01:00:00+00:00")

        self.assertFalse(has_previous_successful_backup(_task("nightly")))
        _point("f2", "nightly", created_at="2026-11-01T02:00:00+00:00")
        self.assertTrue(has_previous_successful_backup(_task("nightly")))

    def test_a_backup_with_no_job_finds_the_vm_own_backups_but_not_copies(self) -> None:
        _point("c1", "offsite", created_at="2026-11-02T01:00:00+00:00", copy_source={"point_id": "f0"})
        self.assertFalse(has_previous_successful_backup(_task(None)))

        _point("f1", "nightly", created_at="2026-11-01T01:00:00+00:00")
        self.assertTrue(has_previous_successful_backup(_task(None)))


if __name__ == "__main__":
    unittest.main()
