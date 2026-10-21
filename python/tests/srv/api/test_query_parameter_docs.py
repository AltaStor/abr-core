# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.backup_server import app


class QueryParameterDocsTests(unittest.TestCase):
    def test_every_query_parameter_says_what_it_takes(self) -> None:
        undescribed = [
            f"{method.upper()} {path} ?{parameter['name']}"
            for path, operations in app.openapi()["paths"].items()
            for method, operation in operations.items()
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query" and not str(parameter.get("description") or "").strip()
        ]

        self.assertEqual(undescribed, [])

    def test_the_session_job_filter_says_it_takes_the_name_not_the_id(self) -> None:
        parameters = app.openapi()["paths"]["/api/v1/sessions"]["get"]["parameters"]
        job = next(parameter for parameter in parameters if parameter["name"] == "job")

        self.assertIn("name, not its ID", job["description"])

    def test_request_fields_that_take_a_name_an_id_or_a_switch_say_which(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        expected = {
            ("RunRetentionRequest", "dry_run"): "omitted runs it and deletes",
            ("CreateJobRequest", "inventory"): "name, not its ID",
            ("CreateJobRequest", "inventory_id"): "ID, not its name",
            ("CreateJobRequest", "repo"): "name, not its ID",
            ("CreateJobRequest", "vms"): "name",
            ("UpdateJobRequest", "active_full_enabled"): "null or omitted leaves it",
            ("StartJobRequest", "target_repository_id"): "ID, not name",
            ("StopSessionRequest", "force"): "works the same",
            ("StartRestoreRequest", "target_host"): "ID or its name",
            ("CreateUserRequest", "role"): "ID or name",
        }
        for (model, field), phrase in expected.items():
            with self.subTest(model=model, field=field):
                self.assertIn(phrase, schemas[model]["properties"][field].get("description", ""))


if __name__ == "__main__":
    unittest.main()
