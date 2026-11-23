# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.services import abr_v41

WEEKLY = {"weekly": 2, "monthly": 0, "yearly": 0, "enabled": True}


def full(point_id: str, created_at: str, *, reason: str = "ScheduledFull", marks: dict | None = None) -> dict:
    return {
        "id": point_id,
        "created_at": created_at,
        "full_backup": True,
        "execution_type": "ActiveFull",
        "execution_reason": reason,
        "gfs_marks": dict(marks or {}),
    }


def weekly(points: list[dict]) -> dict[str, bool]:
    return {point["id"]: point["gfs_marks"]["weekly"] for point in points}


class StickyGfsMarkTests(unittest.TestCase):
    def test_a_second_full_of_the_week_does_not_take_the_mark_the_first_has(self) -> None:
        points = [full("first", "2026-11-21T10:00:00+00:00")]
        abr_v41._apply_gfs_marks(points, WEEKLY)

        points.append(full("second", "2026-11-22T12:00:00+00:00", reason="SourceNewChain"))
        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"first": True, "second": False})
        self.assertTrue(points[0]["gfs_protected"])

    def test_without_an_earlier_mark_the_newest_full_of_the_week_is_marked_as_before(self) -> None:
        points = [full("a", "2026-11-17T10:00:00+00:00"), full("b", "2026-11-19T10:00:00+00:00")]

        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"a": False, "b": True})

    def test_each_week_keeps_its_own_full_and_the_count_still_drops_the_oldest_week(self) -> None:
        points = [full("w1", "2026-11-03T10:00:00+00:00")]
        abr_v41._apply_gfs_marks(points, WEEKLY)
        points.append(full("w2", "2026-11-10T10:00:00+00:00"))
        abr_v41._apply_gfs_marks(points, WEEKLY)
        points.append(full("w2-later", "2026-11-12T10:00:00+00:00"))
        points.append(full("w3", "2026-11-17T10:00:00+00:00"))
        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"w1": False, "w2": True, "w2-later": False, "w3": True})

    def test_a_forced_full_that_is_the_only_full_of_its_week_takes_the_mark(self) -> None:
        for reason in ("ForceFull", "ForceSynthetic"):
            with self.subTest(reason):
                points = [full("forced", "2026-11-21T10:00:00+00:00", reason=reason)]

                abr_v41._apply_gfs_marks(points, WEEKLY)

                self.assertEqual(weekly(points), {"forced": True})
                self.assertTrue(points[0]["gfs_protected"])

    def test_a_forced_full_does_not_take_the_mark_from_a_regular_full_of_the_week(self) -> None:
        points = [full("regular", "2026-11-17T10:00:00+00:00")]
        abr_v41._apply_gfs_marks(points, WEEKLY)

        points.append(full("forced", "2026-11-19T10:00:00+00:00", reason="ForceFull"))
        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"regular": True, "forced": False})

    def test_a_regular_full_later_in_the_week_takes_the_mark_a_forced_one_held(self) -> None:
        points = [full("forced", "2026-11-17T10:00:00+00:00", reason="ForceFull")]
        abr_v41._apply_gfs_marks(points, WEEKLY)

        points.append(full("scheduled", "2026-11-19T10:00:00+00:00"))
        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"forced": False, "scheduled": True})

    def test_a_second_forced_full_of_the_week_leaves_the_mark_on_the_first(self) -> None:
        points = [full("first", "2026-11-17T10:00:00+00:00", reason="ForceFull")]
        abr_v41._apply_gfs_marks(points, WEEKLY)

        points.append(full("second", "2026-11-19T10:00:00+00:00", reason="ForceFull"))
        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"first": True, "second": False})

    def test_weeks_of_forced_fulls_count_against_the_kept_number_like_any_week(self) -> None:
        points = [
            full("w1", "2026-11-03T10:00:00+00:00", reason="ForceFull"),
            full("w2", "2026-11-10T10:00:00+00:00"),
            full("w3", "2026-11-17T10:00:00+00:00", reason="ForceFull"),
        ]

        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"w1": False, "w2": True, "w3": True})

    def test_a_disabled_policy_clears_the_marks_and_a_new_one_starts_from_the_newest(self) -> None:
        points = [full("a", "2026-11-17T10:00:00+00:00")]
        abr_v41._apply_gfs_marks(points, WEEKLY)
        abr_v41._apply_gfs_marks(points, {**WEEKLY, "enabled": False})
        points.append(full("b", "2026-11-19T10:00:00+00:00"))

        abr_v41._apply_gfs_marks(points, WEEKLY)

        self.assertEqual(weekly(points), {"a": False, "b": True})


if __name__ == "__main__":
    unittest.main()
