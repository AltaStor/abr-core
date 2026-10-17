# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import REPOSITORIES
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

S3_PAYLOAD = {
    "name": "ObjectRepo",
    "type": "s3",
    "s3": {
        "bucket": "altastor-backups",
        "prefix": "lab",
        "region": "us-east-1",
        "endpoint_url": "http://minio.local:9000",
        "access_key": "MINIOACCESS",
        "secret_key": "super-secret-key",
        "path_style": True,
        "verify_tls": False,
    },
}


def _reachable_endpoint():
    return patch.object(abr_v41, "s3_endpoint_connect_error", return_value=None)


class S3RegistrationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self._reachable = _reachable_endpoint()
        self._reachable.start()
        self.addCleanup(self._reachable.stop)
        self._bucket = patch.object(abr_v41, "s3_bucket_is_reachable", return_value=True)
        self._bucket.start()
        self.addCleanup(self._bucket.stop)
        self._lock = patch.object(abr_v41, "_s3_immutability_checks", return_value=([], None))
        self._lock.start()
        self.addCleanup(self._lock.stop)

    def tearDown(self) -> None:
        REPOSITORIES.clear()

    def test_an_unreachable_endpoint_is_refused_before_anything_is_written(self) -> None:
        self._reachable.stop()
        self.addCleanup(self._reachable.start)
        with patch.object(
            abr_v41, "s3_endpoint_connect_error", return_value="connection refused"
        ), patch.object(abr_v41, "probe_repository_access") as probe:
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(dict(S3_PAYLOAD))

        self.assertEqual(caught.exception.code, "repository_validation_failed")
        self.assertIn("not reachable", caught.exception.message)
        probe.assert_not_called()
        self.assertEqual(list(REPOSITORIES), [])

    def test_an_unusable_target_is_refused_and_nothing_is_created(self) -> None:
        with patch.object(
            abr_v41, "probe_repository_access", side_effect=StorageToolError("InvalidAccessKeyId")
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(dict(S3_PAYLOAD))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "repository_validation_failed")
        self.assertEqual(list(REPOSITORIES), [])

    def test_the_refusal_names_the_check_that_failed(self) -> None:
        with patch.object(
            abr_v41, "probe_repository_access", side_effect=StorageToolError("InvalidAccessKeyId")
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(dict(S3_PAYLOAD))

        checks = caught.exception.details.get("checks") or []
        failed = [check for check in checks if check.get("status") == "failed"]
        self.assertTrue(failed)
        self.assertIn("InvalidAccessKeyId", str(failed[-1].get("message")))
        self.assertEqual(failed[-1].get("name"), "connectivity")

    def test_a_usable_target_is_created_and_records_the_check(self) -> None:
        with patch.object(abr_v41, "probe_repository_access"):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD))

        self.assertEqual(repository["status"], "available")
        self.assertTrue(repository["storage"]["available"])
        self.assertIsNone(repository["storage"]["error"])
        stored = REPOSITORIES[repository["id"]]
        self.assertEqual(stored["last_verify"]["status"], "success")

    def test_a_failed_verify_is_remembered_on_the_record(self) -> None:
        with patch.object(abr_v41, "probe_repository_access"):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD))

        with patch.object(
            abr_v41, "probe_repository_access", side_effect=StorageToolError("connection refused")
        ):
            verified = abr_v41.verify_repository(repository["id"])

        self.assertEqual(verified["status"], "error")
        again = abr_v41.resolve_repository(record_id=repository["id"])
        self.assertEqual(again["status"], "unavailable")
        self.assertFalse(again["storage"]["available"])
        self.assertIn("validation failed", str(again["storage"]["error"]))

    def test_a_repository_that_recovers_goes_back_to_available(self) -> None:
        with patch.object(abr_v41, "probe_repository_access"):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD))
        with patch.object(
            abr_v41, "probe_repository_access", side_effect=StorageToolError("connection refused")
        ):
            abr_v41.verify_repository(repository["id"])
        with patch.object(abr_v41, "probe_repository_access"):
            abr_v41.verify_repository(repository["id"])

        again = abr_v41.resolve_repository(record_id=repository["id"])
        self.assertEqual(again["status"], "available")
        self.assertTrue(again["storage"]["available"])
        self.assertIsNone(again["storage"]["error"])

    def test_editing_onto_a_broken_target_is_refused(self) -> None:
        with patch.object(abr_v41, "probe_repository_access"):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD))

        with patch.object(
            abr_v41, "probe_repository_access", side_effect=StorageToolError("NoSuchBucket")
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.edit_repository_record(repository["id"], {"bucket": "gone"})

        self.assertEqual(caught.exception.code, "repository_validation_failed")
        self.assertEqual(REPOSITORIES[repository["id"]]["metadata"]["s3"]["bucket"], "altastor-backups")

    def test_editing_a_description_does_not_reprobe_the_storage(self) -> None:
        with patch.object(abr_v41, "probe_repository_access"):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD))

        with patch.object(abr_v41, "probe_repository_access") as probe:
            abr_v41.edit_repository_record(repository["id"], {"description": "renamed"})

        probe.assert_not_called()


class BucketProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self._reachable = _reachable_endpoint()
        self._reachable.start()
        self.addCleanup(self._reachable.stop)

    def tearDown(self) -> None:
        REPOSITORIES.clear()

    def test_a_missing_bucket_is_refused_without_the_flag(self) -> None:
        with patch.object(abr_v41, "s3_bucket_is_reachable", return_value=False), patch.object(
            abr_v41, "probe_repository_access"
        ) as probe:
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(dict(S3_PAYLOAD))

        self.assertEqual(caught.exception.code, "repository_validation_failed")
        self.assertIn("could not be opened", caught.exception.message)
        self.assertIn("--create-bucket", caught.exception.message)
        self.assertNotIn("AltaStor", caught.exception.message)
        probe.assert_not_called()
        self.assertEqual(list(REPOSITORIES), [])

    def test_the_flag_provisions_the_bucket_and_says_so(self) -> None:
        with patch.object(abr_v41, "s3_bucket_is_reachable", return_value=False), patch.object(
            abr_v41, "probe_repository_access"
        ) as probe, patch.object(abr_v41, "_s3_immutability_checks", return_value=([], None)):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD, create_bucket=True))

        self.assertEqual(probe.call_args.kwargs["create_folder"], True)
        self.assertEqual(repository["status"], "available")
        self.assertTrue(any("did not exist and was created" in w for w in repository["warnings"]))
        self.assertTrue(any("immutable backups" in w for w in repository["warnings"]))

    def test_an_existing_bucket_is_not_announced_as_created(self) -> None:
        with patch.object(abr_v41, "s3_bucket_is_reachable", return_value=True), patch.object(
            abr_v41, "probe_repository_access"
        ), patch.object(abr_v41, "_s3_immutability_checks", return_value=([], None)):
            repository = abr_v41.create_repository_record(dict(S3_PAYLOAD, create_bucket=True))

        self.assertEqual(repository["warnings"], [])

    def test_the_flag_is_refused_together_with_immutability(self) -> None:
        payload = dict(S3_PAYLOAD, create_bucket=True)
        payload["s3"] = dict(payload["s3"], immutability={"mode": "governance", "retention_days": 30})

        with patch.object(abr_v41, "probe_repository_access") as probe:
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(payload)

        self.assertEqual(caught.exception.code, "repository_bucket_cannot_be_created")
        self.assertIn("Object Lock can only be turned on", caught.exception.message)
        self.assertNotIn("AltaStor", caught.exception.message)
        probe.assert_not_called()
        self.assertEqual(list(REPOSITORIES), [])

    def test_a_backup_never_provisions_the_bucket_behind_the_check(self) -> None:
        from python.srv.core.storage_tool import disk_tool_storage_args

        repository = {"type": "s3", "s3": dict(S3_PAYLOAD["s3"])}
        args = disk_tool_storage_args(repository, abr_path="vm-1/task-1", create_folder=False)

        self.assertNotIn("--create-folder", args)

    def test_a_local_backup_still_creates_its_task_folder(self) -> None:
        from python.srv.core.storage_tool import disk_tool_storage_args

        args = disk_tool_storage_args(
            {"type": "local", "path": "/srv/backups"}, abr_path="/srv/backups/vm-1/task-1", create_folder=True
        )

        self.assertIn("--create-folder", args)


class LocalRegistrationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self._root = tempfile.mkdtemp(prefix="abr-repo-validation-")

    def tearDown(self) -> None:
        REPOSITORIES.clear()
        shutil.rmtree(self._root, ignore_errors=True)

    def _path(self, name: str) -> str:
        path = Path(self._root) / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o750)
        return str(path)

    def test_a_usable_path_is_created(self) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepo", "type": "local", "path": self._path("good")}
        )

        self.assertEqual(repository["status"], "available")
        self.assertEqual(REPOSITORIES[repository["id"]]["last_verify"]["status"], "success")

    def test_a_path_that_does_not_exist_is_refused(self) -> None:
        missing = str(Path(self._root) / "missing")

        with self.assertRaises(ApiError) as caught:
            abr_v41.create_repository_record({"name": "LocalRepo", "type": "local", "path": missing})

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(list(REPOSITORIES), [])

    def test_a_path_that_cannot_be_written_is_refused(self) -> None:
        path = Path(self._path("readonly"))
        path.chmod(0o550)
        try:
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_repository_record(
                    {"name": "LocalRepo", "type": "local", "path": str(path)}
                )
        finally:
            path.chmod(0o750)

        self.assertEqual(caught.exception.code, "repository_validation_failed")
        self.assertEqual(list(REPOSITORIES), [])


if __name__ == "__main__":
    unittest.main()
