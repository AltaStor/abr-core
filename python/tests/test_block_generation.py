# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, REPOSITORIES
from python.srv.services import abr_v41
from python.srv.services.backup_mode import object_lock_generation_plan

CHAIN = "backup::job-1::vm-1::repo-1"
SEGMENT = "segment-1"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _point(point_id: str, *, started_at: datetime | None, expires_at: datetime | None, chain: str = CHAIN) -> dict:
    return {
        "id": point_id,
        "chain_id": chain,
        "segment_id": "segment-1",
        "repository_id": "repo-1",
        "created_at": (started_at or START).isoformat(),
        "immutability_generation_started_at": started_at.isoformat() if started_at else None,
        "immutability_expires_at": expires_at.isoformat() if expires_at else None,
        "artifact": {"disks": [{"abr_path": "lab/vm-1", "abr_backup_name": f"01-{point_id}.vmdk"}]},
    }


class BlockGenerationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def _plan(self, now: datetime, *, retention: int = 40, window: int = 10) -> dict:
        return object_lock_generation_plan(
            CHAIN, actual_retention_days=retention, block_generation_days=window, now=now
        )

    def test_the_first_backup_of_a_chain_opens_a_generation(self) -> None:
        plan = self._plan(START)
        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["lock_days"], 40)
        self.assertEqual(plan["expires_at"], (START + timedelta(days=40)).isoformat())

    def test_a_backup_inside_the_window_joins_the_generation_and_shares_its_expiry(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        plan = self._plan(START + timedelta(days=9))

        self.assertFalse(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], (START + timedelta(days=40)).isoformat())
        self.assertEqual(plan["lock_days"], 31)

    def test_a_joined_backup_still_keeps_at_least_the_configured_period(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        plan = self._plan(START + timedelta(days=10) - timedelta(seconds=1))

        self.assertGreaterEqual(plan["lock_days"], 30)

    def test_a_backup_past_the_window_opens_the_next_generation(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        now = START + timedelta(days=11)
        plan = self._plan(now)

        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["lock_days"], 40)
        self.assertEqual(plan["expires_at"], (now + timedelta(days=40)).isoformat())

    def test_another_chain_does_not_share_this_one_s_generation(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40), chain="other")

        self.assertTrue(self._plan(START + timedelta(days=1))["starts_new_generation"])

    def test_a_full_backup_always_opens_its_own_generation(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        now = START + timedelta(days=3)
        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=40, block_generation_days=10, now=now, starts_segment=True
        )

        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], (now + timedelta(days=40)).isoformat())

    def test_raising_the_period_mid_generation_cuts_the_generation_short(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        now = START + timedelta(days=2)
        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=70, block_generation_days=10, now=now
        )

        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["lock_days"], 70)

    def test_lowering_the_period_mid_generation_still_joins(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=20, block_generation_days=10, now=START + timedelta(days=2)
        )

        self.assertFalse(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], (START + timedelta(days=40)).isoformat())

    def test_a_repository_without_immutability_gets_no_generation(self) -> None:
        plan = self._plan(START, retention=0)
        self.assertEqual(plan["lock_days"], 0)
        self.assertIsNone(plan["expires_at"])


class ChainLockExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "S3",
            "type": "s3",
            "metadata": {"s3": {"bucket": "b", "immutability": {"mode": "governance", "retention_days": 30}}},
        }

    def tearDown(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()

    def test_every_stored_point_of_the_chain_is_pushed_to_the_new_expiry(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))
        BACKUPS["point-2"] = _point("point-2", started_at=START, expires_at=START + timedelta(days=40))
        new_expiry = (abr_v41._now_utc() + timedelta(days=70)).isoformat()

        with patch.object(abr_v41, "apply_repository_immutability") as apply_mock:
            report = abr_v41.extend_segment_object_lock(SEGMENT, expires_at=new_expiry)

        self.assertEqual(sorted(report["extended_restore_points"]), ["point-1", "point-2"])
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["retention_days"], 70)
        self.assertEqual(apply_mock.call_args.kwargs["retention_days"], 70)
        self.assertEqual(BACKUPS["point-1"]["immutability_expires_at"], new_expiry)

    def test_the_point_that_already_carries_the_new_expiry_is_skipped(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))
        new_expiry = (abr_v41._now_utc() + timedelta(days=70)).isoformat()

        with patch.object(abr_v41, "apply_repository_immutability") as apply_mock:
            report = abr_v41.extend_segment_object_lock(SEGMENT, expires_at=new_expiry, skip_point_ids={"point-1"})

        self.assertEqual(report["extended_restore_points"], [])
        apply_mock.assert_not_called()

    def test_a_storage_refusal_is_reported_and_the_stored_expiry_is_left_alone(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))
        new_expiry = (abr_v41._now_utc() + timedelta(days=70)).isoformat()

        with patch.object(abr_v41, "apply_repository_immutability", side_effect=abr_v41.StorageToolError("refused")):
            report = abr_v41.extend_segment_object_lock(SEGMENT, expires_at=new_expiry)

        self.assertEqual(report["extended_restore_points"], [])
        self.assertEqual(len(report["failures"]), 2)
        self.assertEqual(
            BACKUPS["point-1"]["immutability_expires_at"], (START + timedelta(days=40)).isoformat()
        )

    def test_a_point_that_already_reaches_past_the_new_date_is_not_touched(self) -> None:
        far = (abr_v41._now_utc() + timedelta(days=90)).isoformat()
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=None)
        BACKUPS["point-1"]["immutability_expires_at"] = far

        with patch.object(abr_v41, "apply_repository_immutability") as apply_mock:
            report = abr_v41.extend_segment_object_lock(
                SEGMENT, expires_at=(abr_v41._now_utc() + timedelta(days=70)).isoformat()
            )

        apply_mock.assert_not_called()
        self.assertEqual(report["extended_restore_points"], [])
        self.assertEqual(BACKUPS["point-1"]["immutability_expires_at"], far)

    def test_a_repository_without_immutability_is_left_untouched(self) -> None:
        REPOSITORIES["repo-1"]["metadata"]["s3"]["immutability"] = {"mode": "none", "retention_days": 0}
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        with patch.object(abr_v41, "apply_repository_immutability") as apply_mock:
            report = abr_v41.extend_segment_object_lock(
                SEGMENT, expires_at=(abr_v41._now_utc() + timedelta(days=70)).isoformat()
            )

        apply_mock.assert_not_called()
        self.assertEqual(report["extended_restore_points"], [])


class ApplyImmutabilityKeepsTheGenerationInStepTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "S3",
            "type": "s3",
            "metadata": {"s3": {"bucket": "b", "immutability": {"mode": "governance", "retention_days": 30}}},
        }

    def tearDown(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()

    def test_reapplying_the_lock_writes_the_new_expiry_back_to_the_point(self) -> None:
        BACKUPS["point-1"] = _point("point-1", started_at=START, expires_at=START + timedelta(days=40))

        with patch.object(abr_v41, "apply_repository_immutability"):
            result = abr_v41.apply_immutability_to_existing_backups("repo-1")

        self.assertEqual(result["status"], "success")
        stored = abr_v41._parse_dt(BACKUPS["point-1"]["immutability_expires_at"])
        self.assertAlmostEqual(
            (stored - abr_v41._now_utc()).total_seconds() / 86400, result["actual_retention_days"][0], places=2
        )


if __name__ == "__main__":
    unittest.main()
