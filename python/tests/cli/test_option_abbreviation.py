# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr

from python.cli.parsing.parser_builder import build_parser


class OptionAbbreviationTests(unittest.TestCase):
    def _error(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(list(argv))
        return int(caught.exception.code), err.getvalue()

    def test_a_prefix_of_a_longer_option_is_refused_and_named(self) -> None:
        code, err = self._error("restore", "point", "list", "--vm", "web-01")

        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --vm", err)
        self.assertIn("the following arguments are required: --vm-id", err)

    def test_a_prefix_of_an_option_the_command_has_is_refused_when_nothing_is_missing(self) -> None:
        code, err = self._error("job", "list", "--out", "json")

        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --out json", err)

    def test_the_json_error_names_only_the_unknown_option(self) -> None:
        _code, err = self._error("--output", "json", "restore", "point", "list", "--vm", "web-01")

        self.assertEqual(json.loads(err)["error"]["details"]["argument"], "--vm")

    def test_full_options_aliases_and_short_flags_still_parse(self) -> None:
        listed = build_parser().parse_args(["restore", "point", "list", "--vm-id", "vm-1", "--output", "json"])
        created = build_parser().parse_args(
            ["job", "create", "--name", "j", "--vm", "web-01", "--repo", "r", "--active-full-schedule", "periodic", "--active-full-every", "3650d"]
        )

        self.assertEqual((listed.vm_id, listed.output), ("vm-1", "json"))
        self.assertEqual((created.repo, created.vm), ("r", ["web-01"]))

    def test_a_missing_required_option_alone_is_reported_as_before(self) -> None:
        code, err = self._error("restore", "point", "list")

        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --vm-id", err)
        self.assertNotIn("unrecognized", err)


if __name__ == "__main__":
    unittest.main()
