# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import inspect
import unittest
from typing import get_args
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from python.srv.api import schemas
from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core import mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import JOBS
from python.srv.services import abr_v41, audit_service, rbac
from python.tests.srv.rbac.rbac_case import RbacApiCase

NOT_BOOLEANS = ("true", "false", "", "0", 1, 0, 2.5, [], {})
NESTED_MODELS = (
    schemas.ScheduleRequest,
    schemas.GfsRequest,
    schemas.TaskParametersRequest,
    schemas.S3Request,
    schemas.StartRestoreOptions,
)


def _boolean_keys(model: type[BaseModel]) -> list[str]:
    return [name for name, field in model.model_fields.items() if schemas.LaxBool in get_args(field.annotation)]


def _request_models() -> list[type[BaseModel]]:
    return [
        model
        for _name, model in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(model, BaseModel) and model.__module__ == schemas.__name__ and model.__name__.endswith("Request")
    ]


class EveryBooleanRequestFieldIsStrictTests(unittest.TestCase):
    def test_top_level_booleans_take_only_true_false_or_null(self) -> None:
        checked = 0
        for model in _request_models():
            for key in _boolean_keys(model):
                checked += 1
                for value in NOT_BOOLEANS:
                    with self.subTest(model=model.__name__, key=key, value=value), self.assertRaises(ValidationError):
                        model.model_validate({key: value})
                for value in (True, False, None):
                    self.assertEqual(model.model_validate({key: value}).model_dump()[key], value)
        self.assertGreaterEqual(checked, 30)

    def test_nested_booleans_take_only_true_false_or_null(self) -> None:
        checked = 0
        for model in _request_models():
            for name, field in model.model_fields.items():
                for nested in NESTED_MODELS:
                    if nested.__name__ not in repr(field.annotation):
                        continue
                    wrap = (lambda item: [item]) if "list[" in repr(field.annotation) else (lambda item: item)
                    for key in _boolean_keys(nested):
                        checked += 1
                        for value in NOT_BOOLEANS:
                            with self.subTest(model=model.__name__, field=name, key=key, value=value), self.assertRaisesRegex(
                                ValidationError, f"{key} must be true or false"
                            ):
                                model.model_validate({name: wrap({key: value})})
                        for value in (True, False, None):
                            model.model_validate({name: wrap({key: value})})
        self.assertGreaterEqual(checked, 20)

    def test_a_schedule_given_as_text_is_still_accepted(self) -> None:
        self.assertEqual(schemas.UpdateJobRequest.model_validate({"schedule": "daily 01:00"}).schedule, "daily 01:00")


class BooleanFieldsThroughTheApiTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        self.addCleanup(JOBS.clear)
        for target in (
            patch.object(mongo_db_wrapper, "get_database", return_value=None),
            patch.dict(app.dependency_overrides, {require_session: lambda: {"user_id": "t", "user_name": "t", "role": "administrator"}}),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self.addCleanup(self.client_cm.__exit__, None, None, None)
        JOBS["job-1"] = abr_v41.normalize_job_record(
            {
                "id": "job-1",
                "name": "nightly",
                "job_type": "backup",
                "inventory_id": "inv-1",
                "repo_id": "repo-1",
                "vm_ids": ["vm-1"],
                "retention_days": 7,
                "active_full_schedule": {"kind": "weekly", "weekdays": ["Sun"], "time": "02:00", "timezone": "UTC", "enabled": True},
            }
        )

    def test_a_dry_run_that_is_not_a_boolean_is_refused_and_deletes_nothing(self) -> None:
        with patch.object(abr_v41, "run_job_retention") as retention:
            for value in NOT_BOOLEANS:
                with self.subTest(value=value):
                    response = self.client.post("/api/v1/jobs/job-1/retention", json={"dry_run": value})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["error"]["code"], "validation_error")
            retention.assert_not_called()

    def test_a_boolean_dry_run_or_none_still_runs(self) -> None:
        with patch.object(abr_v41, "run_job_retention", return_value={}) as retention:
            for body in ({"dry_run": True}, {"dry_run": False}, {"dry_run": None}, {}):
                self.assertEqual(self.client.post("/api/v1/jobs/job-1/retention", json=body).status_code, 200)
        self.assertEqual([call.kwargs["dry_run"] for call in retention.call_args_list], [True, False, False, False])

    def test_a_schedule_switch_that_is_not_a_boolean_leaves_the_job_alone(self) -> None:
        before = dict(JOBS["job-1"])
        for body in (
            {"active_full_enabled": "false"},
            {"active_full_enabled": 0},
            {"active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00", "enabled": "false"}},
            {"task_parameters": {"dedup": "no"}},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch("/api/v1/jobs/job-1", json=body).status_code, 422)
        self.assertEqual(JOBS["job-1"], before)

    def test_switching_on_a_schedule_that_is_already_on_answers_200_and_keeps_its_next_run(self) -> None:
        next_run = JOBS["job-1"].get("active_full_next_run_at")

        response = self.client.patch("/api/v1/jobs/job-1", json={"active_full_enabled": True})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["active_full_schedule"]["enabled"])
        self.assertEqual(JOBS["job-1"].get("active_full_next_run_at"), next_run)


class RefusedBooleanIsAuditedTests(RbacApiCase):
    def test_a_refused_dry_run_is_audited_as_a_422_with_its_body(self) -> None:
        JOBS["job-1"] = abr_v41.normalize_job_record(
            {"id": "job-1", "name": "nightly", "job_type": "backup", "inventory_id": "inv-1", "repo_id": "repo-1", "vm_ids": ["vm-1"]}
        )

        with patch.object(abr_v41, "run_job_retention") as retention:
            response = self.client.post(
                "/api/v1/jobs/job-1/retention", json={"dry_run": ""}, headers=self.headers(rbac.ADMINISTRATOR)
            )

        self.assertEqual(response.status_code, 422)
        retention.assert_not_called()
        [entry] = self.audit(function_id="job.retention")
        self.assertEqual((entry["result"], entry["status"]), (audit_service.FAILED, 422))
        self.assertEqual(entry["error"]["code"], "validation_error")
        self.assertEqual(entry["parameters"]["body"], {"dry_run": ""})


if __name__ == "__main__":
    unittest.main()
