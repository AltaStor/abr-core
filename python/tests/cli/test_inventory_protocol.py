# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from python.cli.parsing.parser_builder import build_parser


class CliProtocolOptionTests(unittest.TestCase):
    def _run(self, argv: list[str], method: str) -> dict:
        args = build_parser().parse_args(["--base-url", "http://127.0.0.1:8000", "--output", "json", *argv])
        with (
            patch(f"python.cli.api.client.AltaStorApiClient.{method}", return_value={"id": "inv-1"}) as call,
            redirect_stdout(io.StringIO()),
        ):
            args.handler(args)
        return call.call_args.kwargs["json"]

    def test_inventory_add_sends_the_chosen_protocol_or_leaves_the_default_to_the_server(self) -> None:
        base = ["inventory", "add", "--name", "hv", "--type", "hyper-v", "--address", "192.0.2.90", "--username", "u", "--password", "p"]

        self.assertEqual(self._run([*base, "--protocol", "http"], "post")["protocol"], "http")
        self.assertIsNone(self._run(base, "post")["protocol"])

    def test_inventory_edit_sends_the_protocol_only_when_given(self) -> None:
        self.assertEqual(self._run(["inventory", "edit", "--id", "inv-1", "--protocol", "https"], "patch"), {"protocol": "https"})
        self.assertNotIn("protocol", self._run(["inventory", "edit", "--id", "inv-1", "--new-name", "x"], "patch"))


if __name__ == "__main__":
    unittest.main()
