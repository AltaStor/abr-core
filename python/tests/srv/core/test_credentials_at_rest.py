# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from python.srv.core import credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import HYPERVISORS, SESSIONS

VCENTER_PASSWORD = "Vc-Admin-Secret!42"
RAW_TOKEN = "kOOxqlUisZZl-tGpEmct5jzmRmfv1oG2RHKPYE1ixGM"


class _RecordingDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {"hypervisors": {}, "sessions": {}}
        self.upserts: list[tuple[str, dict]] = []
        self.rehash_calls = 0

    def find(self, collection: str, query: dict) -> list[dict]:
        rows = list(self.collections.setdefault(collection, {}).values())
        return [copy.deepcopy(row) for row in rows if all(row.get(k) == v for k, v in query.items())]

    def upsert(self, collection: str, id_field: str, document: dict) -> None:
        self.upserts.append((collection, copy.deepcopy(document)))
        self.collections.setdefault(collection, {})[document[id_field]] = copy.deepcopy(document)

    def delete(self, collection: str, id_field: str, doc_id: str) -> bool:
        return self.collections.setdefault(collection, {}).pop(doc_id, None) is not None

    def delete_hypervisor_resources(self, hypervisor_id: str) -> None:
        return None

    def rehash_legacy_session_tokens(self, digest) -> int:
        self.rehash_calls += 1
        sessions = self.collections["sessions"]
        rehashed = 0
        for key in list(sessions):
            if not credential_encryption.is_token_digest(key):
                row = sessions.pop(key)
                row["token"] = digest(key)
                sessions[row["token"]] = row
                rehashed += 1
        return rehashed

    def everything_written(self) -> str:
        return json.dumps(self.collections) + json.dumps(self.upserts)


def _inventory(password: str = VCENTER_PASSWORD, token: str | None = None) -> dict:
    return {
        "id": "inv-1",
        "name": "vCenter-Prod",
        "type": "vsphere",
        "metadata": {
            "host": "vc.local",
            "port": 443,
            "credentials": {"username": "administrator@vsphere.local", "password": password},
            "token": token,
        },
    }


class _KeyFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key_path = Path(self._tmp.name) / "tls" / "credentials.key"
        env = patch.dict(os.environ, {credential_encryption.KEY_FILE_ENV: str(self.key_path)})
        env.start()
        self.addCleanup(env.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)


class CredentialEncryptionTests(_KeyFileTestCase):
    def test_a_secret_round_trips_and_never_shows_in_its_ciphertext(self) -> None:
        encrypted = credential_encryption.encrypt_secret(VCENTER_PASSWORD)

        self.assertTrue(encrypted.startswith(credential_encryption.ENCRYPTED_PREFIX))
        self.assertNotIn(VCENTER_PASSWORD, encrypted)
        self.assertEqual(credential_encryption.decrypt_secret(encrypted), VCENTER_PASSWORD)

    def test_a_secret_stored_in_clear_before_reads_as_it_is(self) -> None:
        self.assertEqual(credential_encryption.decrypt_secret(VCENTER_PASSWORD), VCENTER_PASSWORD)

    def test_encrypting_twice_or_encrypting_nothing_changes_nothing(self) -> None:
        encrypted = credential_encryption.encrypt_secret(VCENTER_PASSWORD)

        self.assertEqual(credential_encryption.encrypt_secret(encrypted), encrypted)
        self.assertEqual(credential_encryption.encrypt_secret(""), "")
        self.assertIsNone(credential_encryption.encrypt_secret(None))

    def test_the_key_file_is_created_private_and_reused(self) -> None:
        first = credential_encryption.encrypt_secret(VCENTER_PASSWORD)

        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        credential_encryption.reset_cipher()
        self.assertEqual(credential_encryption.decrypt_secret(first), VCENTER_PASSWORD)

    def test_a_secret_from_another_key_is_handed_back_untouched(self) -> None:
        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()

        self.assertEqual(credential_encryption.decrypt_secret(foreign), foreign)

    def test_a_token_digest_is_stable_and_recognisable(self) -> None:
        digest = credential_encryption.token_digest(RAW_TOKEN)

        self.assertEqual(digest, credential_encryption.token_digest(RAW_TOKEN))
        self.assertTrue(credential_encryption.is_token_digest(digest))
        self.assertFalse(credential_encryption.is_token_digest(RAW_TOKEN))


