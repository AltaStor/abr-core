# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest

from python.srv.api.schemas import ApiErrorDTO, RepositoryDTO
from python.srv.backup_server import app
from python.srv.services.api_errors import api_error

DOCUMENTED = (
    ("/api/v1/repositories", "post"),
    ("/api/v1/repositories/{repository_id}", "patch"),
    ("/api/v1/repositories/test-smb", "post"),
)


class RefusalListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = app.openapi()

    def test_create_edit_and_test_smb_list_both_smb_refusals_with_the_error_body(self) -> None:
        for path, method in DOCUMENTED:
            with self.subTest(f"{method} {path}"):
                refused = self.spec["paths"][path][method]["responses"]["422"]
                body = refused["content"]["application/json"]

                self.assertEqual(body["schema"], {"$ref": "#/components/schemas/ApiErrorDTO"})
                for code in ("share_mount_failed", "invalid_repository", "validation_error"):
                    self.assertIn(code, refused["description"])
                    self.assertEqual(body["examples"][code]["value"]["error"]["code"], code)

    def test_the_mount_errors_a_client_meets_are_named(self) -> None:
        description = self.spec["paths"]["/api/v1/repositories/test-smb"]["post"]["responses"]["422"]["description"]

        for number in ("13", "2", "113", "111"):
            self.assertIn(f" {number} ", description)

    def test_every_example_is_a_body_the_server_sends(self) -> None:
        examples = self.spec["paths"]["/api/v1/repositories"]["post"]["responses"]["422"]["content"]["application/json"]["examples"]

        for name, example in examples.items():
            with self.subTest(name):
                ApiErrorDTO.model_validate(example["value"])
        ApiErrorDTO.model_validate(api_error(422, "share_mount_failed", "mount error 13", repository="r").to_payload())

    def test_other_routes_keep_their_responses(self) -> None:
        responses = self.spec["paths"]["/api/v1/repositories/test-s3"]["post"]["responses"]

        self.assertEqual(responses["422"]["content"]["application/json"]["schema"], {"$ref": "#/components/schemas/HTTPValidationError"})


class ManagedMountFieldTests(unittest.TestCase):
    def test_managed_mount_is_a_documented_field_of_the_repository_metadata(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        metadata = schemas["RepositoryDTO"]["properties"]["metadata"]
        field = schemas["RepositoryMetadataDTO"]["properties"]["managed_mount"]

        self.assertIn({"$ref": "#/components/schemas/RepositoryMetadataDTO"}, metadata["anyOf"])
        self.assertEqual(field["type"], "boolean")
        self.assertIn("/run/altastor-backup-mounts/<repository id>", field["description"])

    def test_the_answer_keeps_every_metadata_key_and_adds_none(self) -> None:
        stored = {"server": "files01", "share": "backups", "username": "svc", "password": "********", "managed_mount": True, "last_scan_at": None}
        repository = {
            "id": "r1", "name": "R", "type": "smb", "status": "available", "created_at": "t", "updated_at": "t",
            "storage": {"available": True}, "metadata": stored,
        }

        dumped = RepositoryDTO.model_validate(repository).model_dump(exclude_unset=True)

        self.assertEqual(dumped["metadata"], stored)
        s3 = {**repository, "metadata": {"s3": {"bucket": "b"}, "last_scan_at": None}}
        self.assertEqual(RepositoryDTO.model_validate(s3).model_dump(exclude_unset=True)["metadata"], s3["metadata"])


if __name__ == "__main__":
    unittest.main()
