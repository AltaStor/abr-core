# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.cli.services.server_defaults_template import render_recovered_server_defaults
from python.srv.services.abr_v41 import (
    CONFIGURATION_EXPORT_ALLOWLIST,
    SECRET_CONFIGURATION_KEY_PARTS,
    configuration_without_secrets,
)

SETTINGS_KNOWN_TO_HOLD_NO_SECRET = frozenset(
    {
        "ALTASTOR_BACKUP_APP",
        "ALTASTOR_BACKUP_HOST",
        "ALTASTOR_BACKUP_PORT",
        "ABR_HTTP_REDIRECT_PORT",
        "MONGODB_DB_NAME",
        "MONGODB_MAX_POOL_SIZE",
        "MONGODB_SERVER_SELECTION_TIMEOUT_MS",
        "MONGODB_CONNECT_TIMEOUT_MS",
        "MONGODB_SOCKET_TIMEOUT_MS",
        "BACKUPSERVER_STORAGE_ROOT",
        "BACKUPSERVER_VCENTER_TOOL_TIMEOUT_SECONDS",
        "BACKUPSERVER_VCENTER_MIN_TRANSFER_MBPS",
        "BACKUPSERVER_VCENTER_SESSION_KEEPALIVE_SECONDS",
        "BACKUP_WORKER_COUNT",
        "MAX_CONCURRENT_BACKUPS",
        "BACKUP_TOOL_MAX_CAPTURE_BYTES",
        "BACKUP_TOOL_NICE",
        "BACKUP_TOOL_IONICE_CLASS",
        "ALTASTOR_DISK_TOOL_PATH",
        "ALTASTOR_LOG_DIR",
        "TZ",
        "ABR_BOOTSTRAP_ADMIN_USER",
        "ABR_SESSION_TTL_SECONDS",
        "ABR_LOGIN_MAX_ATTEMPTS",
        "ABR_LOGIN_LOCKOUT_SECONDS",
    }
)

CONFIGURATION = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Bootstrap-Secret-77",
    "MONGODB_URI": "mongodb://catalog:Db-Secret-88@db.local:27017/backup_db?authSource=admin",
    "MONGODB_DB_NAME": "backup_db",
    "ABR_SESSION_TTL_SECONDS": "3600",
}


class ConfigurationWithoutSecretsTests(unittest.TestCase):
    def test_the_bootstrap_password_is_masked(self) -> None:
        self.assertEqual(configuration_without_secrets(CONFIGURATION)["ABR_BOOTSTRAP_ADMIN_PASSWORD"], "********")

    def test_uri_credentials_are_masked_and_the_rest_of_the_uri_kept(self) -> None:
        self.assertEqual(
            configuration_without_secrets(CONFIGURATION)["MONGODB_URI"],
            "mongodb://********@db.local:27017/backup_db?authSource=admin",
        )

    def test_a_password_holding_an_at_sign_does_not_leak_its_tail(self) -> None:
        shown = configuration_without_secrets({"MONGODB_URI": "mongodb+srv://u:p@ss@cluster0.example.net/db"})
        self.assertEqual(shown["MONGODB_URI"], "mongodb+srv://********@cluster0.example.net/db")

    def test_a_uri_without_credentials_and_plain_settings_are_unchanged(self) -> None:
        shown = configuration_without_secrets(
            {**CONFIGURATION, "MONGODB_URI": "mongodb://127.0.0.1:27018"}
        )
        self.assertEqual(shown["MONGODB_URI"], "mongodb://127.0.0.1:27018")
        self.assertEqual(shown["MONGODB_DB_NAME"], "backup_db")
        self.assertEqual(shown["ABR_SESSION_TTL_SECONDS"], "3600")
        self.assertEqual(shown["ABR_BOOTSTRAP_ADMIN_USER"], "admin")

    def test_an_unset_secret_stays_visibly_unset(self) -> None:
        self.assertEqual(configuration_without_secrets({"ABR_BOOTSTRAP_ADMIN_PASSWORD": ""}), {"ABR_BOOTSTRAP_ADMIN_PASSWORD": ""})

    def test_every_exportable_setting_is_either_masked_or_known_to_hold_no_secret(self) -> None:
        unclassified = [
            key
            for key in CONFIGURATION_EXPORT_ALLOWLIST
            if key not in SETTINGS_KNOWN_TO_HOLD_NO_SECRET
            and not any(part in key.upper() for part in SECRET_CONFIGURATION_KEY_PARTS)
            and not key.endswith("_URI")
        ]

        self.assertEqual(
            unclassified,
            [],
            "a setting added to the export allowlist must be classified: add it to "
            "SETTINGS_KNOWN_TO_HOLD_NO_SECRET if it holds no secret, or name it so it is masked",
        )

    def test_the_file_abr_system_import_writes_is_unchanged(self) -> None:
        self.assertEqual(
            render_recovered_server_defaults(configuration_without_secrets(CONFIGURATION)),
            render_recovered_server_defaults(CONFIGURATION),
        )


if __name__ == "__main__":
    unittest.main()