class InventoryCredentialStorageTests(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        HYPERVISORS.clear()
        self.addCleanup(HYPERVISORS.clear)
        self.db = _RecordingDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)

    def _stored_password(self) -> str:
        return self.db.collections["hypervisors"]["inv-1"]["metadata"]["credentials"]["password"]

    def test_a_registered_inventory_reaches_the_database_encrypted(self) -> None:
        mongo_db_wrapper.persist_hypervisor(_inventory(token="vc-session-token"))

        self.assertNotIn(VCENTER_PASSWORD, self.db.everything_written())
        self.assertNotIn("vc-session-token", self.db.everything_written())
        self.assertTrue(credential_encryption.is_encrypted(self._stored_password()))
        self.assertEqual(HYPERVISORS["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
        self.assertEqual(HYPERVISORS["inv-1"]["metadata"]["token"], "vc-session-token")

    def test_an_updated_inventory_is_written_back_encrypted(self) -> None:
        mongo_db_wrapper.persist_hypervisor(_inventory())
        self.db.upserts.clear()

        mongo_db_wrapper.update_hypervisor_fields("inv-1", {"last_scan_at": "2026-10-05T12:00:00+00:00"})

        self.assertEqual(len(self.db.upserts), 1)
        self.assertNotIn(VCENTER_PASSWORD, self.db.everything_written())
        self.assertEqual(HYPERVISORS["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)

    def test_an_inventory_stored_in_clear_is_read_in_clear_and_rewritten_encrypted(self) -> None:
        self.db.collections["hypervisors"]["inv-1"] = _inventory()

        loaded = mongo_db_wrapper.load_registered_hypervisors()

        self.assertEqual(loaded["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
        self.assertTrue(credential_encryption.is_encrypted(self._stored_password()))

    def test_an_inventory_already_encrypted_is_decrypted_without_a_rewrite(self) -> None:
        mongo_db_wrapper.persist_hypervisor(_inventory())
        self.db.upserts.clear()

        loaded = mongo_db_wrapper.load_registered_hypervisors()

        self.assertEqual(loaded["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
        self.assertEqual(self.db.upserts, [])

    def test_a_system_import_writes_the_restored_inventory_encrypted(self) -> None:
        mongo_db_wrapper.restore_catalog({"hypervisors": {"inv-1": _inventory()}})

        self.assertNotIn(VCENTER_PASSWORD, self.db.everything_written())
        self.assertEqual(HYPERVISORS["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)

    def test_a_password_this_key_cannot_open_is_kept_as_it_was(self) -> None:
        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        self.db.collections["hypervisors"]["inv-1"] = _inventory(password=foreign)

        with self.assertLogs(mongo_db_wrapper.LOGGER, level="ERROR") as logs:
            mongo_db_wrapper.load_registered_hypervisors()
        mongo_db_wrapper.update_hypervisor_fields("inv-1", {"last_scan_at": "2026-10-05T12:00:00+00:00"})

        self.assertEqual(self._stored_password(), foreign)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("inventory vCenter-Prod cannot be decrypted", logs.output[0])
        self.assertIn(str(self.key_path), logs.output[0])
        self.assertIn("abr inventory edit --name vCenter-Prod --password", logs.output[0])

    def test_a_password_entered_again_after_a_lost_key_is_stored_under_the_current_key(self) -> None:
        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        self.db.collections["hypervisors"]["inv-1"] = _inventory(password=foreign)
        with self.assertLogs(mongo_db_wrapper.LOGGER, level="ERROR"):
            mongo_db_wrapper.load_registered_hypervisors()
        mongo_db_wrapper.persist_hypervisor(_inventory())

        self.assertNotEqual(self._stored_password(), foreign)
        self.assertEqual(credential_encryption.decrypt_secret(self._stored_password()), VCENTER_PASSWORD)
        with self.assertNoLogs(mongo_db_wrapper.LOGGER, level="ERROR"):
            loaded = mongo_db_wrapper.load_registered_hypervisors()
        self.assertEqual(loaded["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)


class SessionTokenStorageTests(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        SESSIONS.clear()
        self.addCleanup(SESSIONS.clear)
        self.db = _RecordingDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)

    def _session(self, token: str = RAW_TOKEN, user_id: str = "user-1") -> dict:
        return {"token": token, "user_id": user_id, "expires_at": "2099-01-01T00:00:00+00:00"}

    def test_a_session_reaches_the_database_only_as_a_digest(self) -> None:
        mongo_db_wrapper.persist_session(self._session())

        self.assertNotIn(RAW_TOKEN, self.db.everything_written())
        self.assertNotIn(RAW_TOKEN, SESSIONS)
        self.assertIn(credential_encryption.token_digest(RAW_TOKEN), self.db.collections["sessions"])

    def test_the_raw_token_still_finds_renews_and_ends_its_session(self) -> None:
        mongo_db_wrapper.persist_session(self._session())

        self.assertEqual(mongo_db_wrapper.get_session(RAW_TOKEN)["user_id"], "user-1")
        mongo_db_wrapper.renew_session(RAW_TOKEN, {"expires_at": "2099-02-01T00:00:00+00:00"})
        self.assertEqual(mongo_db_wrapper.get_session(RAW_TOKEN)["expires_at"], "2099-02-01T00:00:00+00:00")
        self.assertNotIn(RAW_TOKEN, self.db.everything_written())

        mongo_db_wrapper.delete_session(RAW_TOKEN)

        self.assertIsNone(mongo_db_wrapper.get_session(RAW_TOKEN))
        self.assertEqual(self.db.collections["sessions"], {})

    def test_sessions_stored_before_the_change_are_rehashed_and_still_valid(self) -> None:
        self.db.collections["sessions"][RAW_TOKEN] = self._session()

        loaded = mongo_db_wrapper.load_sessions()

        self.assertEqual(self.db.rehash_calls, 1)
        self.assertIn(credential_encryption.token_digest(RAW_TOKEN), loaded)
        self.assertNotIn(RAW_TOKEN, self.db.everything_written())

    def test_changing_a_password_keeps_only_the_callers_session(self) -> None:
        other = "Zy9-other-session-token-of-the-same-user-xx"
        mongo_db_wrapper.persist_session(self._session())
        mongo_db_wrapper.persist_session(self._session(token=other))

        mongo_db_wrapper.delete_other_sessions_for_user("user-1", RAW_TOKEN)

        self.assertIsNotNone(mongo_db_wrapper.get_session(RAW_TOKEN))
        self.assertIsNone(mongo_db_wrapper.get_session(other))
        self.assertEqual(list(self.db.collections["sessions"]), [credential_encryption.token_digest(RAW_TOKEN)])


class UnreadablePasswordIsNeverSentTests(_KeyFileTestCase):
    def test_a_password_this_key_cannot_open_is_not_tried_against_vcenter(self) -> None:
        from python.srv.plugins.vcenter import vcenter

        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        node = vcenter.VCenterNode("vc.local", "administrator@vsphere.local", credential_encryption.decrypt_secret(foreign))

        with patch.object(vcenter, "SmartConnect") as smart_connect:
            with self.assertRaises(credential_encryption.StoredCredentialUnreadable) as raised:
                node.connect()

        smart_connect.assert_not_called()
        self.assertIn("vc.local", str(raised.exception))
        self.assertIn("abr inventory edit", str(raised.exception))
        self.assertNotIn(foreign, str(raised.exception))

    def test_an_inventory_check_reports_why_instead_of_a_generic_connection_error(self) -> None:
        from python.srv.plugins.vcenter import vcenter
        from python.srv.services import inventory_management

        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        record = _inventory(password=credential_encryption.decrypt_secret(foreign))

        with patch.object(vcenter, "SmartConnect") as smart_connect:
            with self.assertRaises(credential_encryption.StoredCredentialUnreadable):
                inventory_management.VsphereInventoryProvider().test_connection(record)

        smart_connect.assert_not_called()

    def test_a_readable_password_still_logs_in(self) -> None:
        from python.srv.plugins.vcenter import vcenter

        stored = credential_encryption.encrypt_secret(VCENTER_PASSWORD)
        node = vcenter.VCenterNode("vc.local", "administrator@vsphere.local", credential_encryption.decrypt_secret(stored))

        with patch.object(vcenter, "SmartConnect") as smart_connect:
            node.connect()

        self.assertEqual(smart_connect.call_args.kwargs["pwd"], VCENTER_PASSWORD)


class UnreadableHyperVPasswordIsNeverSentTests(_KeyFileTestCase):
    def test_a_password_this_key_cannot_open_is_not_tried_against_a_hyperv_host(self) -> None:
        from python.srv.plugins.hyperv import hyperv

        foreign = credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"other").decode()
        node = hyperv.HyperVNode("hv.local", "CORP\\backupsvc", credential_encryption.decrypt_secret(foreign))

        with patch.object(hyperv, "_port_is_open", return_value=True), patch("pypsrp.client.Client") as client:
            with self.assertRaises(credential_encryption.StoredCredentialUnreadable) as raised:
                node.connect()

        client.assert_not_called()
        self.assertIn("hv.local", str(raised.exception))
        self.assertIn("abr inventory edit", str(raised.exception))
        self.assertNotIn(foreign, str(raised.exception))

    def test_a_readable_password_still_reaches_a_hyperv_host(self) -> None:
        from python.srv.plugins.hyperv import hyperv

        stored = credential_encryption.encrypt_secret("hyperv-secret")
        node = hyperv.HyperVNode("hv.local", "CORP\\backupsvc", credential_encryption.decrypt_secret(stored))

        with patch("pypsrp.client.Client") as client:
            node._build_client("hv.local", hyperv.HTTPS_PORT, "https")

        self.assertEqual(client.call_args.kwargs["password"], "hyperv-secret")


class KeyFileDurabilityTests(_KeyFileTestCase):
    def test_a_new_key_is_flushed_and_published_whole(self) -> None:
        synced: list[int] = []
        real_fsync = os.fsync
        with patch.object(credential_encryption.os, "fsync", side_effect=lambda fd: (synced.append(fd), real_fsync(fd))):
            credential_encryption.encrypt_secret(VCENTER_PASSWORD)

        self.assertGreaterEqual(len(synced), 2)
        Fernet(self.key_path.read_bytes().strip())
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.key_path.parent.iterdir()], ["credentials.key"])

    def test_a_crash_before_the_key_is_published_leaves_no_key_and_no_leftover(self) -> None:
        with patch.object(credential_encryption.os, "link", side_effect=RuntimeError("power lost")):
            with self.assertRaises(RuntimeError):
                credential_encryption.encrypt_secret(VCENTER_PASSWORD)

        self.assertEqual(list(self.key_path.parent.iterdir()), [])
        credential_encryption.reset_cipher()
        encrypted = credential_encryption.encrypt_secret(VCENTER_PASSWORD)
        self.assertEqual(credential_encryption.decrypt_secret(encrypted), VCENTER_PASSWORD)

    def test_an_existing_key_file_is_never_replaced(self) -> None:
        self.key_path.parent.mkdir(parents=True)
        self.key_path.write_bytes(b"")

        self.assertIn("cannot be used: the file is empty", credential_encryption.credential_key_problem())
        self.assertEqual(self.key_path.read_bytes(), b"")


class UnusableKeyTests(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        HYPERVISORS.clear()
        self.addCleanup(HYPERVISORS.clear)
        self.db = _RecordingDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)
        mongo_db_wrapper.persist_hypervisor(_inventory())
        self.encrypted = self.db.collections["hypervisors"]["inv-1"]["metadata"]["credentials"]["password"]
        clear = _inventory()
        clear.update(id="inv-2", name="vCenter-Old")
        self.db.collections["hypervisors"]["inv-2"] = clear
        self.good_key = self.key_path.read_bytes()
        HYPERVISORS.clear()
        self.db.upserts.clear()

    def _break_key(self, how: str) -> None:
        credential_encryption.reset_cipher()
        if how == "corrupt":
            self.key_path.write_bytes(b"not-a-fernet-key")
        else:
            real_read = Path.read_bytes
            def refuse(path: Path) -> bytes:
                if path == self.key_path:
                    raise PermissionError(13, "Permission denied", str(path))
                return real_read(path)
            handle = patch.object(Path, "read_bytes", refuse)
            handle.start()
            self.addCleanup(handle.stop)

    def _load(self) -> tuple[dict, list[str]]:
        with self.assertLogs(mongo_db_wrapper.LOGGER, level="ERROR") as logs:
            loaded = mongo_db_wrapper.load_registered_hypervisors()
        HYPERVISORS.update(loaded)
        return loaded, logs.output

    def test_the_server_still_starts_and_says_what_is_wrong(self) -> None:
        for how, reason in (("corrupt", "the file does not hold a valid key"), ("unreadable", "permission denied")):
            with self.subTest(how=how):
                self._break_key(how)

                loaded, logs = self._load()

                self.assertEqual(len(logs), 1)
                self.assertIn(str(self.key_path), logs[0])
                self.assertIn(reason, logs[0])
                self.assertIn("inventories vCenter-Prod cannot connect", logs[0])
                self.assertIn("restart the server", logs[0])
                self.assertEqual(loaded["inv-1"]["metadata"]["credentials"]["password"], self.encrypted)
                self.assertEqual(loaded["inv-2"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
                self.assertEqual(self.db.upserts, [])

    def test_status_updates_still_write_the_stored_form_unchanged(self) -> None:
        self._break_key("corrupt")
        self._load()

        mongo_db_wrapper.update_hypervisor_fields("inv-1", {"status": "unavailable"})
        mongo_db_wrapper.update_hypervisor_fields("inv-2", {"status": "available"})

        stored = self.db.collections["hypervisors"]
        self.assertEqual(stored["inv-1"]["metadata"]["credentials"]["password"], self.encrypted)
        self.assertEqual(stored["inv-1"]["status"], "unavailable")
        self.assertEqual(stored["inv-2"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)

    def test_a_new_password_is_refused_rather_than_stored_in_clear(self) -> None:
        from python.srv.services.api_errors import ApiError

        self._break_key("corrupt")
        self._load()
        before = copy.deepcopy(self.db.collections["hypervisors"]["inv-1"])

        with self.assertRaises(ApiError) as raised:
            mongo_db_wrapper.update_hypervisor_fields(
                "inv-1", {"metadata": {**HYPERVISORS["inv-1"]["metadata"], "credentials": {"username": "administrator@vsphere.local", "password": "New-Secret-1"}}}
            )
        with self.assertRaises(ApiError):
            mongo_db_wrapper.persist_hypervisor({**_inventory(password="New-Secret-2"), "id": "inv-3", "name": "vCenter-New"})

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "credential_key_unavailable")
        self.assertIn(str(self.key_path), raised.exception.message)
        self.assertNotIn("New-Secret", self.db.everything_written())
        self.assertEqual(self.db.collections["hypervisors"]["inv-1"], before)
        self.assertEqual(HYPERVISORS["inv-1"]["metadata"]["credentials"]["password"], self.encrypted)
        self.assertNotIn("inv-3", HYPERVISORS)

    def test_the_connection_check_points_at_the_key_file(self) -> None:
        from python.srv.plugins.vcenter import vcenter

        self._break_key("corrupt")
        self._load()
        node = vcenter.VCenterNode("vc.local", "administrator@vsphere.local", HYPERVISORS["inv-1"]["metadata"]["credentials"]["password"])

        with patch.object(vcenter, "SmartConnect") as smart_connect:
            with self.assertRaises(credential_encryption.StoredCredentialUnreadable) as raised:
                node.connect()

        smart_connect.assert_not_called()
        self.assertIn(str(self.key_path), str(raised.exception))
        self.assertIn("restart the server", str(raised.exception))

    def test_once_the_key_file_is_good_again_a_restart_reads_everything(self) -> None:
        self._break_key("corrupt")
        self._load()
        self.key_path.write_bytes(self.good_key)
        credential_encryption.reset_cipher()

        with self.assertNoLogs(mongo_db_wrapper.LOGGER, level="ERROR"):
            loaded = mongo_db_wrapper.load_registered_hypervisors()

        self.assertEqual(loaded["inv-1"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
        self.assertEqual(loaded["inv-2"]["metadata"]["credentials"]["password"], VCENTER_PASSWORD)
        self.assertTrue(credential_encryption.is_encrypted(self.db.collections["hypervisors"]["inv-2"]["metadata"]["credentials"]["password"]))


if __name__ == "__main__":
    unittest.main()
