# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import logger_conf

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient


class CrashReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._reset_fault_state()
        self.addCleanup(self._reset_fault_state)

    def _reset_fault_state(self) -> None:
        if logger_conf._fault_file is not None:
            try:
                logger_conf._fault_file.close()
            except OSError:
                pass
        logger_conf._fault_file = None
        sys.excepthook = sys.__excepthook__

    def test_enable_crash_reporting_creates_fault_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "srv")

            self.assertTrue((Path(tmp) / "server.log.fault").is_file())
            self.assertIsNotNone(logger_conf._fault_file)

    def test_enable_crash_reporting_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            logger_conf._enable_crash_reporting(tmp_a, "srv")
            first_file = logger_conf._fault_file

            logger_conf._enable_crash_reporting(tmp_b, "srv")

            self.assertIs(logger_conf._fault_file, first_file)
            self.assertFalse((Path(tmp_b) / "server.log.fault").exists())

    def test_excepthook_writes_traceback_to_fault_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "cli")
            try:
                raise ValueError("boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())

            content = (Path(tmp) / "cli.log.fault").read_text(encoding="utf-8")

        self.assertIn("unhandled exception", content)
        self.assertIn("ValueError: boom", content)

    def test_interrupting_a_command_is_not_recorded_as_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "cli")
            for exc in (KeyboardInterrupt("^C"), SystemExit(1)):
                try:
                    raise exc
                except (KeyboardInterrupt, SystemExit):
                    sys.excepthook(*sys.exc_info())

            fault_file = Path(tmp) / "cli.log.fault"
            self.assertEqual(fault_file.stat().st_size, 0)

    def test_a_real_exception_after_an_interrupt_is_still_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "cli")
            try:
                raise KeyboardInterrupt("^C")
            except KeyboardInterrupt:
                sys.excepthook(*sys.exc_info())
            try:
                raise ValueError("boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())

            content = (Path(tmp) / "cli.log.fault").read_text(encoding="utf-8")

        self.assertIn("ValueError: boom", content)
        self.assertNotIn("KeyboardInterrupt", content)

    def test_enable_crash_reporting_does_not_raise_when_dir_unwritable(self) -> None:
        logger_conf._enable_crash_reporting("/nonexistent-dir-xyz/definitely-not-here", "srv")

        self.assertIsNone(logger_conf._fault_file)

    def test_clean_exit_removes_the_untouched_fault_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "srv")
            fault_path = Path(tmp) / "server.log.fault"
            self.assertTrue(fault_path.is_file())

            logger_conf._remove_fault_file_if_empty(str(fault_path))

            self.assertFalse(fault_path.exists())

    def test_a_crash_that_wrote_content_survives_the_same_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "cli")
            fault_path = Path(tmp) / "cli.log.fault"
            try:
                raise ValueError("boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())

            logger_conf._remove_fault_file_if_empty(str(fault_path))

            self.assertTrue(fault_path.is_file())
            self.assertIn("ValueError: boom", fault_path.read_text(encoding="utf-8"))

    def test_remove_fault_file_if_empty_does_not_raise_once_the_directory_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "srv")
            fault_path = str(Path(tmp) / "server.log.fault")

        logger_conf._remove_fault_file_if_empty(fault_path)

    def test_close_crash_reporting_is_the_entrypoint_the_app_calls_on_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "srv")
            fault_path = Path(tmp) / "server.log.fault"
            self.assertTrue(fault_path.is_file())

            logger_conf.close_crash_reporting()

            self.assertFalse(fault_path.exists())
            self.assertIsNone(logger_conf._fault_file)
            self.assertIsNone(logger_conf._fault_path)

    def test_close_crash_reporting_is_a_harmless_noop_when_nothing_was_enabled(self) -> None:
        logger_conf.close_crash_reporting()

    def test_close_crash_reporting_called_twice_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_conf._enable_crash_reporting(tmp, "srv")
            logger_conf.close_crash_reporting()
            logger_conf.close_crash_reporting()


class AppShutdownCallsCrashReportingCleanupTests(unittest.TestCase):
    def test_lifespan_shutdown_calls_close_crash_reporting(self) -> None:
        from python.srv.backup_server import app

        with patch("python.srv.backup_server.close_crash_reporting") as mock_close:
            with TestClient(app):
                mock_close.assert_not_called()
            mock_close.assert_called_once()


class IsWorthCollectingTests(unittest.TestCase):
    def test_non_fault_file_is_always_worth_collecting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_bytes(b"")

            self.assertTrue(logger_conf.is_worth_collecting(path))

    def test_empty_fault_file_is_not_worth_collecting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log.fault"
            path.write_bytes(b"")

            self.assertFalse(logger_conf.is_worth_collecting(path))

    def test_non_empty_fault_file_is_worth_collecting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log.fault"
            path.write_bytes(b"Fatal Python error: Segmentation fault")

            self.assertTrue(logger_conf.is_worth_collecting(path))

    def test_missing_fault_file_is_not_worth_collecting(self) -> None:
        self.assertFalse(logger_conf.is_worth_collecting("/nonexistent-really/no.fault"))


REPO_ROOT = Path(__file__).resolve().parents[4]


