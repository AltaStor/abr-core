# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from python.cli.errors import CliStructuredError
from python.cli.services import system_service


class _FakeApiClient:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.calls: list[tuple[str, dict | None]] = []

    def post(self, path: str, json: dict | None = None):
        self.calls.append((path, json))
        return self._response


class _FakeContext:
    def __init__(self, response: dict) -> None:
        self.api_client = _FakeApiClient(response)


def _build_zip(manifest: dict, extra_files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, content in (extra_files or {}).items():
            archive.writestr(arcname, content)
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


class MergeLocalDiagnosticsIntoBundleTests(unittest.TestCase):
    def _merge(self, archive_bytes: bytes, manifest: dict, *, logs=()):
        with patch(
            "python.cli.services.system_service._find_local_cli_logs", return_value=list(logs)
        ):
            return system_service._merge_local_diagnostics_into_bundle(archive_bytes, manifest)

    def test_returns_original_bytes_unchanged_when_nothing_local_is_found(self) -> None:
        manifest = {"files": ["manifest.json"], "warnings": []}
        archive_bytes = _build_zip(manifest)

        result_bytes, result_manifest = self._merge(archive_bytes, manifest)

        self.assertEqual(result_bytes, archive_bytes)
        self.assertEqual(result_manifest, manifest)

    def test_merges_local_cli_log_under_its_own_prefix_and_keeps_it_on_disk(self) -> None:
        manifest = {"files": ["manifest.json", "logs/cli/cli.log"], "warnings": []}
        archive_bytes = _build_zip(manifest, {"logs/cli/cli.log": b"server-host cli log"})

        with tempfile.TemporaryDirectory() as log_dir:
            log_file = Path(log_dir) / "cli.log"
            log_file.write_bytes(b"my own machine's cli log")

            result_bytes, result_manifest = self._merge(archive_bytes, manifest, logs=[log_file])

            self.assertTrue(log_file.exists(), "a live log must never be deleted")

        self.assertIn("logs/cli-local/cli.log", result_manifest["files"])
        with zipfile.ZipFile(io.BytesIO(result_bytes)) as archive:
            self.assertEqual(archive.read("logs/cli-local/cli.log"), b"my own machine's cli log")
            self.assertEqual(archive.read("logs/cli/cli.log"), b"server-host cli log")

    def test_local_cli_log_identical_to_the_servers_copy_is_not_duplicated(self) -> None:
        manifest = {"files": ["manifest.json", "logs/cli/cli.log"], "warnings": []}
        archive_bytes = _build_zip(manifest, {"logs/cli/cli.log": b"same machine, same file"})

        with tempfile.TemporaryDirectory() as log_dir:
            log_file = Path(log_dir) / "cli.log"
            log_file.write_bytes(b"same machine, same file")

            result_bytes, result_manifest = self._merge(archive_bytes, manifest, logs=[log_file])

        self.assertNotIn("logs/cli-local/cli.log", result_manifest["files"])
        with zipfile.ZipFile(io.BytesIO(result_bytes)) as archive:
            self.assertNotIn("logs/cli-local/cli.log", archive.namelist())
            self.assertEqual(archive.read("logs/cli/cli.log"), b"same machine, same file")

    def test_local_cli_log_secrets_are_masked(self) -> None:
        manifest = {"files": ["manifest.json"], "warnings": []}
        archive_bytes = _build_zip(manifest)

        with tempfile.TemporaryDirectory() as log_dir:
            log_file = Path(log_dir) / "cli.log"
            log_file.write_bytes(b'login failed: {"password": "hunter2"}')

            result_bytes, _ = self._merge(archive_bytes, manifest, logs=[log_file])

        with zipfile.ZipFile(io.BytesIO(result_bytes)) as archive:
            content = archive.read("logs/cli-local/cli.log").decode("utf-8")
        self.assertNotIn("hunter2", content)
        self.assertIn("********", content)

    def test_local_cli_log_is_tailed_not_shipped_whole(self) -> None:
        manifest = {"files": ["manifest.json"], "warnings": []}
        archive_bytes = _build_zip(manifest)

        with tempfile.TemporaryDirectory() as log_dir:
            log_file = Path(log_dir) / "cli.log"
            log_file.write_bytes(b"OLD" * 100 + b"NEWEST")

            with patch("python.cli.services.system_service._LOCAL_LOG_TAIL_BYTES", 6):
                result_bytes, _ = self._merge(archive_bytes, manifest, logs=[log_file])

        with zipfile.ZipFile(io.BytesIO(result_bytes)) as archive:
            self.assertEqual(archive.read("logs/cli-local/cli.log"), b"NEWEST")

class FindLocalCliLogsTests(unittest.TestCase):
    def test_returns_cli_logs_including_rotations_and_a_non_empty_fault_file(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            (Path(log_dir) / "cli.log").write_bytes(b"current")
            (Path(log_dir) / "cli.log.1").write_bytes(b"rotated")
            (Path(log_dir) / "cli.log.fault").write_bytes(b"Fatal Python error: Segmentation fault")

            with patch(
                "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
            ):
                found = system_service._find_local_cli_logs()

        self.assertEqual([p.name for p in found], ["cli.log", "cli.log.1", "cli.log.fault"])

    def test_an_empty_fault_file_is_left_out_so_its_presence_still_means_something(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            (Path(log_dir) / "cli.log").write_bytes(b"current")
            (Path(log_dir) / "cli.log.fault").write_bytes(b"")

            with patch(
                "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
            ):
                found = system_service._find_local_cli_logs()

        self.assertEqual([p.name for p in found], ["cli.log"])

    def test_returns_empty_list_when_log_dir_missing(self) -> None:
        with patch(
            "python.cli.services.system_service._resolve_log_dir",
            return_value="/nonexistent-really/does-not-matter",
        ):
            self.assertEqual(system_service._find_local_cli_logs(), [])


class DefaultDestinationTests(unittest.TestCase):
    def test_uses_current_directory_when_it_is_writable(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            with patch("python.cli.services.system_service.Path.cwd", return_value=Path(cwd)):
                destination = system_service._default_support_bundle_destination()

        self.assertEqual(destination.parent, Path(cwd))
        self.assertTrue(destination.name.startswith("support-bundle-"))

    def test_falls_back_to_home_when_current_directory_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch(
                "python.cli.services.system_service.Path.cwd",
                return_value=Path("/nonexistent-really/not-writable"),
            ), patch(
                "python.cli.services.system_service.os.path.expanduser", return_value=home
            ):
                destination = system_service._default_support_bundle_destination()

        self.assertEqual(destination.parent, Path(home))

    def test_falls_back_to_tmp_when_neither_is_writable(self) -> None:
        with patch(
            "python.cli.services.system_service.Path.cwd",
            return_value=Path("/nonexistent-really/not-writable"),
        ), patch("python.cli.services.system_service.os.path.expanduser", return_value="~"):
            destination = system_service._default_support_bundle_destination()

        self.assertEqual(destination.parent, Path(tempfile.gettempdir()))

    def test_get_support_bundle_writes_to_the_resolved_default(self) -> None:
        manifest = {"files": ["manifest.json"], "warnings": []}
        archive_bytes = _build_zip(manifest)
        response = {
            "manifest": manifest,
            "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
        }

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as log_dir:
            with patch(
                "python.cli.services.system_service.Path.cwd",
                return_value=Path("/nonexistent-really/not-writable"),
            ), patch(
                "python.cli.services.system_service.os.path.expanduser", return_value=home
            ), patch(
                "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
            ):
                service = system_service.SystemService(_FakeContext(response))
                result = service.get_support_bundle(file=None)

            written = list(Path(home).glob("support-bundle-*.zip"))
            self.assertEqual(len(written), 1)
            self.assertEqual(Path(result["file"]), written[0].resolve())


class GetSupportBundleMergeTests(unittest.TestCase):
    def test_get_support_bundle_merges_a_remote_machines_crash_traceback(self) -> None:
        # This is the whole point of not shipping raw core dumps: a CLI crash
        # on a machine the server never sees still has to leave something
        # readable in the bundle, and this - the Python-level traceback,
        # masked like any other log - is what fills that gap.
        manifest = {"files": ["manifest.json"], "warnings": []}
        archive_bytes = _build_zip(manifest)
        response = {
            "manifest": manifest,
            "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
        }

        with tempfile.TemporaryDirectory() as log_dir, tempfile.TemporaryDirectory() as out_dir:
            fault_file = Path(log_dir) / "cli.log.fault"
            fault_file.write_bytes(
                b'Fatal Python error: Segmentation fault\nrepository password="hunter2"'
            )
            destination = str(Path(out_dir) / "bundle.zip")

            with patch(
                "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
            ):
                service = system_service.SystemService(_FakeContext(response))
                result = service.get_support_bundle(file=destination)

            self.assertTrue(fault_file.is_file(), "a live crash record must never be deleted")

            self.assertIn("logs/cli-local/cli.log.fault", result["manifest"]["files"])
            with zipfile.ZipFile(destination) as archive:
                content = archive.read("logs/cli-local/cli.log.fault").decode("utf-8")
            self.assertIn("Segmentation fault", content)
            self.assertNotIn("hunter2", content)


class ArchiveWriteFailureTests(unittest.TestCase):
    def _service(self):
        manifest = {"files": ["manifest.json"], "warnings": []}
        return system_service.SystemService(
            _FakeContext(
                {
                    "manifest": manifest,
                    "archive_base64": base64.b64encode(_build_zip(manifest)).decode("ascii"),
                }
            )
        )

    def test_unwritable_destination_raises_a_structured_error_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir, tempfile.TemporaryDirectory() as log_dir:
            destination = str(Path(out_dir) / "bundle.zip")

            with patch(
                "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
            ), patch(
                "python.cli.services.system_service.Path.write_bytes",
                side_effect=PermissionError(13, "Permission denied"),
            ), self.assertRaises(CliStructuredError) as caught:
                self._service().get_support_bundle(file=destination)

        error = caught.exception
        self.assertEqual(error.exit_code, 1)
        self.assertNotEqual(error.code, "runtime_error", "runtime_error logs a full traceback")
        self.assertIn(destination, error.message)
        self.assertIn("Permission denied", error.message)
        self.assertNotIn("Traceback", error.message)
        self.assertNotIn(".py", error.message)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions"
    )
    def test_a_real_read_only_directory_is_reported_the_same_way(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir, tempfile.TemporaryDirectory() as log_dir:
            read_only = Path(out_dir) / "read-only"
            read_only.mkdir()
            read_only.chmod(0o500)
            try:
                with patch(
                    "python.cli.services.system_service._resolve_log_dir", return_value=log_dir
                ), self.assertRaises(CliStructuredError) as caught:
                    self._service().get_support_bundle(file=str(read_only / "bundle.zip"))
            finally:
                read_only.chmod(0o700)

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertIn("Permission denied", caught.exception.message)

    def test_the_catalog_export_reports_a_write_failure_the_same_way(self) -> None:
        service = system_service.SystemService(
            _FakeContext({"archive_base64": base64.b64encode(b"PK").decode("ascii")})
        )

        with tempfile.TemporaryDirectory() as out_dir:
            destination = str(Path(out_dir) / "export.enc")

            with patch(
                "python.cli.services.system_service.Path.write_bytes",
                side_effect=PermissionError(13, "Permission denied"),
            ), self.assertRaises(CliStructuredError) as caught:
                service.export_catalog(file=destination, password="hunter2hunter2")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertIn("catalog export", caught.exception.message)
        self.assertNotIn("hunter2", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
