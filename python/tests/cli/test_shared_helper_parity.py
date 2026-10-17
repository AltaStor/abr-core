# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DUPLICATED_MODULES = ["logger_conf.py"]


class SharedHelperParityTests(unittest.TestCase):
    def test_duplicated_modules_are_byte_identical(self) -> None:
        for filename in DUPLICATED_MODULES:
            cli_path = REPO_ROOT / "python" / "cli" / "core" / filename
            srv_path = REPO_ROOT / "python" / "srv" / "core" / filename

            with self.subTest(filename=filename):
                self.assertTrue(cli_path.is_file(), f"missing {cli_path}")
                self.assertTrue(srv_path.is_file(), f"missing {srv_path}")
                self.assertEqual(
                    cli_path.read_text(encoding="utf-8"),
                    srv_path.read_text(encoding="utf-8"),
                    f"python.cli.core.{filename[:-3]} and python.srv.core.{filename[:-3]} "
                    "have drifted apart; fix the bug/change in both copies.",
                )


if __name__ == "__main__":
    unittest.main()
