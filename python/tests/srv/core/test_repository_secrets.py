# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from python.srv.core import credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import REPOSITORIES
from python.srv.services.api_errors import ApiError
from python.tests.srv.core.test_credentials_at_rest import (
    _KeyFileTestCase,
    _RecordingDatabase,
)

SMB_PASSWORD = "Smb-Share-Secret!7"
S3_SECRET = "s3-secret-key-0123456789abcdef"
PREFIX = credential_encryption.ENCRYPTED_PREFIX


def _smb(password: str | None = SMB_PASSWORD) -> dict:
    return {
        "id": "repo-smb",
        "name": "HV-SMB",
        "type": "smb",
        "path": "/mnt/hv",
        "metadata": {"server": "nas", "share": "hv", "username": "backupsvc", "password": password},
    }


def _s3(secret: str | None = S3_SECRET) -> dict:
    return {
        "id": "repo-s3",
        "name": "S3",
        "type": "s3",
        "path": "s3://bucket/prefix",
        "metadata": {"s3": {"bucket": "bucket", "access_key": "AKIA0000", "secret_key": secret}},
    }


class _RepositoryCase(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        self.db = _RecordingDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)

    def stored(self, repository_id: str) -> dict:
        return self.db.collections["repositories"][repository_id]

    def stored_secret(self, repository_id: str) -> str:
        metadata = self.stored(repository_id)["metadata"]
        return metadata["password"] if "password" in metadata else metadata["s3"]["secret_key"]

    def load(self) -> dict:
        loaded = mongo_db_wrapper.load_repositories()
        REPOSITORIES.clear()
        REPOSITORIES.update(loaded)
        return loaded


class StoredSecretTests(_RepositoryCase):
    def test_new_repositories_store_their_secrets_encrypted_and_keep_them_readable(self) -> None:
        mongo_db_wrapper.persist_repository(_smb())
        mongo_db_wrapper.persist_repository(_s3())

        for repository_id, secret in (("repo-smb", SMB_PASSWORD), ("repo-s3", S3_SECRET)):
            with self.subTest(repository=repository_id):
                self.assertTrue(self.stored_secret(repository_id).startswith(PREFIX))
                self.assertNotIn(secret, self.db.everything_written())
                self.assertEqual(credential_encryption.decrypt_secret(self.stored_secret(repository_id)), secret)
        self.assertEqual(REPOSITORIES["repo-smb"]["metadata"]["password"], SMB_PASSWORD)
        self.assertEqual(REPOSITORIES["repo-s3"]["metadata"]["s3"]["secret_key"], S3_SECRET)

    def test_an_edited_secret_is_stored_encrypted_too(self) -> None:
        mongo_db_wrapper.persist_repository(_smb())
        mongo_db_wrapper.persist_repository(_smb("New-Password-9"))

        self.assertEqual(credential_encryption.decrypt_secret(self.stored_secret("repo-smb")), "New-Password-9")
        self.assertNotIn("New-Password-9", self.db.everything_written())

    def test_repositories_without_secrets_are_stored_as_they_are(self) -> None:
        local = {"id": "repo-local", "name": "Local", "type": "local", "path": "/srv/backup", "metadata": {"password": None}}
        mongo_db_wrapper.persist_repository(local)
        mongo_db_wrapper.persist_repository(_s3(secret=None))

        self.assertIsNone(self.stored("repo-local")["metadata"]["password"])
        self.assertIsNone(self.stored("repo-s3")["metadata"]["s3"]["secret_key"])

    def test_a_restart_reads_the_secrets_back(self) -> None:
        mongo_db_wrapper.persist_repository(_smb())
        mongo_db_wrapper.persist_repository(_s3())

        loaded = self.load()

        self.assertEqual(loaded["repo-smb"]["metadata"]["password"], SMB_PASSWORD)
        self.assertEqual(loaded["repo-s3"]["metadata"]["s3"]["secret_key"], S3_SECRET)


