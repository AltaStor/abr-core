# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from python.srv.services.abr_v41 import (
    normalize_schedule_payload,
    schedule_latest_next_run,
    schedule_next_run,
)

KYIV = "Europe/Kyiv"
KYIV_ZONE = ZoneInfo(KYIV)
FIRST_PASS = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
SECOND_PASS = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
INSIDE_REPEATED_HOUR = datetime(2026, 10, 25, 1, 5, tzinfo=timezone.utc)
BEFORE_THE_NIGHT = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

SCHEDULES = {
    "daily": {"type": "daily", "time": "03:30", "timezone": KYIV},
    "weekly": {"type": "weekly", "days": ["sun"], "time": "03:30", "timezone": KYIV},
    "monthly": {"type": "monthly", "day_of_month": 25, "time": "03:30", "timezone": KYIV},
    "cron": {"type": "cron", "expression": "30 3 * * *", "timezone": KYIV},
}

NEXT_AFTER_THE_FIRST_PASS = {
    "daily": datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc),
    "weekly": datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc),
    "monthly": datetime(2026, 11, 25, 1, 30, tzinfo=timezone.utc),
    "cron": datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc),
}


def _next_run(kind: str, after: datetime) -> datetime:
    return schedule_next_run(normalize_schedule_payload(SCHEDULES[kind]), after=after)


def _latest_next_run(kind: str, until: datetime) -> datetime:
    return schedule_latest_next_run(normalize_schedule_payload(SCHEDULES[kind]), until=until)


class RepeatedHourTests(unittest.TestCase):
    def test_the_hour_really_repeats_on_this_date(self) -> None:
        self.assertEqual(FIRST_PASS.astimezone(KYIV_ZONE).strftime("%H:%M %Z"), "03:30 EEST")
        self.assertEqual(SECOND_PASS.astimezone(KYIV_ZONE).strftime("%H:%M %Z"), "03:30 EET")

    def test_every_kind_created_in_the_repeated_hour_runs_at_its_second_pass(self) -> None:
        for kind in SCHEDULES:
            with self.subTest(kind=kind):
                self.assertEqual(_next_run(kind, INSIDE_REPEATED_HOUR), SECOND_PASS)

    def test_no_kind_lands_in_the_past(self) -> None:
        for kind in SCHEDULES:
            with self.subTest(kind=kind):
                self.assertGreater(_next_run(kind, INSIDE_REPEATED_HOUR), INSIDE_REPEATED_HOUR)

    def test_a_run_in_the_first_pass_is_not_repeated_in_the_second(self) -> None:
        for kind, expected in NEXT_AFTER_THE_FIRST_PASS.items():
            with self.subTest(kind=kind):
                self.assertEqual(_next_run(kind, FIRST_PASS), expected)

    def test_computed_before_the_night_every_kind_picks_the_first_pass(self) -> None:
        for kind in SCHEDULES:
            with self.subTest(kind=kind):
                self.assertEqual(_next_run(kind, BEFORE_THE_NIGHT), FIRST_PASS)

    def test_an_ordinary_day_is_unchanged(self) -> None:
        ordinary = datetime(2026, 10, 20, 1, 5, tzinfo=timezone.utc)
        expected = {
            "daily": datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc),
            "weekly": datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
            "monthly": datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
            "cron": datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc),
        }
        for kind, moment in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(_next_run(kind, ordinary), moment)


class LatestNextRunTests(unittest.TestCase):
    def test_in_the_second_pass_a_run_already_taken_in_the_first_pass_is_not_offered_again(self) -> None:
        for kind, expected in NEXT_AFTER_THE_FIRST_PASS.items():
            with self.subTest(kind=kind):
                self.assertEqual(_latest_next_run(kind, INSIDE_REPEATED_HOUR), expected)

    def test_at_the_very_start_of_the_second_pass_it_is_the_same(self) -> None:
        start_of_second_pass = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
        for kind, expected in NEXT_AFTER_THE_FIRST_PASS.items():
            with self.subTest(kind=kind):
                self.assertEqual(_latest_next_run(kind, start_of_second_pass), expected)

    def test_outside_the_repeated_hour_it_is_the_next_run(self) -> None:
        for moment in (BEFORE_THE_NIGHT, FIRST_PASS, SECOND_PASS, datetime(2026, 10, 20, 1, 5, tzinfo=timezone.utc)):
            for kind in SCHEDULES:
                with self.subTest(kind=kind, moment=moment):
                    self.assertEqual(_latest_next_run(kind, moment), _next_run(kind, moment))

    def test_a_periodic_schedule_is_unchanged(self) -> None:
        schedule = normalize_schedule_payload({"type": "periodic", "every": "10m", "timezone": KYIV})
        self.assertEqual(
            schedule_latest_next_run(schedule, until=INSIDE_REPEATED_HOUR),
            schedule_next_run(schedule, after=INSIDE_REPEATED_HOUR),
        )


if __name__ == "__main__":
    unittest.main()
