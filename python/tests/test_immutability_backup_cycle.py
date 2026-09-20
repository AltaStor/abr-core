# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from python.srv.core.mongo_db_wrapper import BACKUPS
from python.srv.services.backup_mode import (
    days_to_next_full,
    object_lock_actual_retention_days,
    object_lock_expiry,
    object_lock_generation_plan,
)

CHAIN = "backup::job-1::vm-1::repo-1"
JAN_1 = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)


def _job(retention: int = 30, *, weekly_on: str = "Thu", enabled: bool = True) -> dict:
    return {
        "id": "job-1",
        "retention_days": retention,
        "active_full_schedule": {
            "kind": "weekly",
            "enabled": enabled,
            "weekdays": [weekly_on],
            "time": "02:00",
        },
    }


def _monthly_job(retention: int = 30) -> dict:
    return {
        "id": "job-1",
        "retention_days": retention,
        "active_full_schedule": {
            "kind": "monthly",
            "enabled": True,
            "day_of_month": 1,
            "time": "02:00",
        },
    }


def _immutability(days: int = 7, scope: str = "job_retention") -> dict:
    return {"mode": "governance", "retention_days": days, "retention_scope": scope}


class CycleDerivedLockTests(unittest.TestCase):
    def test_the_lock_reaches_the_next_full_plus_the_job_retention(self) -> None:
        # 01.01.2026 is a Thursday, so the next weekly Thursday full is seven days out.
        days = object_lock_actual_retention_days(
            _job(retention=30), _immutability(), block_generation_days=10, now=JAN_1
        )

        self.assertEqual(days, 7 + 30)

    def test_a_later_point_of_the_same_cycle_lands_on_the_same_date(self) -> None:
        first = JAN_1 + timedelta(days=0)
        fifth = JAN_1 + timedelta(days=5)

        first_days = object_lock_actual_retention_days(
            _job(), _immutability(), block_generation_days=10, now=first
        )
        fifth_days = object_lock_actual_retention_days(
            _job(), _immutability(), block_generation_days=10, now=fifth
        )

        self.assertEqual(first + timedelta(days=first_days), fifth + timedelta(days=fifth_days))

    def test_a_monthly_cycle_asks_for_more_than_the_old_flat_padding(self) -> None:
        days = object_lock_actual_retention_days(
            _monthly_job(retention=30), _immutability(), block_generation_days=10, now=JAN_1
        )

        self.assertGreater(days, 30 + 10)
        self.assertEqual(days, days_to_next_full(_monthly_job(), now=JAN_1) + 30)

    def test_the_repository_period_is_a_floor_not_an_addend(self) -> None:
        days = object_lock_actual_retention_days(
            _job(retention=1), _immutability(days=90), block_generation_days=10, now=JAN_1
        )

        self.assertEqual(days, 90)

    def test_minimum_scope_still_means_the_configured_period_alone(self) -> None:
        days = object_lock_actual_retention_days(
            _job(retention=30),
            _immutability(days=7, scope="minimum_only"),
            block_generation_days=10,
            now=JAN_1,
        )

        self.assertEqual(days, 7)

    def test_immutability_off_stays_off(self) -> None:
        days = object_lock_actual_retention_days(
            _job(), _immutability(days=0), block_generation_days=10, now=JAN_1
        )

        self.assertEqual(days, 0)

    def test_without_a_full_schedule_the_block_generation_is_the_fallback(self) -> None:
        job = _job(enabled=False)

        self.assertIsNone(days_to_next_full(job, now=JAN_1))
        self.assertEqual(
            object_lock_actual_retention_days(
                job, _immutability(), block_generation_days=10, now=JAN_1
            ),
            10 + 30,
        )


class ChainKeepsTheLaterDateTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def _point(self, expires: datetime, started: datetime) -> None:
        BACKUPS["point-1"] = {
            "id": "point-1",
            "chain_id": CHAIN,
            "created_at": started.isoformat(),
            "immutability_generation_started_at": started.isoformat(),
            "immutability_expires_at": expires.isoformat(),
        }

    def test_an_increment_inside_the_cycle_joins_the_chain_date(self) -> None:
        chain_expiry = JAN_1 + timedelta(days=37)
        self._point(chain_expiry, JAN_1)
        later = JAN_1 + timedelta(days=3)

        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=34, block_generation_days=10, now=later
        )

        self.assertFalse(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], chain_expiry.isoformat())

    def test_a_late_full_makes_the_chain_reach_further_and_extends_it(self) -> None:
        chain_expiry = JAN_1 + timedelta(days=37)
        self._point(chain_expiry, JAN_1)
        # The full never came; a week later the next one is another seven days out.
        later = JAN_1 + timedelta(days=7)

        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=37, block_generation_days=10, now=later
        )

        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], (later + timedelta(days=37)).isoformat())

    def test_a_full_always_dates_its_own_segment(self) -> None:
        self._point(JAN_1 + timedelta(days=90), JAN_1)
        later = JAN_1 + timedelta(days=7)

        plan = object_lock_generation_plan(
            CHAIN, actual_retention_days=37, block_generation_days=10, now=later, starts_segment=True
        )

        self.assertTrue(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], (later + timedelta(days=37)).isoformat())

    def test_two_backups_minutes_apart_share_the_instant_and_do_not_rewrite(self) -> None:
        job, immutability = _job(), _immutability()
        first_at = JAN_1
        second_at = JAN_1 + timedelta(seconds=46)

        first = object_lock_expiry(
            job, immutability, block_generation_days=10, now=first_at
        )
        second = object_lock_expiry(
            job, immutability, block_generation_days=10, now=second_at
        )

        self.assertEqual(first, second)

        self._point(first, first_at)
        plan = object_lock_generation_plan(
            CHAIN,
            actual_retention_days=37,
            block_generation_days=10,
            now=second_at,
            own_expiry=second,
        )

        self.assertFalse(plan["starts_new_generation"])
        self.assertEqual(plan["expires_at"], first.isoformat())

    def test_a_healthy_cycle_never_rewrites_the_chain(self) -> None:
        job, immutability = _job(), _immutability()
        started = JAN_1
        days = object_lock_actual_retention_days(
            job, immutability, block_generation_days=10, now=started
        )
        self._point(started + timedelta(days=days), started)

        extensions = 0
        for offset in range(1, 7):
            now = JAN_1 + timedelta(days=offset)
            plan = object_lock_generation_plan(
                CHAIN,
                actual_retention_days=object_lock_actual_retention_days(
                    job, immutability, block_generation_days=10, now=now
                ),
                block_generation_days=10,
                now=now,
            )
            extensions += int(bool(plan["starts_new_generation"]))

        self.assertEqual(extensions, 0)


if __name__ == "__main__":
    unittest.main()
