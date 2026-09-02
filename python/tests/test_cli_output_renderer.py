# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from python.cli.cli_output_renderer import CliOutputRenderer


class CliOutputRendererTableFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = CliOutputRenderer()

    def _render(self, output: str, payload, columns=None) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.renderer.print_output(output, payload, columns)
        return buf.getvalue()

    def test_object_payload_without_columns_renders_as_table_not_json(self) -> None:
        text = self._render(
            "table",
            {"cli_binary": "abr", "product": "AltaStor Backup & Replication", "version": "4.1.0"},
        )
        self.assertNotIn("{", text)
        self.assertIn("FIELD", text)
        self.assertIn("VALUE", text)
        self.assertIn("cli_binary", text)
        self.assertIn("4.1.0", text)

    def test_object_payload_with_explicit_columns_still_renders_as_before(self) -> None:
        text = self._render(
            "table",
            {"status": "ok", "components": {"api": "ok"}},
            [("status", "STATUS"), ("components", "COMPONENTS")],
        )
        self.assertIn("STATUS", text)
        self.assertIn("COMPONENTS", text)
        self.assertIn("ok", text)
        self.assertNotIn("FIELD", text)

    def test_list_command_with_items_and_columns_is_unaffected(self) -> None:
        text = self._render(
            "table",
            {"items": [{"id": "r1", "name": "Repo1"}, {"id": "r2", "name": "Repo2"}]},
            [("id", "ID"), ("name", "NAME")],
        )
        self.assertIn("ID", text)
        self.assertIn("Repo1", text)
        self.assertIn("Repo2", text)

    def test_json_output_ignores_columns_and_is_unaffected(self) -> None:
        text = self._render("json", {"cli_binary": "abr", "version": "4.1.0"})
        self.assertEqual(json.loads(text), {"cli_binary": "abr", "version": "4.1.0"})

    def test_none_payload_without_columns_does_not_crash(self) -> None:
        text = self._render("table", None)
        self.assertIn("VALUE", text)


if __name__ == "__main__":
    unittest.main()
