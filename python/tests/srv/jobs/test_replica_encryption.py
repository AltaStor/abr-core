# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.services.internal.replication.replica_update import _repository_and_key
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
    ReplicationValidationError,
)
from python.tests.srv.jobs.test_replica_update import _UpdateCase

UPDATE = "python.srv.services.internal.replication.replica_update"
KEY = "placeholder-job-key-one"
NEW_KEY = "placeholder-job-key-two"
STORED_ENCRYPTED = "abr-enc:v1:placeholder-that-cannot-be-decrypted"


class _KeyCase(_UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(REPOSITORIES.clear)
        REPOSITORIES["repo"] = {"id": "repo", "name": "local-repo", "type": "local", "metadata": {}}
        for point in BACKUPS.values():
            point["repository_id"] = "repo"
        self.keys: list[str] = []
        original = self.platform.write_increments

        def write(node, state, disk, layers, repository, key, **kwargs):
            self.keys.append(key)
            return original(node, state, disk, layers, repository, key, **kwargs)

        self.platform.write_increments = write
        patcher = patch(f"{UPDATE}._repository_and_key", _repository_and_key)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _encrypt_chain(self, *, job_key: str | None, point_key: str | None = None) -> None:
        parameters: dict[str, Any] = {"encryption_enabled": job_key is not None}
        if job_key is not None:
            parameters.update({"encryption_key": job_key, "encryption_keys": [{"key_id": 1, "key": job_key}]})
        JOBS["src"]["task_parameters"] = parameters
        for point in BACKUPS.values():
            point["encryption_enabled"] = True
            point["encryption_key_id"] = 1
            if point_key is not None:
                point["encryption_key"] = point_key


class EncryptedChainTests(_KeyCase):
    def test_every_incremental_of_an_encrypted_chain_is_written_with_the_job_key(self) -> None:
        self._encrypt_chain(job_key=KEY)

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(self.keys, [KEY, KEY])

    def test_after_a_key_rotation_the_points_of_the_old_chain_are_written_with_their_own_key(self) -> None:
        self._encrypt_chain(job_key=KEY)
        JOBS["src"]["task_parameters"].update(
            {"encryption_key": NEW_KEY, "encryption_keys": [{"key_id": 1, "key": KEY}, {"key_id": 2, "key": NEW_KEY}]}
        )

        self._run()

        self.assertEqual(self.keys, [KEY, KEY])

    def test_an_unencrypted_chain_is_written_without_a_key(self) -> None:
        self._run()

        self.assertEqual(self.keys, ["", ""])

    def test_a_chain_whose_job_no_longer_encrypts_uses_the_key_the_points_kept(self) -> None:
        self._encrypt_chain(job_key=None, point_key=KEY)

        self._run()

        self.assertEqual(self.keys, [KEY, KEY])

    def test_a_key_that_cannot_be_read_fails_the_run_before_the_replica_is_touched(self) -> None:
        cases = {
            "the stored key cannot be decrypted": {"job_key": None, "point_key": STORED_ENCRYPTED},
            "no key is kept anywhere": {"job_key": None, "point_key": None},
        }
        for label, keys in cases.items():
            with self.subTest(label):
                self.setUp()
                self._encrypt_chain(**keys)
                with self.assertRaisesRegex(ReplicationValidationError, "is encrypted and its key cannot be read"):
                    self._run()
                self.assertEqual(self.platform.calls, [])
                self.assertEqual(self._replica()["point_id"], "rp-1")

    def test_a_repository_that_is_gone_fails_the_run_as_a_validation_error(self) -> None:
        REPOSITORIES.clear()

        with self.assertRaisesRegex(ReplicationValidationError, "is no longer registered"):
            self._run()
        self.assertEqual(self.platform.calls, [])

    def test_an_unmounted_share_fails_the_run_without_touching_the_replica(self) -> None:
        with patch(
            "python.srv.services.abr_v41.unmounted_share_message", return_value="The share //nas/abr is not mounted at /mnt/abr"
        ), self.assertRaises(ReplicationError) as raised:
            self._run()
        self.assertNotIsInstance(raised.exception, ReplicationValidationError)
        self.assertIn("is not mounted", str(raised.exception))
        self.assertEqual(self.platform.calls, [])


if __name__ == "__main__":
    unittest.main()
