# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from python.cli import abr_cli
from python.cli.exit_codes import INVALID_USAGE
from python.cli.parsing.parser_builder import LOG_LEVEL_CHOICES


class UnknownLogLevelTests(unittest.TestCase):
    def _refusal(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            abr_cli.main(argv)
        return ctx.exception.code, stderr.getvalue()

    def test_an_unknown_level_after_the_command_is_a_usage_error(self) -> None:
        code, stderr = self._refusal(["repo", "list", "--log-level", "verbose"])

        self.assertEqual(code, INVALID_USAGE)
        self.assertIn(
            "argument --log-level: invalid choice: 'verbose' (choose from debug, info, warn, error)", stderr
        )
        self.assertNotIn("Traceback", stderr)

    def test_an_unknown_level_before_the_command_is_a_usage_error(self) -> None:
        code, stderr = self._refusal(["--log-level", "LOUD", "system", "version"])

        self.assertEqual(code, INVALID_USAGE)
        self.assertIn("invalid choice: 'LOUD'", stderr)

    def test_warning_is_refused_rather_than_mangled_into_a_crash(self) -> None:
        code, stderr = self._refusal(["repo", "list", "--log-level", "warning"])

        self.assertEqual(code, INVALID_USAGE)
        self.assertIn("invalid choice: 'warning'", stderr)


class LogLevelScanTests(unittest.TestCase):
    def test_every_level_the_option_accepts_is_applied_early(self) -> None:
        for level in LOG_LEVEL_CHOICES:
            with self.subTest(level=level):
                self.assertEqual(abr_cli._scan_log_level(["repo", "list", "--log-level", level]), level)

    def test_a_level_the_option_refuses_falls_back_to_info(self) -> None:
        for value in ("verbose", "warning", "DEBUG", ""):
            with self.subTest(value=value):
                self.assertEqual(abr_cli._scan_log_level(["repo", "list", "--log-level", value]), "info")

    def test_a_missing_or_absent_level_is_info(self) -> None:
        self.assertEqual(abr_cli._scan_log_level(["repo", "list", "--log-level"]), "info")
        self.assertEqual(abr_cli._scan_log_level(["repo", "list"]), "info")


if __name__ == "__main__":
    unittest.main()
