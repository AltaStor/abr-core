# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
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
from python.srv.services import support_bundle_service


class SupportBundleServiceUnitTests(unittest.TestCase):
    def _generate(
        self,
        *,
        server_defaults_text: str | None = None,
        mongodb_defaults_text: str | None = None,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            candidates: list[pathlib.Path] = []

            if server_defaults_text is not None:
                server_file = tmp_path / "altastor-backup-server.default"
                server_file.write_text(server_defaults_text, encoding="utf-8")
                candidates.append(server_file)
            else:
                candidates.append(tmp_path / "does-not-exist-server.default")

            patches = [
                patch(
                    "python.srv.services.support_bundle_service._server_configuration_file_candidates",
                    return_value=candidates,
                ),
                patch(
                    "python.srv.services.support_bundle_service._resolve_log_dir",
                    return_value=str(tmp_path / "empty-logs"),
                ),
                patch(
                    "python.srv.services.support_bundle_service._run_tool", return_value=None
                ),
            ]

            if mongodb_defaults_text is not None:
                mongodb_file = tmp_path / "mongod.conf"
                mongodb_file.write_text(mongodb_defaults_text, encoding="utf-8")
            else:
                mongodb_file = tmp_path / "does-not-exist-mongod.conf"
            patches.append(
                patch(
                    "python.srv.services.support_bundle_service._mongodb_configuration_file",
                    return_value=mongodb_file,
                )
            )
            for p in patches:
                p.start()
            try:
                return support_bundle_service.generate_support_bundle()
            finally:
                for p in patches:
                    p.stop()

    def _archive(self, result: dict) -> zipfile.ZipFile:
        archive_bytes = base64.b64decode(result["archive_base64"])
        return zipfile.ZipFile(io.BytesIO(archive_bytes))

    def test_manifest_has_expected_shape(self) -> None:
        result = self._generate()
        manifest = result["manifest"]

        self.assertEqual(manifest["product"], "AltaStor Backup & Replication")
        self.assertIn("generated_at", manifest)
        self.assertIsInstance(manifest["files"], list)
        self.assertIsInstance(manifest["warnings"], list)

    def test_zip_always_contains_manifest_and_system_info(self) -> None:
        result = self._generate()
        with self._archive(result) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("system-info.json", names)

    def test_missing_logs_and_config_are_warned_not_fatal(self) -> None:
        result = self._generate()
        manifest = result["manifest"]

        self.assertTrue(any("server logs" in w.lower() for w in manifest["warnings"]))
        self.assertTrue(any("configuration file not found" in w.lower() for w in manifest["warnings"]))

    def test_cli_logs_are_found_via_the_same_resolver_setup_logging_uses(self) -> None:
        with tempfile.TemporaryDirectory() as srv_tmp, tempfile.TemporaryDirectory() as cli_tmp:
            (pathlib.Path(cli_tmp) / "cli.log").write_text("cli log content", encoding="utf-8")

            def fake_resolve_log_dir(component: str = "srv", *, notes=None) -> str:
                return cli_tmp if component == "cli" else srv_tmp

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                side_effect=fake_resolve_log_dir,
            ), patch(
                "python.srv.services.support_bundle_service._server_configuration_file_candidates",
                return_value=[pathlib.Path(srv_tmp) / "does-not-exist.default"],
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=pathlib.Path(srv_tmp) / "does-not-exist-mongodb.default",
            ):
                result = support_bundle_service.generate_support_bundle()

        self.assertIn("logs/cli/cli.log", result["manifest"]["files"])
        with self._archive(result) as archive:
            self.assertEqual(archive.read("logs/cli/cli.log").decode("utf-8"), "cli log content")

    def test_server_config_is_included_and_password_is_redacted(self) -> None:
        result = self._generate(
            server_defaults_text=(
                "ABR_BOOTSTRAP_ADMIN_USER=admin\n"
                "ABR_BOOTSTRAP_ADMIN_PASSWORD=TotallySecretPW123\n"
                "BACKUP_WORKER_COUNT=4\n"
            )
        )
        with self._archive(result) as archive:
            self.assertIn("config/altastor-backup-server.default", archive.namelist())
            text = archive.read("config/altastor-backup-server.default").decode("utf-8")

        self.assertNotIn("TotallySecretPW123", text)
        self.assertIn("ABR_BOOTSTRAP_ADMIN_PASSWORD=********", text)
        self.assertIn("BACKUP_WORKER_COUNT=4", text)
        self.assertIn("ABR_BOOTSTRAP_ADMIN_USER=admin", text)

    def test_mongodb_uri_credentials_are_redacted(self) -> None:
        result = self._generate(
            mongodb_defaults_text=(
                "MONGO_BIND_IP=127.0.0.1\n"
                "MONGO_PORT=27018\n"
            ),
            server_defaults_text="MONGODB_URI=mongodb://dbuser:dbpass@127.0.0.1:27018/abr\n",
        )
        with self._archive(result) as archive:
            text = archive.read("config/altastor-backup-server.default").decode("utf-8")

        self.assertNotIn("dbuser:dbpass", text)
        self.assertIn("mongodb://********:********@127.0.0.1:27018/abr", text)

    def test_no_secret_bytes_survive_anywhere_in_the_archive(self) -> None:
        result = self._generate(
            server_defaults_text=(
                "ABR_BOOTSTRAP_ADMIN_PASSWORD=SuperSecretValue999\n"
                "MONGODB_URI=mongodb://au:ap@host:27018/db\n"
            )
        )
        archive_bytes = base64.b64decode(result["archive_base64"])
        self.assertNotIn(b"SuperSecretValue999", archive_bytes)
        self.assertNotIn(b"au:ap@", archive_bytes)

    def test_inventory_token_is_redacted_in_the_catalog_section(self) -> None:
        from python.srv.core.mongo_db_wrapper import HYPERVISORS
        from python.srv.services import abr_v41

        inventory = abr_v41.create_inventory_record(
            {
                "name": "probe-token",
                "type": "vsphere",
                "address": "10.255.255.9",
                "username": "probe-user",
                "password": "Canary-Password-8c07",
                "token": "Canary-Token-4d91ac",
                "insecure": True,
            }
        )
        self.addCleanup(HYPERVISORS.pop, inventory["id"], None)

        result = self._generate(server_defaults_text="BACKUP_WORKER_COUNT=4\n")

        archive_bytes = base64.b64decode(result["archive_base64"])
        self.assertNotIn(b"Canary-Token-4d91ac", archive_bytes)
        self.assertNotIn(b"Canary-Password-8c07", archive_bytes)
        with self._archive(result) as archive:
            inventories = json.loads(archive.read("catalog/inventories.json"))
        probe = next(item for item in inventories if item["id"] == inventory["id"])
        self.assertEqual(probe["metadata"]["token"], "********")
        self.assertEqual(probe["metadata"]["credentials"]["password"], "********")

    def test_smb_repository_password_is_redacted_in_the_catalog_section(self) -> None:
        from python.srv.core.mongo_db_wrapper import REPOSITORIES
        from python.srv.services import abr_v41

        repository = abr_v41.create_repository_record(
            {
                "name": "probe-smb",
                "type": "smb",
                "server": "10.255.255.9",
                "share": "backups",
                "username": "probe-user",
                "password": "Canary-Password-9f3b21",
            }
        )
        self.addCleanup(REPOSITORIES.pop, repository["id"], None)

        result = self._generate(server_defaults_text="BACKUP_WORKER_COUNT=4\n")

        archive_bytes = base64.b64decode(result["archive_base64"])
        self.assertNotIn(b"Canary-Password-9f3b21", archive_bytes)
        with self._archive(result) as archive:
            repositories = json.loads(archive.read("catalog/repositories.json"))
        probe = next(item for item in repositories if item["id"] == repository["id"])
        self.assertEqual(probe["metadata"]["password"], "********")

    def test_files_list_in_manifest_matches_actual_zip_members_exactly(self) -> None:
        result = self._generate(server_defaults_text="BACKUP_WORKER_COUNT=4\n")
        with self._archive(result) as archive:
            actual = archive.namelist()

        listed = result["manifest"]["files"]
        self.assertIn("manifest.json", listed)
        self.assertEqual(set(listed), set(actual))
        self.assertEqual(len(listed), len(actual))

    def test_server_config_candidates_check_the_v1_0_path_first(self) -> None:
        from python.srv.services import abr_v41

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(abr_v41.SERVER_DEFAULTS_FILE_ENV_VAR, None)
            candidates = [str(p) for p in abr_v41._server_defaults_file_candidates()]

        self.assertEqual(candidates[0], "/etc/altastor-backup/server.conf")
        self.assertIn("/etc/default/altastor-backup-server", candidates)

    def test_support_bundle_config_candidates_fall_back_to_defaults_env(self) -> None:
        from python.srv.core.server_defaults import DEFAULTS_ENV_PATH

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ABR_SERVER_DEFAULTS_FILE", None)
            candidates = [
                str(p) for p in support_bundle_service._server_configuration_file_candidates()
            ]

        self.assertEqual(candidates[0], "/etc/altastor-backup/server.conf")
        self.assertEqual(candidates[-1], str(DEFAULTS_ENV_PATH))
        self.assertNotIn("altastor-backup-server.default", candidates[-1])

    def test_support_bundle_includes_defaults_env_when_no_deployed_config_exists(self) -> None:
        from python.srv.core.server_defaults import DEFAULTS_ENV_PATH

        with tempfile.TemporaryDirectory() as tmp, patch(
            "python.srv.services.support_bundle_service._server_configuration_file_candidates",
            return_value=[pathlib.Path(tmp) / "does-not-exist-server.conf", DEFAULTS_ENV_PATH],
        ):
            warnings: list[str] = []
            files = support_bundle_service._collect_configuration(warnings)

        names = {name for name, _ in files}
        self.assertIn("config/defaults.env", names)
        text = dict(files)["config/defaults.env"].decode("utf-8")
        self.assertIn("BACKUP_WORKER_COUNT=", text)

    def test_the_mongodb_config_path_comes_from_the_unit_mongod_actually_runs(self) -> None:
        with patch(
            "python.srv.services.support_bundle_service._unit_property",
            return_value="{ path=/usr/bin/mongod ; argv[]=/usr/bin/mongod "
            "--config /etc/altastor-backup/mongod.conf ; ignore_errors=no }",
        ):
            self.assertEqual(
                str(support_bundle_service._mongodb_configuration_file()),
                "/etc/altastor-backup/mongod.conf",
            )

    def test_the_mongodb_config_falls_back_to_the_packaged_path_without_systemd(self) -> None:
        with patch(
            "python.srv.services.support_bundle_service._unit_property", return_value=""
        ), patch.object(support_bundle_service.Path, "is_file", return_value=False):
            self.assertEqual(
                str(support_bundle_service._mongodb_configuration_file()),
                "/etc/altastor-backup/mongod.conf",
            )

    def test_package_versions_are_listed_in_the_manifest(self) -> None:
        dpkg_output = (
            "altastor-backup-mongodb-config 8.0.26+4\n"
            "altastor-backup-server 1.0+37.g39d04b7cae77\n"
            "altastor-backup-webui 1.0+24.g691e43457344\n"
        )

        with patch(
            "python.srv.services.support_bundle_service._run_tool", return_value=dpkg_output
        ):
            self.assertEqual(
                support_bundle_service._package_versions(),
                {
                    "altastor-backup-mongodb-config": "8.0.26+4",
                    "altastor-backup-server": "1.0+37.g39d04b7cae77",
                    "altastor-backup-webui": "1.0+24.g691e43457344",
                },
            )

    def test_the_manifest_omits_package_versions_when_dpkg_is_unavailable(self) -> None:
        with patch(
            "python.srv.services.support_bundle_service._package_versions", return_value={}
        ):
            result = self._generate()

        self.assertNotIn("package_versions", result["manifest"])
        self.assertFalse([w for w in result["manifest"]["warnings"] if "package" in w.lower()])

    def test_the_manifest_carries_package_versions_when_dpkg_answers(self) -> None:
        with patch(
            "python.srv.services.support_bundle_service._package_versions",
            return_value={"altastor-backup-server": "1.0+37.g39d04b7cae77"},
        ):
            result = self._generate()

        self.assertEqual(
            result["manifest"]["package_versions"],
            {"altastor-backup-server": "1.0+37.g39d04b7cae77"},
        )

    def test_systemd_units_and_drop_ins_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            unit = tmp_path / "altastor-backup-server.service"
            unit.write_text("[Service]\nUser=altastor-backup\n", encoding="utf-8")
            drop_in_dir = tmp_path / "mongod.service.d"
            drop_in_dir.mkdir()
            drop_in = drop_in_dir / "altastor-backup.conf"
            drop_in.write_text("[Service]\nExecStart=/usr/bin/mongod\n", encoding="utf-8")

            def unit_files(name: str):
                return [unit] if name.startswith("altastor") else [drop_in]

            with patch(
                "python.srv.services.support_bundle_service._systemd_unit_files",
                side_effect=unit_files,
            ):
                files = dict(support_bundle_service._collect_systemd_units())

        self.assertIn("config/systemd/altastor-backup-server.service", files)
        self.assertIn("config/systemd/mongod.service.d/altastor-backup.conf", files)
        self.assertIn(b"User=altastor-backup", files["config/systemd/altastor-backup-server.service"])

    def test_a_secret_in_a_unit_file_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unit = pathlib.Path(tmp) / "altastor-backup-server.service"
            unit.write_text(
                "[Service]\n"
                "Environment=ABR_BOOTSTRAP_ADMIN_PASSWORD=TotallySecretPW123\n"
                "Environment=ABR_TLS_KEY_FILE=/etc/altastor-backup/tls/server.key\n",
                encoding="utf-8",
            )

            with patch(
                "python.srv.services.support_bundle_service._systemd_unit_files",
                side_effect=lambda name: [unit] if name.startswith("altastor") else [],
            ):
                files = dict(support_bundle_service._collect_systemd_units())

        text = files["config/systemd/altastor-backup-server.service"].decode("utf-8")
        self.assertNotIn("TotallySecretPW123", text)
        self.assertIn("Environment=ABR_BOOTSTRAP_ADMIN_PASSWORD=********", text)
        self.assertIn("ABR_TLS_KEY_FILE=/etc/altastor-backup/tls/server.key", text)

    def test_no_systemd_means_no_files_and_no_warnings(self) -> None:
        with patch("python.srv.services.support_bundle_service._run_tool", return_value=None):
            self.assertEqual(support_bundle_service._collect_systemd_units(), [])

    def test_mongodb_log_path_is_read_from_the_yaml_mongod_conf(self) -> None:
        text = (
            "storage:\n"
            "  dbPath: /var/lib/altastor-backup/mongodb\n"
            "systemLog:\n"
            "  destination: file\n"
            "  logAppend: true\n"
            "  path: /var/log/mongodb/mongod.log\n"
            "net:\n"
            "  port: 27018\n"
        )

        self.assertEqual(
            support_bundle_service._mongodb_log_path_from_text(text),
            "/var/log/mongodb/mongod.log",
        )

    def test_mongodb_log_path_ignores_paths_outside_the_system_log_block(self) -> None:
        text = (
            "systemLog:\n"
            "  destination: file\n"
            "  path: /var/log/mongodb/mongod.log\n"
            "auditLog:\n"
            "  path: /var/log/mongodb/audit.log\n"
        )

        self.assertEqual(
            support_bundle_service._mongodb_log_path_from_text(text),
            "/var/log/mongodb/mongod.log",
        )

    def test_mongodb_log_path_accepts_quotes_and_inline_comments(self) -> None:
        text = (
            "systemLog:\n"
            '  path: "/var/log/mongodb/mongod.log"  # written by the installer\n'
        )

        self.assertEqual(
            support_bundle_service._mongodb_log_path_from_text(text),
            "/var/log/mongodb/mongod.log",
        )

    def test_mongodb_log_path_falls_back_when_the_config_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "mongod.conf"
            config.write_text("net:\n  port: 27018\n", encoding="utf-8")

            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                self.assertEqual(
                    support_bundle_service._mongodb_log_path(),
                    support_bundle_service._DEFAULT_MONGODB_LOG_PATH,
                )

    def test_unreadable_mongod_conf_falls_back_to_mongodbs_own_default_location(self) -> None:
        self.assertEqual(
            support_bundle_service._DEFAULT_MONGODB_LOG_PATH, "/var/log/mongodb/mongod.log"
        )

        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "mongod.conf"
            config.write_text("systemLog:\n  path: /var/log/mongodb/mongod.log\n", encoding="utf-8")

            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ), patch.object(
                support_bundle_service.Path, "read_text", side_effect=PermissionError("denied")
            ):
                self.assertEqual(
                    support_bundle_service._mongodb_log_path(), "/var/log/mongodb/mongod.log"
                )

    def test_mongod_conf_is_collected_into_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "mongod.conf"
            config.write_text(
                "systemLog:\n  path: /var/log/mongodb/mongod.log\nnet:\n  port: 27017\n",
                encoding="utf-8",
            )

            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                warnings: list[str] = []
                files = support_bundle_service._collect_mongodb_configuration(warnings)

        self.assertEqual([name for name, _ in files], ["config/mongod.conf"])
        self.assertIn("port: 27017", dict(files)["config/mongod.conf"].decode("utf-8"))
        self.assertEqual(warnings, [])

    def test_a_secret_in_mongod_conf_is_redacted_like_any_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "mongod.conf"
            config.write_text(
                "security:\n  keyFile: /etc/keyfile\n  password: TotallySecretPW123\n",
                encoding="utf-8",
            )

            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                files = support_bundle_service._collect_mongodb_configuration([])

        text = dict(files)["config/mongod.conf"].decode("utf-8")
        self.assertNotIn("TotallySecretPW123", text)

    def test_unreadable_mongod_conf_is_warned_with_the_owner_and_the_bundle_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "mongod.conf"
            config.write_text("systemLog:\n  path: /var/log/mongodb/mongod.log\n", encoding="utf-8")
            config.chmod(0o640)

            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ), patch.object(
                support_bundle_service.Path, "read_text", side_effect=PermissionError("denied")
            ):
                warnings: list[str] = []
                files = support_bundle_service._collect_mongodb_configuration(warnings)

        self.assertEqual(files, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Failed to read MongoDB configuration", warnings[0])
        self.assertIn("mode 0640", warnings[0])
        self.assertIn("the support bundle runs as", warnings[0])

    def test_a_missing_mongod_conf_is_skipped_without_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=pathlib.Path(tmp) / "absent.conf",
            ):
                warnings: list[str] = []
                files = support_bundle_service._collect_mongodb_configuration(warnings)

        self.assertEqual(files, [])
        self.assertEqual(warnings, [])

    def test_the_log_is_still_collected_when_mongod_conf_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_log = tmp_path / "mongod.log"
            mongo_log.write_text("waiting for connections", encoding="utf-8")

            config = tmp_path / "mongod.conf"
            config.write_text("systemLog:\n  path: /nowhere/mongod.log\n", encoding="utf-8")

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ), patch.object(
                support_bundle_service.Path, "read_text", side_effect=PermissionError("denied")
            ), patch.object(
                support_bundle_service, "_DEFAULT_MONGODB_LOG_PATH", str(mongo_log)
            ):
                warnings: list[str] = []
                files = support_bundle_service._collect_logs(warnings)

        self.assertIn("logs/mongodb/mongod.log", [name for name, _ in files])
        self.assertFalse([w for w in warnings if "MongoDB" in w])

    def test_cli_logs_are_found_when_the_server_log_dir_is_pinned_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            server_dir = tmp_path / "server"
            cli_dir = tmp_path / "cli"
            server_dir.mkdir()
            cli_dir.mkdir()
            (server_dir / "server.log").write_text("server line", encoding="utf-8")
            (cli_dir / "cli.log").write_text("cli line", encoding="utf-8")

            with patch.dict(os.environ, {"ALTASTOR_LOG_DIR": str(server_dir)}), patch.object(
                support_bundle_service, "SYSTEM_LOG_DIR", str(tmp_path)
            ), patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(server_dir),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=tmp_path / "absent.conf",
            ):
                warnings: list[str] = []
                files = dict(support_bundle_service._collect_logs(warnings))

        self.assertEqual(files["logs/server/server.log"], b"server line")
        self.assertEqual(files["logs/cli/cli.log"], b"cli line")
        self.assertFalse([w for w in warnings if "CLI logs" in w])

    def test_a_missing_cli_log_dir_is_reported_with_every_place_searched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            (server_dir / "server.log").write_text("server line", encoding="utf-8")

            with patch.dict(os.environ, {"ALTASTOR_LOG_DIR": str(server_dir)}), patch.object(
                support_bundle_service, "SYSTEM_LOG_DIR", str(tmp_path)
            ), patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(server_dir),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=tmp_path / "absent.conf",
            ):
                warnings: list[str] = []
                support_bundle_service._collect_logs(warnings)

        cli_warnings = [w for w in warnings if "CLI logs" in w]
        self.assertEqual(len(cli_warnings), 1)
        self.assertIn(str(tmp_path / "cli"), cli_warnings[0])

    def test_rotated_mongodb_logs_are_collected_next_to_the_live_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_dir = tmp_path / "mongodb"
            mongo_dir.mkdir()
            (mongo_dir / "mongod.log").write_text("live line", encoding="utf-8")
            (mongo_dir / "mongod.log.1").write_text("yesterday line", encoding="utf-8")
            (mongo_dir / "mongod.log.2.gz").write_bytes(gzip.compress(b"last week line"))

            config = tmp_path / "mongod.conf"
            config.write_text(
                f"systemLog:\n  path: {mongo_dir / 'mongod.log'}\n", encoding="utf-8"
            )

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                warnings: list[str] = []
                files = dict(support_bundle_service._collect_logs(warnings))

        self.assertEqual(
            sorted(name for name in files if name.startswith("logs/mongodb/")),
            ["logs/mongodb/mongod.log", "logs/mongodb/mongod.log.1", "logs/mongodb/mongod.log.2"],
        )
        self.assertEqual(files["logs/mongodb/mongod.log.1"], b"yesterday line")
        self.assertFalse([w for w in warnings if "MongoDB log not found" in w])

    def test_secrets_inside_a_compressed_rotation_are_redacted(self) -> None:
        secret_line = b'{"attr":{"command":{"password":"Hunter2","secret_key":"AKIAEXAMPLE"}}}'
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_dir = tmp_path / "mongodb"
            mongo_dir.mkdir()
            (mongo_dir / "mongod.log").write_text("live", encoding="utf-8")
            (mongo_dir / "mongod.log.1.gz").write_bytes(gzip.compress(secret_line))

            config = tmp_path / "mongod.conf"
            config.write_text(
                f"systemLog:\n  path: {mongo_dir / 'mongod.log'}\n", encoding="utf-8"
            )

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                files = dict(support_bundle_service._collect_logs([]))

        collected = files["logs/mongodb/mongod.log.1"]
        self.assertNotIn(b"Hunter2", collected)
        self.assertNotIn(b"AKIAEXAMPLE", collected)
        self.assertIn(b"********", collected)

    def test_a_large_log_is_collected_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_dir = tmp_path / "mongodb"
            mongo_dir.mkdir()
            huge = mongo_dir / "mongod.log"
            filler = b"x" * 1024 + b"\n"
            with huge.open("wb") as handle:
                while handle.tell() < 3 * 1024 * 1024:
                    handle.write(filler)
                handle.write(b"the newest line\n")
            size = huge.stat().st_size

            config = tmp_path / "mongod.conf"
            config.write_text(f"systemLog:\n  path: {huge}\n", encoding="utf-8")

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                warnings: list[str] = []
                files = dict(support_bundle_service._collect_logs(warnings))

        collected = files["logs/mongodb/mongod.log"]
        self.assertEqual(len(collected), size)
        self.assertTrue(collected.endswith(b"the newest line\n"))
        self.assertFalse([w for w in warnings if "Collected only" in w])

    def test_rotated_server_logs_are_collected_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            (tmp_path / "server.log").write_text("live", encoding="utf-8")
            (tmp_path / "server.log.1").write_text("older", encoding="utf-8")

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=tmp_path / "absent.conf",
            ):
                files = dict(support_bundle_service._collect_logs([]))

        self.assertIn("logs/server/server.log", files)
        self.assertIn("logs/server/server.log.1", files)

    def test_unreadable_mongodb_log_warning_names_the_owner_and_the_bundle_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_log = tmp_path / "mongod.log"
            mongo_log.write_text("log line", encoding="utf-8")
            mongo_log.chmod(0o600)

            config = tmp_path / "mongod.conf"
            config.write_text(f"systemLog:\n  path: {mongo_log}\n", encoding="utf-8")

            real_open = support_bundle_service.Path.open

            def deny_only_the_mongo_log(self, *args, **kwargs):
                if self == mongo_log:
                    raise PermissionError(13, "Permission denied")
                return real_open(self, *args, **kwargs)

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ), patch.object(
                support_bundle_service.Path, "open", deny_only_the_mongo_log
            ):
                warnings: list[str] = []
                support_bundle_service._collect_logs(warnings)

        mongo_warnings = [w for w in warnings if str(mongo_log) in w]
        self.assertEqual(len(mongo_warnings), 1)
        self.assertIn("Permission denied", mongo_warnings[0])
        self.assertIn("mode 0600", mongo_warnings[0])
        self.assertIn("the support bundle runs as", mongo_warnings[0])

    def test_mongodb_log_is_collected_from_the_path_mongod_conf_declares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            mongo_log_dir = tmp_path / "var-log-mongodb"
            mongo_log_dir.mkdir()
            mongo_log = mongo_log_dir / "mongod.log"
            mongo_log.write_text("{\"t\": {}, \"msg\": \"waiting for connections\"}\n", encoding="utf-8")

            config = tmp_path / "mongod.conf"
            config.write_text(
                "systemLog:\n"
                "  destination: file\n"
                f"  path: {mongo_log}\n",
                encoding="utf-8",
            )

            with patch(
                "python.srv.services.support_bundle_service._resolve_log_dir",
                return_value=str(tmp_path / "empty-logs"),
            ), patch(
                "python.srv.services.support_bundle_service._server_configuration_file_candidates",
                return_value=[tmp_path / "does-not-exist.conf"],
            ), patch(
                "python.srv.services.support_bundle_service._mongodb_configuration_file",
                return_value=config,
            ):
                result = support_bundle_service.generate_support_bundle()

        self.assertIn("logs/mongodb/mongod.log", result["manifest"]["files"])
        self.assertFalse(
            any("mongodb log not found" in w.lower() for w in result["manifest"]["warnings"])
        )
        with self._archive(result) as archive:
            self.assertIn("waiting for connections", archive.read("logs/mongodb/mongod.log").decode("utf-8"))

    def test_unreadable_configuration_file_is_warned_not_fatal(self) -> None:
        with patch.object(support_bundle_service.Path, "is_file", return_value=True), patch.object(
            support_bundle_service.Path, "read_text", side_effect=PermissionError("denied")
        ):
            warnings: list[str] = []
            files = support_bundle_service._collect_configuration(warnings)

        self.assertEqual(files, [])
        self.assertTrue(any("failed to read server configuration" in w.lower() for w in warnings))
        self.assertFalse(any("not found" in w.lower() for w in warnings))

    def _generate_with_log_dir(self, log_dir: str):
        with patch(
            "python.srv.services.support_bundle_service._resolve_log_dir",
            return_value=log_dir,
        ), patch(
            "python.srv.services.support_bundle_service._server_configuration_file_candidates",
            return_value=[pathlib.Path(log_dir) / "does-not-exist.default"],
        ), patch(
            "python.srv.services.support_bundle_service._mongodb_configuration_file",
            return_value=pathlib.Path(log_dir) / "does-not-exist-mongodb.default",
        ), patch(
            "python.srv.services.support_bundle_service._run_tool", return_value=None
        ):
            return support_bundle_service.generate_support_bundle()

    def test_fault_file_holding_a_crash_traceback_is_collected(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            (pathlib.Path(log_dir) / "server.log.fault").write_text(
                "Fatal Python error: Segmentation fault", encoding="utf-8"
            )
            (pathlib.Path(log_dir) / "cli.log.fault").write_text(
                "Fatal Python error: Segmentation fault (cli)", encoding="utf-8"
            )
            (pathlib.Path(log_dir) / "server.log").write_text("normal log", encoding="utf-8")

            result = self._generate_with_log_dir(log_dir)

        with self._archive(result) as archive:
            names = archive.namelist()
            self.assertIn("logs/server/server.log.fault", names)
            self.assertIn("logs/cli/cli.log.fault", names)
            self.assertIn("logs/server/server.log", names)
            self.assertEqual(
                archive.read("logs/server/server.log.fault").decode("utf-8"),
                "Fatal Python error: Segmentation fault",
            )

    def test_secrets_leaking_into_application_logs_are_masked(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            (pathlib.Path(log_dir) / "server.log").write_text(
                "job failed: connecting to mongodb://dbuser:dbpass@10.0.0.5:27018/abr\n"
                'repository payload {"username": "svc", "password": "hunter2"}\n'
                "retry with secret_key=AKIAIOSFODNN7EXAMPLE and Authorization: Bearer eyJhbGciOi.J9\n",
                encoding="utf-8",
            )

            result = self._generate_with_log_dir(log_dir)

        with self._archive(result) as archive:
            content = archive.read("logs/server/server.log").decode("utf-8")

        for secret in ("dbpass", "hunter2", "AKIAIOSFODNN7EXAMPLE", "eyJhbGciOi.J9"):
            self.assertNotIn(secret, content)
        self.assertIn("********", content)
        self.assertIn("10.0.0.5:27018", content, "the host must survive, only the secret goes")

    def test_redaction_does_not_mangle_ordinary_log_lines(self) -> None:
        untouched = (
            "Password must be at least 8 characters\n"
            "password_required=true\n"
            "connected to mongodb://127.0.0.1:27018\n"
            "job 'nightly-vm' finished with status=success in 42s\n"
        )
        with tempfile.TemporaryDirectory() as log_dir:
            (pathlib.Path(log_dir) / "server.log").write_text(untouched, encoding="utf-8")

            result = self._generate_with_log_dir(log_dir)

        with self._archive(result) as archive:
            self.assertEqual(archive.read("logs/server/server.log").decode("utf-8"), untouched)

    def test_log_bytes_that_are_not_valid_utf8_survive_redaction(self) -> None:
        raw = b"before \xff\xfe truncated multibyte \xe2\x82 after\n"
        with tempfile.TemporaryDirectory() as log_dir:
            (pathlib.Path(log_dir) / "server.log").write_bytes(raw)

            result = self._generate_with_log_dir(log_dir)

        with self._archive(result) as archive:
            self.assertEqual(archive.read("logs/server/server.log"), raw)

    def test_empty_fault_file_is_left_out_so_its_presence_still_means_something(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            (pathlib.Path(log_dir) / "server.log").write_text("normal log", encoding="utf-8")
            (pathlib.Path(log_dir) / "server.log.fault").write_text("", encoding="utf-8")
            (pathlib.Path(log_dir) / "cli.log.fault").write_text("", encoding="utf-8")

            result = self._generate_with_log_dir(log_dir)

        self.assertFalse([f for f in result["manifest"]["files"] if "fault" in f])
        with self._archive(result) as archive:
            names = archive.namelist()
            self.assertFalse([n for n in names if "fault" in n])
            self.assertIn("logs/server/server.log", names)


class CatalogCollectionTests(unittest.TestCase):
    _SOURCES = (
        "list_sessions",
        "list_jobs",
        "list_repository_records",
        "list_inventory_records",
        "list_restore_points",
        "system_health",
    )

    def _collect(self, **overrides) -> tuple[dict[str, bytes], list[str]]:
        patches = [
            patch.object(
                support_bundle_service.abr_v41,
                name,
                overrides.get(name, list),
            )
            for name in self._SOURCES
        ]
        for p in patches:
            p.start()
        try:
            warnings: list[str] = []
            files = support_bundle_service._collect_catalog(warnings)
        finally:
            for p in patches:
                p.stop()
        return dict(files), warnings

    def test_every_catalog_section_lands_in_the_bundle(self) -> None:
        files, warnings = self._collect()

        self.assertEqual(
            sorted(files),
            [
                "catalog/health.json",
                "catalog/inventories.json",
                "catalog/jobs.json",
                "catalog/repositories.json",
                "catalog/restore-points.json",
                "catalog/sessions.json",
            ],
        )
        self.assertEqual(warnings, [])

    def test_session_history_is_capped_so_the_bundle_stays_bounded(self) -> None:
        sessions = [{"id": f"session-{index}"} for index in range(250)]

        files, _ = self._collect(list_sessions=lambda: sessions)

        collected = json.loads(files["catalog/sessions.json"])
        self.assertEqual(len(collected), support_bundle_service._SESSION_HISTORY_LIMIT)
        self.assertEqual(collected[0]["id"], "session-0", "newest first must be kept")

    def test_session_logs_and_errors_are_carried_through(self) -> None:
        session = {
            "id": "s1",
            "state": "failed",
            "triggered_by": "schedule",
            "errors": [{"message": "repository unreachable"}],
            "logs": [{"level": "error", "message": "connect timed out"}],
        }

        files, _ = self._collect(list_sessions=lambda: [session])

        collected = json.loads(files["catalog/sessions.json"])[0]
        self.assertEqual(collected["errors"][0]["message"], "repository unreachable")
        self.assertEqual(collected["logs"][0]["message"], "connect timed out")
        self.assertEqual(collected["triggered_by"], "schedule")

    def test_one_unreachable_source_is_warned_and_the_rest_still_collected(self) -> None:
        def exploding():
            raise RuntimeError("catalog database is down")

        files, warnings = self._collect(list_sessions=exploding)

        self.assertNotIn("catalog/sessions.json", files)
        self.assertIn("catalog/jobs.json", files)
        self.assertTrue(any("catalog database is down" in w for w in warnings))

    def test_users_and_hypervisor_resources_are_never_collected(self) -> None:
        files, _ = self._collect()

        self.assertFalse([name for name in files if "user" in name or "resource" in name])


class SupportBundleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)

    def test_support_bundle_endpoint_returns_archive_and_manifest(self) -> None:
        response = self.client.post("/api/v1/system/support-bundle")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("archive_base64", body)
        self.assertIn("manifest", body)
        self.assertEqual(body["manifest"]["product"], "AltaStor Backup & Replication")

    def test_support_bundle_endpoint_requires_authentication(self) -> None:
        app.dependency_overrides.pop(require_session, None)
        try:
            response = self.client.post("/api/v1/system/support-bundle")
            self.assertEqual(response.status_code, 401)
        finally:
            app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}


if __name__ == "__main__":
    unittest.main()