class CliStdoutIsolationTests(unittest.TestCase):
    """Regression guard: importing/running the CLI must not leak server logs to stdout.

    The CLI does not import anything from python.srv (it has its own copy of
    logger_conf under python.cli.core; this test exercises the behavior via
    python.srv.core's copy, which is identical), so it never triggers
    backup_server's module-level setup_logging() (console -> stdout) and
    "Serving web UI from ..." log line, which would otherwise corrupt
    `--output json` (stdout must be a single JSON document).

    Both copies resolve their repo-local dev fallback under python/logs
    (three parents up from core/logger_conf.py), each into its own
    component subdirectory (python/logs/server, python/logs/cli) - mirroring
    the same server/cli split used under /var/log/altastor-backup in
    production, so dev and prod behave the same way.
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "python.cli.main", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_importing_cli_does_not_load_server_or_write_stdout(self) -> None:
        code = (
            "import sys, python.cli.main as m;"
            "sys.stderr.write('loaded=' + str('python.srv.backup_server' in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.stdout, "", f"unexpected stdout: {proc.stdout!r}")
        self.assertIn("loaded=False", proc.stderr)

    def test_json_command_stdout_has_no_log_line(self) -> None:
        # No server is running in the test env, so the command fails to connect;
        # the point is that stdout stays clean of the "Serving web UI" log line
        # regardless of outcome.
        proc = self._run("repo", "list", "--output", "json")
        self.assertNotIn("Serving web UI", proc.stdout)
        self.assertNotIn("INFO", proc.stdout)
        if proc.stdout.strip():
            # Whatever lands on stdout in JSON mode must parse as JSON.
            json.loads(proc.stdout)


class LoggerConfigTests(unittest.TestCase):
    def test_resolve_log_dir_uses_explicit_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"ALTASTOR_LOG_DIR": tmpdir}, clear=False):
                self.assertEqual(logger_conf._resolve_log_dir(), tmpdir)
                self.assertTrue(Path(tmpdir).is_dir())

    def test_resolve_log_dir_splits_system_log_dir_by_component(self) -> None:
        srv_dir = os.path.join(logger_conf.SYSTEM_LOG_DIR, "server")
        cli_dir = os.path.join(logger_conf.SYSTEM_LOG_DIR, "cli")

        def fake_prepare(path: str) -> bool:
            return path in (srv_dir, cli_dir)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALTASTOR_LOG_DIR", None)
            with patch.object(logger_conf, "_prepare_log_dir", side_effect=fake_prepare):
                self.assertEqual(logger_conf._resolve_log_dir("srv"), srv_dir)
                self.assertEqual(logger_conf._resolve_log_dir("cli"), cli_dir)

    def test_resolve_log_dir_prefers_per_user_state_dir_over_shared_tmp(self) -> None:
        real_prepare = logger_conf._prepare_log_dir
        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ, {"HOME": home}, clear=False
        ):
            os.environ.pop("XDG_STATE_HOME", None)
            os.environ.pop("ALTASTOR_LOG_DIR", None)
            state_dir = logger_conf._user_state_log_dir()

            def fake_prepare(path: str) -> bool:
                if path == state_dir:
                    return real_prepare(path)
                return False

            with patch.object(logger_conf, "_prepare_log_dir", side_effect=fake_prepare):
                resolved = logger_conf._resolve_log_dir()

        self.assertEqual(resolved, state_dir)
        self.assertNotIn("/tmp/altastor-logs", resolved)

    def test_two_different_users_get_independent_state_dirs(self) -> None:
        real_prepare = logger_conf._prepare_log_dir

        def resolve_for_home(home: str) -> str:
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                os.environ.pop("XDG_STATE_HOME", None)
                os.environ.pop("ALTASTOR_LOG_DIR", None)
                state_dir = logger_conf._user_state_log_dir()

                def fake_prepare(path: str) -> bool:
                    if path == state_dir:
                        return real_prepare(path)
                    return False

                with patch.object(
                    logger_conf, "_prepare_log_dir", side_effect=fake_prepare
                ):
                    return logger_conf._resolve_log_dir()

        with tempfile.TemporaryDirectory() as home_a, tempfile.TemporaryDirectory() as home_b:
            resolved_a = resolve_for_home(home_a)
            resolved_b = resolve_for_home(home_b)

            self.assertTrue(Path(resolved_a).is_dir())
            self.assertTrue(Path(resolved_b).is_dir())
            self.assertNotEqual(resolved_a, resolved_b)

    def test_shared_tmp_fallback_self_heals_a_locked_parent(self) -> None:
        parent = os.path.join("/tmp", "altastor-logs")
        self.addCleanup(lambda: __import__("shutil").rmtree(parent, ignore_errors=True))
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o755)

        result = logger_conf._shared_tmp_fallback_dir()

        self.assertTrue(Path(result).is_dir())
        self.assertEqual(os.stat(parent).st_mode & 0o1777, 0o1777)

    def test_shared_tmp_fallback_raises_clear_error_when_unrecoverable(self) -> None:
        parent = os.path.join("/tmp", "altastor-logs")
        uid_dir = os.path.join(parent, str(os.getuid()))

        def fake_makedirs(path: str, exist_ok: bool = False) -> None:
            if path == uid_dir:
                raise PermissionError(13, "Permission denied")

        with patch.object(
            logger_conf.os, "chmod", side_effect=PermissionError(1, "not owner")
        ), patch.object(
            logger_conf.os, "makedirs", side_effect=fake_makedirs
        ), self.assertRaises(RuntimeError) as ctx:
            logger_conf._shared_tmp_fallback_dir()

        self.assertIn(uid_dir, str(ctx.exception))
        self.assertIn("ALTASTOR_LOG_DIR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
