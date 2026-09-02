# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace

from python.cli.api.client import AltaStorApiClient
from python.cli.cli_context import CliContext
from python.cli.cli_output_renderer import CliOutputRenderer
from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.cli.parsing.validators import parse_strict_hhmm, parse_weekday_csv
from python.cli.table_renderer import TableRenderer


class CliArchitectureTests(unittest.TestCase):
    def test_parser_builder_returns_altastor_parser_with_major_groups(self) -> None:
        parser = AltaStorParserBuilder().build()
        help_output = parser.format_help()

        self.assertIn("inventory", help_output)
        self.assertIn("repo", help_output)
        self.assertIn("job", help_output)
        self.assertIn("backup", help_output)
        self.assertIn("restore", help_output)
        self.assertIn("session", help_output)
        self.assertIn("system", help_output)

    def test_cli_context_builds_api_client_and_renderer(self) -> None:
        context = CliContext.from_args(SimpleNamespace(base_url="http://127.0.0.1:8000/", output="json"))

        self.assertEqual(context.base_url, "http://127.0.0.1:8000/")
        self.assertIsInstance(context.api_client, AltaStorApiClient)
        self.assertEqual(context.api_client.base_url, "http://127.0.0.1:8000")
        self.assertIsInstance(context.output_renderer, CliOutputRenderer)

    def test_parse_weekday_csv_dedupes_and_normalizes(self) -> None:
        self.assertEqual(parse_weekday_csv("Mon,wed,Mon,Fri"), ["monday", "wednesday", "friday"])

    def test_parse_strict_hhmm_accepts_and_rejects_expected_values(self) -> None:
        self.assertEqual(parse_strict_hhmm("02:30"), "02:30")
        with self.assertRaises(Exception):
            parse_strict_hhmm("2:30")

    def test_table_renderer_supports_nested_dict_columns(self) -> None:
        renderer = TableRenderer()

        rendered = renderer.render(
            [
                {
                    "id": "sess-1",
                    "job": {"id": "job-1", "name": "Nightly"},
                }
            ],
            [("id", "SESSION ID"), ("job.id", "JOB ID")],
        )

        self.assertIn("sess-1", rendered)
        self.assertIn("job-1", rendered)
        self.assertNotIn("Nightly", rendered)

    def test_table_renderer_wraps_long_values_onto_extra_lines_instead_of_truncating(self) -> None:
        renderer = TableRenderer()
        long_name = "word " * 30

        rendered = renderer.render(
            [{"field": "name", "value": long_name}],
            [("field", "FIELD"), ("value", "VALUE")],
        )

        self.assertNotIn("…", rendered)
        self.assertIn("word", rendered)
        lines = rendered.splitlines()
        self.assertGreater(len(lines), 3)

    def test_table_renderer_does_not_break_a_single_unbreakable_token_mid_word(self) -> None:
        renderer = TableRenderer()
        long_path = "/tmp/some/very/deeply/nested/path/" + "segment/" * 15 + "file.txt"

        rendered = renderer.render(
            [{"field": "path", "value": long_path}],
            [("field", "FIELD"), ("value", "VALUE")],
        )

        self.assertIn(long_path, rendered)

    def test_table_renderer_keeps_the_id_column_on_a_single_line_for_copying(self) -> None:
        renderer = TableRenderer()
        long_id = "backup::" + "a" * 90

        rendered = renderer.render(
            [{"id": long_id, "name": "demo"}],
            [("id", "ID"), ("name", "NAME")],
        )

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn(long_id, lines[2])

    def test_table_renderer_keeps_any_underscore_id_column_on_a_single_line(self) -> None:
        renderer = TableRenderer()
        long_backup_id = "backup::" + "a" * 90

        rendered = renderer.render(
            [{"id": "point-1", "backup_id": long_backup_id}],
            [("id", "POINT ID"), ("backup_id", "BACKUP ID")],
        )

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn(long_backup_id, lines[2])

    def test_table_renderer_keeps_the_id_field_row_on_a_single_line_in_a_field_value_dump(self) -> None:
        renderer = TableRenderer()
        long_id = "backup::" + "a" * 90

        rendered = renderer.render(
            [{"field": "id", "value": long_id}, {"field": "name", "value": "demo"}],
            [("field", "FIELD"), ("value", "VALUE")],
        )

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn(long_id, lines[2])


if __name__ == "__main__":
    unittest.main()
