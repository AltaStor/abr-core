# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS
from python.srv.services import abr_v41
from python.srv.services.backup_mode import (
    encryption_changed_since_last_backup,
    resolve_job_backup_mode,
)
from python.srv.services.backup_catalog import (
    _resolve_point_encryption_key_id,
    backfill_backup_encryption_key_ids,
)

KEY_A = "et-key-alpha-000000000001"
KEY_B = "et-key-bravo-000000000002"
KEY_C = "et-key-charlie-0000000003"


def _validated(params: dict, previous: dict | None = None) -> dict:
    return abr_v41._validate_task_parameters(dict(params), previous=previous)


class JobKeyListTests(unittest.TestCase):
    def test_enabling_encryption_records_the_first_key_as_id_zero(self) -> None:
        result = _validated({"encryption_enabled": True, "encryption_key": KEY_A})

        self.assertEqual(result["encryption_key_id"], 0)
        self.assertEqual(result["encryption_keys"], [{"key_id": 0, "key": KEY_A}])

    def test_rotating_appends_instead_of_replacing(self) -> None:
        first = _validated({"encryption_enabled": True, "encryption_key": KEY_A})

        second = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=first)

        self.assertEqual(second["encryption_key"], KEY_B)
        self.assertEqual(second["encryption_key_id"], 1)
        self.assertEqual(
            second["encryption_keys"],
            [{"key_id": 0, "key": KEY_A}, {"key_id": 1, "key": KEY_B}],
        )

    def test_key_ids_keep_incrementing_across_rotations(self) -> None:
        first = _validated({"encryption_enabled": True, "encryption_key": KEY_A})
        second = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=first)

        third = _validated({"encryption_enabled": True, "encryption_key": KEY_C}, previous=second)

        self.assertEqual([entry["key_id"] for entry in third["encryption_keys"]], [0, 1, 2])
        self.assertEqual(third["encryption_key_id"], 2)

    def test_re_supplying_the_same_key_does_not_mint_a_new_id(self) -> None:
        first = _validated({"encryption_enabled": True, "encryption_key": KEY_A})

        again = _validated({"encryption_enabled": True, "encryption_key": KEY_A}, previous=first)

        self.assertEqual(again["encryption_key_id"], 0)
        self.assertEqual(len(again["encryption_keys"]), 1)

    def test_a_job_written_before_the_list_existed_is_carried_over_as_id_zero(self) -> None:
        legacy = {"encryption_enabled": True, "encryption_key": KEY_A}

        rotated = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=legacy)

        self.assertEqual(
            rotated["encryption_keys"],
            [{"key_id": 0, "key": KEY_A}, {"key_id": 1, "key": KEY_B}],
        )

    def test_turning_encryption_off_drops_the_list(self) -> None:
        first = _validated({"encryption_enabled": True, "encryption_key": KEY_A})

        disabled = _validated({"encryption_enabled": False}, previous=first)

        self.assertNotIn("encryption_keys", disabled)
        self.assertNotIn("encryption_key_id", disabled)


class PointKeyIdTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        BACKUPS.clear()

    def tearDown(self) -> None:
        JOBS.clear()
        BACKUPS.clear()

    def _job(self) -> dict:
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_A})
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=parameters)
        return {"id": "job-1", "task_parameters": parameters}

    def test_a_point_is_stamped_with_the_key_it_was_written_under(self) -> None:
        job = self._job()

        self.assertEqual(_resolve_point_encryption_key_id(job, KEY_A), 0)
        self.assertEqual(_resolve_point_encryption_key_id(job, KEY_B), 1)

    def test_an_unknown_key_gets_no_id(self) -> None:
        self.assertIsNone(_resolve_point_encryption_key_id(self._job(), KEY_C))

    def test_existing_points_are_stamped_at_startup(self) -> None:
        JOBS["job-1"] = self._job()
        BACKUPS["point-1"] = {
            "id": "point-1",
            "job_id": "job-1",
            "encryption_enabled": True,
            "encryption_key": KEY_A,
        }

        self.assertEqual(backfill_backup_encryption_key_ids(), 1)
        self.assertEqual(BACKUPS["point-1"]["encryption_key_id"], 0)


class RestoreKeyResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()

    def tearDown(self) -> None:
        JOBS.clear()

    def _rotated_job(self) -> dict:
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_A})
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=parameters)
        return {
            "id": "job-1",
            "name": "rotated",
            "job_type": "backup",
            "task_parameters": parameters,
        }

    def test_a_point_written_under_the_old_key_still_resolves_it(self) -> None:
        JOBS["job-1"] = self._rotated_job()
        point = {"id": "p1", "job_id": "job-1", "encryption_enabled": True, "encryption_key_id": 0}

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_A)

    def test_a_point_written_under_the_current_key_resolves_it(self) -> None:
        JOBS["job-1"] = self._rotated_job()
        point = {"id": "p2", "job_id": "job-1", "encryption_enabled": True, "encryption_key_id": 1}

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_B)

    def test_the_key_stored_on_the_point_still_wins_so_a_deleted_job_cannot_strand_it(self) -> None:
        point = {"id": "p3", "job_id": "gone", "encryption_enabled": True, "encryption_key": KEY_A}

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_A)

    def test_the_key_id_is_what_resolves_it_not_the_copy_on_the_point(self) -> None:
        JOBS["job-1"] = self._rotated_job()
        point = {
            "id": "p5",
            "job_id": "job-1",
            "encryption_enabled": True,
            "encryption_key_id": 0,
            "encryption_key": "a-stale-copy-that-must-not-win",
        }

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_A)

    def test_the_copy_on_the_point_is_the_fallback_when_the_job_is_gone(self) -> None:
        point = {
            "id": "p6",
            "job_id": "gone",
            "encryption_enabled": True,
            "encryption_key_id": 0,
            "encryption_key": KEY_A,
        }

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_A)

    def test_an_id_the_job_no_longer_lists_falls_back_to_the_stored_copy(self) -> None:
        JOBS["job-1"] = self._rotated_job()
        point = {
            "id": "p7",
            "job_id": "job-1",
            "encryption_enabled": True,
            "encryption_key_id": 99,
            "encryption_key": KEY_A,
        }

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_A)

    def test_a_point_with_no_id_falls_back_to_the_current_key(self) -> None:
        JOBS["job-1"] = self._rotated_job()
        point = {"id": "p4", "job_id": "job-1", "encryption_enabled": True}

        self.assertEqual(abr_v41._resolve_restore_encryption_key(point), KEY_B)


class ChainNeverSpansTwoKeysTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def _task(self) -> dict:
        return {
            "id": "task-1",
            "job_id": "job-1",
            "hypervisor_id": "hv-1",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-1",
        }

    def _point(self, key: str | None, *, enabled: bool = True) -> None:
        BACKUPS["point-1"] = {
            "id": "point-1",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "encryption_enabled": enabled,
            "encryption_key": key,
            "origin": {"job_id": "job-1"},
            "source": {
                "hypervisor_id": "hv-1",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-1",
            },
        }

    def test_an_unchanged_key_still_allows_an_incremental(self) -> None:
        self._point(KEY_A)
        parameters = {"encryption_enabled": True, "encryption_key": KEY_A}

        self.assertFalse(encryption_changed_since_last_backup(self._task(), parameters))
        self.assertEqual(resolve_job_backup_mode(self._task(), parameters), "incremental")

    def test_a_rotated_key_forces_a_full_so_the_chain_starts_again(self) -> None:
        self._point(KEY_A)
        parameters = {"encryption_enabled": True, "encryption_key": KEY_B}

        self.assertTrue(encryption_changed_since_last_backup(self._task(), parameters))
        self.assertEqual(resolve_job_backup_mode(self._task(), parameters), "full")

    def test_turning_encryption_on_over_plain_backups_forces_a_full(self) -> None:
        self._point(None, enabled=False)
        parameters = {"encryption_enabled": True, "encryption_key": KEY_A}

        self.assertEqual(resolve_job_backup_mode(self._task(), parameters), "full")

    def test_turning_encryption_off_over_encrypted_backups_forces_a_full(self) -> None:
        self._point(KEY_A)
        parameters: dict = {}

        self.assertEqual(resolve_job_backup_mode(self._task(), parameters), "full")

    def test_a_plain_job_that_stays_plain_still_increments(self) -> None:
        self._point(None, enabled=False)
        parameters: dict = {}

        self.assertFalse(encryption_changed_since_last_backup(self._task(), parameters))
        self.assertEqual(resolve_job_backup_mode(self._task(), parameters), "incremental")

    def test_the_first_backup_of_a_job_is_not_treated_as_a_rotation(self) -> None:
        parameters = {"encryption_enabled": True, "encryption_key": KEY_A}

        self.assertFalse(encryption_changed_since_last_backup(self._task(), parameters))

    def test_a_point_written_before_keys_were_recorded_does_not_force_a_full(self) -> None:
        self._point(None)
        parameters = {"encryption_enabled": True, "encryption_key": KEY_A}

        self.assertFalse(encryption_changed_since_last_backup(self._task(), parameters))


class JobViewTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()

    def tearDown(self) -> None:
        JOBS.clear()

    def test_the_job_view_summarises_the_keys_without_revealing_them(self) -> None:
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_A})
        parameters = _validated({"encryption_enabled": True, "encryption_key": KEY_B}, previous=parameters)
        job = {
            "id": "job-1",
            "name": "rotated",
            "job_type": "backup",
            "inventory_id": "inv-1",
            "task_parameters": parameters,
        }

        view = abr_v41.job_to_dto(job)

        self.assertEqual(view["encryption"]["key_id"], 1)
        self.assertEqual(view["encryption"]["key_ids"], [0, 1])
        self.assertEqual(view["encryption"]["key_count"], 2)
        rendered = str(view)
        self.assertNotIn(KEY_A, rendered)
        self.assertNotIn(KEY_B, rendered)


if __name__ == "__main__":
    unittest.main()
