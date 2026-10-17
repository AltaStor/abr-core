# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.cli.services.server_defaults_template import render_recovered_server_defaults
from python.srv.core import server_defaults
from python.srv.core.server_defaults import (
    DEFAULTS_ENV_PATH,
    apply_server_defaults,
    env_int,
    parse_defaults_env,
)


class ParseDefaultsEnvTests(unittest.TestCase):
    def test_parses_key_value_pairs_ignoring_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defaults.env"
            path.write_text(
                "\n".join(
                    [
                        "# a comment",
                        "",
                        "FOO=bar",
                        "  BAZ = 42  ",
                        "# QUUX=ignored",
                    ]
                ),
                encoding="utf-8",
            )
            values = parse_defaults_env(path)

        self.assertEqual(values, {"FOO": "bar", "BAZ": "42"})

    def test_missing_file_raises_a_clear_error_naming_the_path(self) -> None:
        missing = Path(tempfile.gettempdir()) / "does-not-exist-defaults.env"
        with self.assertRaises(RuntimeError) as ctx:
            parse_defaults_env(missing)
        self.assertIn(str(missing), str(ctx.exception))

    def test_malformed_line_raises_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defaults.env"
            path.write_text("FOO=bar\nNOT_A_KEY_VALUE_LINE\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                parse_defaults_env(path)
        self.assertIn("NOT_A_KEY_VALUE_LINE", str(ctx.exception))

    def test_empty_file_raises_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defaults.env"
            path.write_text("# only comments\n\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                parse_defaults_env(path)


class ApplyServerDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        shipped = patch.object(server_defaults, "_DEFAULTS", server_defaults._DEFAULTS)
        shipped.start()
        self.addCleanup(shipped.stop)

    def test_environment_value_wins_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defaults.env"
            path.write_text("SOME_TEST_SETTING=from-file\n", encoding="utf-8")
            with patch.dict("os.environ", {"SOME_TEST_SETTING": "from-env"}):
                apply_server_defaults(path)
                self.assertEqual(os.environ["SOME_TEST_SETTING"], "from-env")

    def test_unset_variable_gets_the_file_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defaults.env"
            path.write_text("ANOTHER_TEST_SETTING=from-file\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("ANOTHER_TEST_SETTING", None)
                apply_server_defaults(path)
                self.assertEqual(os.environ["ANOTHER_TEST_SETTING"], "from-file")

    def test_the_real_defaults_env_file_is_present_and_well_formed(self) -> None:
        values = parse_defaults_env(DEFAULTS_ENV_PATH)
        self.assertIn("BACKUP_WORKER_COUNT", values)
        self.assertNotIn("MONGODB_URI", values)
        self.assertEqual(values["ABR_BOOTSTRAP_ADMIN_PASSWORD"], "admin")


class EnvIntTests(unittest.TestCase):
    def test_returns_the_parsed_integer(self) -> None:
        with patch.dict("os.environ", {"SOME_INT_SETTING": "7"}):
            self.assertEqual(env_int("SOME_INT_SETTING"), 7)

    def test_raises_a_clear_error_on_non_integer_values(self) -> None:
        with patch.dict("os.environ", {"SOME_INT_SETTING": "not-a-number"}):
            with self.assertRaises(RuntimeError) as ctx:
                env_int("SOME_INT_SETTING")
        self.assertIn("SOME_INT_SETTING", str(ctx.exception))

    def test_raises_when_below_the_configured_minimum(self) -> None:
        with patch.dict("os.environ", {"SOME_INT_SETTING": "0"}):
            with self.assertRaises(RuntimeError):
                env_int("SOME_INT_SETTING", minimum=1)


class RenderRecoveredServerDefaultsTests(unittest.TestCase):
    def test_substitutes_recovered_values_into_the_defaults_env_skeleton(self) -> None:
        rendered = render_recovered_server_defaults({"BACKUP_WORKER_COUNT": "8"})
        self.assertIn("BACKUP_WORKER_COUNT=8", rendered)
        self.assertNotIn("BACKUP_WORKER_COUNT=4", rendered)

    def test_unrecognized_keys_are_appended_under_the_extra_settings_header(self) -> None:
        rendered = render_recovered_server_defaults({"ALTASTOR_SOME_FUTURE_SETTING": "9443"})
        self.assertIn("# --- Additional recovered settings", rendered)
        self.assertIn("ALTASTOR_SOME_FUTURE_SETTING=9443", rendered)

    def test_keys_absent_from_configuration_keep_the_defaults_env_value(self) -> None:
        rendered = render_recovered_server_defaults({})
        self.assertIn("BACKUP_WORKER_COUNT=4", rendered)

    def test_bootstrap_admin_password_is_redacted_not_written_in_plain(self) -> None:
        rendered = render_recovered_server_defaults(
            {"ABR_BOOTSTRAP_ADMIN_PASSWORD": "TotallySecretPW123"}
        )
        self.assertNotIn("TotallySecretPW123", rendered)
        self.assertIn("ABR_BOOTSTRAP_ADMIN_PASSWORD=********", rendered)

    def test_mongodb_uri_credentials_are_redacted(self) -> None:
        rendered = render_recovered_server_defaults(
            {"MONGODB_URI": "mongodb://dbuser:dbpass@127.0.0.1:27018/abr"}
        )
        self.assertNotIn("dbuser:dbpass", rendered)
        self.assertIn("mongodb://********:********@127.0.0.1:27018/abr", rendered)


if __name__ == "__main__":
    unittest.main()
