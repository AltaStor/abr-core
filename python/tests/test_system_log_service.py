# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.services import system_log_service


def _line(stamp: str, level: str, logger: str, message: str, *, pid: int = 4242) -> str:
    return f"{stamp} | {level:<8} | PID:{pid} | {logger}:some_function:12 | {message}"


class SystemLogParsingTests(unittest.TestCase):
    def test_parses_the_shipped_file_format(self) -> None:
        text = _line(
            "2026-09-04 18:09:47", "WARNING", "python.srv.core.tls_bootstrap", "TLS directory unusable"
        )

        (entry,) = system_log_service.parse_log_text(text)

        self.assertEqual(entry["level"], "warn")
        self.assertEqual(entry["logger"], "python.srv.core.tls_bootstrap")
        # The chip groups by the module that emitted the line, not its package path.
        self.assertEqual(entry["source"], "tls_bootstrap")
        self.assertEqual(entry["function"], "some_function")
        self.assertEqual(entry["line"], 12)
        self.assertEqual(entry["pid"], 4242)
        self.assertEqual(entry["message"], "TLS directory unusable")
        self.assertTrue(entry["ts"].startswith("2026-09-04T18:09:47"))
        # No timezone in the file means local time, so the record has to carry
        # the appliance's offset rather than silently claiming UTC.
        self.assertRegex(entry["ts"], r"(\+|-)\d{2}:\d{2}$")

    def test_critical_folds_into_error(self) -> None:
        (entry,) = system_log_service.parse_log_text(
            _line("2026-09-04 18:09:47", "CRITICAL", "python.srv", "boom")
        )

        self.assertEqual(entry["level"], "error")

    def test_continuation_lines_belong_to_the_record_above(self) -> None:
        text = "\n".join(
            [
                _line("2026-09-04 18:09:47", "ERROR", "python.srv.db.db_manager", "Traceback:"),
                "  File \"db_manager.py\", line 9, in connect",
                "    raise RuntimeError",
                _line("2026-09-04 18:09:48", "INFO", "python.srv", "next line"),
            ]
        )

        first, second = system_log_service.parse_log_text(text)

        self.assertIn("raise RuntimeError", first["message"])
        self.assertEqual(second["message"], "next line")

    def test_orphaned_continuation_before_any_record_is_dropped(self) -> None:
        # What a tail that cut into the middle of a traceback looks like.
        text = "    still inside a traceback\n" + _line(
            "2026-09-04 18:09:47", "INFO", "python.srv", "first real line"
        )

        entries = system_log_service.parse_log_text(text)

        self.assertEqual([e["message"] for e in entries], ["first real line"])

    def test_identical_lines_get_distinct_ids(self) -> None:
        repeated = _line("2026-09-04 18:09:47", "WARN", "python.srv", "retrying")
        entries = system_log_service.parse_log_text(repeated + "\n" + repeated)

        self.assertEqual(len({e["id"] for e in entries}), 2)


class SystemLogReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = pathlib.Path(self._tmp.name)
        patcher = patch.dict("os.environ", {"ALTASTOR_LOG_DIR": str(self.log_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name: str, lines: list[str]) -> None:
        (self.log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_returns_newest_first(self) -> None:
        self._write(
            "server.log",
            [
                _line("2026-09-04 18:00:00", "INFO", "python.srv", "older"),
                _line("2026-09-04 18:00:01", "INFO", "python.srv", "newer"),
            ],
        )

        result = system_log_service.read_system_log()

        self.assertEqual([e["message"] for e in result["entries"]], ["newer", "older"])
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["has_more"])

    def test_walks_rotations_newest_file_first(self) -> None:
        self._write("server.log.1", [_line("2026-09-04 17:00:00", "INFO", "python.srv", "rotated")])
        self._write("server.log", [_line("2026-09-04 18:00:00", "INFO", "python.srv", "current")])

        result = system_log_service.read_system_log()

        self.assertEqual([e["message"] for e in result["entries"]], ["current", "rotated"])

    def test_fault_files_are_not_parsed(self) -> None:
        self._write("server.log", [_line("2026-09-04 18:00:00", "INFO", "python.srv", "real")])
        (self.log_dir / "server.log.fault").write_text(
            "Current thread 0x00007f (most recent call first):\n", encoding="utf-8"
        )

        result = system_log_service.read_system_log()

        self.assertEqual([e["message"] for e in result["entries"]], ["real"])
        self.assertNotIn("fault", " ".join(result["files"]))

    def test_level_is_a_floor(self) -> None:
        self._write(
            "server.log",
            [
                _line("2026-09-04 18:00:00", "DEBUG", "python.srv", "debug line"),
                _line("2026-09-04 18:00:01", "INFO", "python.srv", "info line"),
                _line("2026-09-04 18:00:02", "WARNING", "python.srv", "warn line"),
                _line("2026-09-04 18:00:03", "ERROR", "python.srv", "error line"),
            ],
        )

        result = system_log_service.read_system_log(level="warn")

        self.assertEqual([e["message"] for e in result["entries"]], ["error line", "warn line"])
        self.assertEqual(result["level"], "warn")

    def test_limit_caps_the_window_and_reports_more(self) -> None:
        self._write(
            "server.log",
            [_line("2026-09-04 18:00:0%d" % i, "INFO", "python.srv", f"line {i}") for i in range(5)],
        )

        result = system_log_service.read_system_log(limit=2)

        self.assertEqual([e["message"] for e in result["entries"]], ["line 4", "line 3"])
        self.assertTrue(result["has_more"])

    def test_secrets_are_redacted(self) -> None:
        self._write(
            "server.log",
            [_line("2026-09-04 18:00:00", "INFO", "python.srv", "connecting with password=hunter2")],
        )

        (entry,) = system_log_service.read_system_log()["entries"]

        self.assertNotIn("hunter2", entry["message"])

    def test_missing_log_reports_where_it_looked(self) -> None:
        result = system_log_service.read_system_log()

        self.assertEqual(result["entries"], [])
        self.assertEqual(result["files"], [])
        self.assertIn(str(self.log_dir), result["searched"])


class SystemLogEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[require_session] = lambda: {
            "user_id": "test-user",
            "user_name": "test-user",
        }
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)

    def test_endpoint_returns_entries(self) -> None:
        response = self.client.get("/api/v1/system/logs?limit=3")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("entries", body)
        self.assertLessEqual(len(body["entries"]), 3)

    def test_endpoint_rejects_an_unknown_level(self) -> None:
        self.assertEqual(self.client.get("/api/v1/system/logs?level=chatty").status_code, 422)

    def test_endpoint_requires_a_session(self) -> None:
        app.dependency_overrides.pop(require_session, None)
        try:
            self.assertEqual(self.client.get("/api/v1/system/logs").status_code, 401)
        finally:
            app.dependency_overrides[require_session] = lambda: {
                "user_id": "test-user",
                "user_name": "test-user",
            }


if __name__ == "__main__":
    unittest.main()