class UpgradeTests(_RepositoryCase):
    def test_the_first_start_encrypts_what_an_earlier_version_left_in_clear_and_a_second_changes_nothing(self) -> None:
        self.db.collections["repositories"] = {"repo-smb": _smb(), "repo-s3": _s3()}

        with self.assertLogs(mongo_db_wrapper.LOGGER, level="INFO") as logs:
            first = self.load()

        self.assertTrue(self.stored_secret("repo-smb").startswith(PREFIX))
        self.assertTrue(self.stored_secret("repo-s3").startswith(PREFIX))
        self.assertNotIn(SMB_PASSWORD, json_of(self.db.collections))
        self.assertNotIn(S3_SECRET, json_of(self.db.collections))
        self.assertEqual(first["repo-smb"]["metadata"]["password"], SMB_PASSWORD)
        self.assertEqual(len([line for line in logs.output if "Encrypted the stored" in line]), 2)

        before = copy.deepcopy(self.db.collections["repositories"])
        self.db.upserts.clear()
        second = self.load()

        self.assertEqual(self.db.upserts, [])
        self.assertEqual(self.db.collections["repositories"], before)
        self.assertEqual(second["repo-s3"]["metadata"]["s3"]["secret_key"], S3_SECRET)

    def test_a_secret_already_encrypted_by_the_share_code_is_read_back(self) -> None:
        encrypted = credential_encryption.encrypt_secret(SMB_PASSWORD)
        self.db.collections["repositories"] = {"repo-smb": _smb(encrypted)}

        loaded = self.load()

        self.assertEqual(loaded["repo-smb"]["metadata"]["password"], SMB_PASSWORD)
        self.assertEqual(self.stored_secret("repo-smb"), encrypted)

    def test_a_secret_of_another_key_is_named_and_kept(self) -> None:
        foreign = PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        self.db.collections["repositories"] = {"repo-s3": _s3(foreign)}
        credential_encryption.encrypt_secret("make the key")

        with self.assertLogs(mongo_db_wrapper.LOGGER, level="ERROR") as logs:
            loaded = self.load()

        self.assertEqual(loaded["repo-s3"]["metadata"]["s3"]["secret_key"], foreign)
        self.assertIn("repository S3 cannot be decrypted", "\n".join(logs.output))
        self.assertIn("--secret-key", "\n".join(logs.output))


class MissingKeyTests(_RepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        mongo_db_wrapper.persist_repository(_smb())
        self.encrypted = self.stored_secret("repo-smb")
        real_read = Path.read_bytes

        def refuse(path: Path) -> bytes:
            if path == self.key_path:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read(path)

        credential_encryption.reset_cipher()
        handle = patch.object(Path, "read_bytes", refuse)
        handle.start()
        self.addCleanup(handle.stop)

    def test_a_changed_secret_is_refused_with_the_reason(self) -> None:
        with self.assertRaises(ApiError) as caught:
            mongo_db_wrapper.persist_repository(_smb("Another-Password-1"))

        self.assertEqual((caught.exception.status_code, caught.exception.code), (503, "credential_key_unavailable"))
        self.assertIn("HV-SMB", caught.exception.message)
        self.assertIn("is not stored", caught.exception.message)
        self.assertEqual(self.stored_secret("repo-smb"), self.encrypted)
        self.assertNotIn("Another-Password-1", self.db.everything_written())

    def test_an_edit_that_keeps_the_secret_is_still_saved(self) -> None:
        unchanged = copy.deepcopy(REPOSITORIES["repo-smb"])
        unchanged["description"] = "renamed"

        mongo_db_wrapper.persist_repository(unchanged)

        self.assertEqual(self.stored("repo-smb")["description"], "renamed")

    def test_a_start_without_the_key_leaves_secrets_as_they_are_and_says_so(self) -> None:
        self.db.collections["repositories"]["repo-s3"] = _s3()

        with self.assertLogs(mongo_db_wrapper.LOGGER, level="ERROR") as logs:
            loaded = self.load()

        self.assertEqual(self.stored("repo-s3")["metadata"]["s3"]["secret_key"], S3_SECRET)
        self.assertEqual(self.stored_secret("repo-smb"), self.encrypted)
        self.assertEqual(loaded["repo-smb"]["metadata"]["password"], self.encrypted)
        self.assertIn("none is encrypted", "\n".join(logs.output))
        self.assertIn("HV-SMB", "\n".join(logs.output))


class ImportTests(_RepositoryCase):
    def test_an_import_without_the_key_is_refused_before_anything_is_written(self) -> None:
        credential_encryption.reset_cipher()
        with (
            patch.object(credential_encryption, "_cipher", side_effect=credential_encryption.CredentialKeyUnavailable("no key")),
            self.assertRaises(ApiError) as caught,
        ):
            mongo_db_wrapper.restore_catalog({"repositories": {"repo-s3": _s3()}})

        self.assertEqual(caught.exception.code, "credential_key_unavailable")
        self.assertNotIn("repo-s3", self.db.collections.get("repositories", {}))


def json_of(value: object) -> str:
    return json.dumps(value)
