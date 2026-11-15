# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from python.cli.parsing.parser_builder import build_parser


class ScheduleOptionErrorTests(unittest.TestCase):
    def _error(self, *argv: str) -> str:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            build_parser().parse_args(["job", "edit", "--name", "Nightly", *argv])
        return err.getvalue()

    def test_a_bad_day_of_month_or_interval_names_the_option_given(self) -> None:
        for prefix in ("", "active-full-", "synthetic-full-", "verify-"):
            with self.subTest(prefix=prefix or "backup"):
                day = self._error(f"--{prefix}day-of-month", "32")
                every = self._error(f"--{prefix}every", "0m")

                self.assertIn(f"argument --{prefix}day-of-month: `--{prefix}day-of-month` must be an integer from 1 to 31", day)
                self.assertIn(f"argument --{prefix}every: `--{prefix}every` must use a duration like 15m, 1h, 6h, or 1d", every)

    def test_good_values_still_parse(self) -> None:
        args = build_parser().parse_args(
            ["job", "edit", "--name", "Nightly", "--verify-day-of-month", "31", "--verify-every", "1H", "--active-full-day-of-month", "1", "--every", "15m"]
        )

        self.assertEqual((args.verify_day_of_month, args.verify_every, args.active_full_day_of_month, args.every), (31, "1h", 1, "15m"))


if __name__ == "__main__":
    unittest.main()
