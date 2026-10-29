# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, SESSIONS, TASKS, USERS
from python.srv.services import abr_v41, auth_service
from python.srv.services.api_errors import ApiError


class _Api(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, SESSIONS, TASKS, USERS):
            store.clear()
            self.addCleanup(store.clear)
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user", "role": "administrator"}
        self.addCleanup(app.dependency_overrides.pop, require_session, None)
        self.client = self.enterContext(TestClient(app))

    def _refused(self, call, *args, **kwargs) -> ApiError:
        with self.assertRaises(ApiError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.status_code, 422, raised.exception.message)
        return raised.exception


class UserNameAndPasswordTests(_Api):
    def test_an_empty_or_blank_user_name_is_refused(self) -> None:
        before = dict(USERS)
        for body in ({"name": "", "password": "Abcde1234", "fullName": "AQA"}, {"name": "   ", "password": "Abcde1234"}, {"password": "Abcde1234"}):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/users", json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_user")
        self.assertEqual(USERS, before)

    def test_a_user_name_longer_than_64_characters_is_refused(self) -> None:
        error = self._refused(auth_service.create_user, {"name": "n" * 65, "password": "Abcde1234"})
        self.assertIn("at most 64", error.message)
        self.assertEqual(auth_service.create_user({"name": "n" * 64, "password": "Abcde1234", "role": "auditor"})["name"], "n" * 64)

    def test_a_password_longer_than_128_characters_is_refused(self) -> None:
        error = self._refused(auth_service.create_user, {"name": "alice", "password": "A1" * 50000})
        self.assertIn("at most 128", error.message)
        self.assertEqual(auth_service.create_user({"name": "alice", "password": "a" * 128, "role": "auditor"})["name"], "alice")

    def test_a_blank_password_or_one_equal_to_the_user_name_is_refused(self) -> None:
        self.assertIn("only spaces", self._refused(auth_service.create_user, {"name": "alice", "password": " " * 8}).message)
        self.assertIn("user name", self._refused(auth_service.create_user, {"name": "alice.admin", "password": "Alice.Admin"}).message)

    def test_the_bootstrap_admin_is_still_created_and_flagged(self) -> None:
        user = auth_service.create_user({"name": "root", "password": "root", "role": "administrator"}, enforce_password_policy=False)
        self.assertTrue(user["passwordPolicyExempt"])


class ChangePasswordTests(_Api):
    def setUp(self) -> None:
        super().setUp()
        self.user = auth_service.create_user({"name": "alice", "password": "InitPass123", "role": "auditor"})

    def _change(self, new_password: str):
        return auth_service.change_password(self.user["id"], "InitPass123", new_password, keep_token="token")

    def test_the_current_password_cannot_be_set_again(self) -> None:
        self.assertIn("differ from the current one", self._refused(self._change, "InitPass123").message)

    def test_a_new_password_follows_the_same_rules_as_a_new_user(self) -> None:
        for new_password, expected in (("short", "at least 8"), ("x" * 129, "at most 128"), (" " * 9, "only spaces"), ("ALICE", "at least 8"), ("Alice123", None)):
            with self.subTest(new_password=new_password[:12]):
                if expected:
                    self.assertIn(expected, self._refused(self._change, new_password).message)

    def test_a_new_password_equal_to_the_user_name_is_refused(self) -> None:
        operator = auth_service.create_user({"name": "operator1", "password": "InitPass123", "role": "auditor"})
        error = self._refused(auth_service.change_password, operator["id"], "InitPass123", "Operator1", keep_token="token")
        self.assertIn("user name", error.message)

    def test_a_different_password_is_accepted(self) -> None:
        self.assertEqual(self._change("NewPass456")["name"], "alice")


class JobFieldTests(_Api):
    def setUp(self) -> None:
        super().setUp()
        registrable = patch.object(abr_v41, "assert_inventory_registrable", return_value={})
        registrable.start()
        self.addCleanup(registrable.stop)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "smb", "metadata": {"server": "nas.example", "share": "backups"}, "path": "/tmp/repo-1"}
        self.inventory = abr_v41.create_inventory_record(
            {"name": "hv", "type": "hyper-v", "address": "192.168.10.10", "username": "administrator", "password": "secret"}
        )
        now = datetime.now(timezone.utc).isoformat()
        RESOURCES_BY_HV[str(self.inventory["id"])] = {
            ("virtual_machine", f"id-{name}"): {"resource_type": "virtual_machine", "external_id": f"id-{name}", "name": name, "payload": {}, "discovered_at": now}
            for name in ("web-01", "db-01")
        }

    def _payload(self, **overrides) -> dict:
        payload = {
            "name": "Nightly",
            "vms": ["web-01"],
            "inventory_id": self.inventory["id"],
            "repo_id": "repo-1",
            "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
        }
        payload.update(overrides)
        return payload

    def test_retention_days_must_be_a_whole_number_within_limits(self) -> None:
        for value, expected in (("7", "whole number"), (1.5, "whole number"), (None, "whole number"), (True, "whole number"), (0, "greater than 0"), (-1, "greater than 0"), (3651, "at most 3650"), (100000, "at most 3650")):
            with self.subTest(value=value):
                self.assertIn(expected, self._refused(abr_v41.create_backup_job, self._payload(retention_days=value)).message)
        self.assertEqual(abr_v41.create_backup_job(self._payload(retention_days=3650))["retention_days"], 3650)
        self.assertEqual(abr_v41.create_backup_job(self._payload(name="Default"))["retention_days"], 14)

    def test_compression_level_must_be_a_whole_number(self) -> None:
        for value in (1.5, "3", True):
            with self.subTest(value=value):
                self._refused(abr_v41.create_backup_job, self._payload(task_parameters={"compression_level": value}))
        job = abr_v41.create_backup_job(self._payload(task_parameters={"compression_level": 5}))
        self.assertEqual(job["task_parameters"]["compression_level"], 5)

    def test_gfs_counts_must_be_whole_numbers_within_limits(self) -> None:
        self.assertIn("at most 520", self._refused(abr_v41.create_backup_job, self._payload(gfs={"weekly": 1000000000, "monthly": 1, "yearly": 1})).message)
        self.assertIn("integers", self._refused(abr_v41.create_backup_job, self._payload(gfs={"weekly": "1"})).message)
        self.assertIn(">= 0", self._refused(abr_v41.create_backup_job, self._payload(gfs={"yearly": -1})).message)
        job = abr_v41.create_backup_job(self._payload(gfs={"weekly": 520, "monthly": 120, "yearly": 99}))
        self.assertEqual((job["gfs"]["weekly"], job["gfs"]["monthly"], job["gfs"]["yearly"]), (520, 120, 99))

    def test_a_vm_listed_twice_is_refused(self) -> None:
        for vms in (["web-01", "web-01"], ["web-01", "id-web-01"]):
            with self.subTest(vms=vms):
                self.assertIn("more than once", self._refused(abr_v41.create_backup_job, self._payload(vms=vms)).message)
        self.assertEqual(len(abr_v41.create_backup_job(self._payload(vms=["web-01", "db-01"]))["vm_ids"]), 2)

    def test_a_schedule_time_zone_must_exist(self) -> None:
        bad = {"type": "daily", "time": "03:00", "timezone": "Mars/Phobos"}
        self.assertIn("Mars/Phobos", self._refused(abr_v41.create_backup_job, self._payload(schedule=bad)).message)
        self._refused(abr_v41.create_backup_job, self._payload(active_full_schedule={"type": "weekly", "days": ["sunday"], "time": "02:00", "timezone": "Nowhere/Land"}))
        job = abr_v41.create_backup_job(self._payload(schedule={"type": "daily", "time": "03:00", "timezone": "Europe/Kyiv"}))
        self.assertEqual(job["schedule"]["timezone"], "Europe/Kyiv")

    def test_job_edit_checks_the_same_fields(self) -> None:
        job = abr_v41.create_backup_job(self._payload())
        for change, expected in (
            ({"retention_days": "7"}, "whole number"),
            ({"retention_days": 4000}, "at most 3650"),
            ({"gfs_weekly": 521}, "at most 520"),
            ({"vms": ["web-01", "web-01"]}, "more than once"),
            ({"schedule": {"type": "daily", "time": "03:00", "timezone": "Mars/Phobos"}}, "Mars/Phobos"),
        ):
            with self.subTest(change=change):
                self.assertIn(expected, self._refused(abr_v41.edit_job_record, str(job["id"]), change).message)

    def test_the_http_api_answers_422_for_these_fields(self) -> None:
        for value in (100000, "7", 1.5, None):
            with self.subTest(retention_days=value):
                response = self.client.post("/api/v1/jobs", json=self._payload(retention_days=value))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_job")
        self.assertEqual(JOBS, {})

    def test_the_http_api_refuses_a_job_edit_with_loose_numbers(self) -> None:
        job = abr_v41.create_backup_job(self._payload())
        for change in ({"retention_days": "7"}, {"retention_days": 1.5}, {"gfs_weekly": "2"}):
            with self.subTest(change=change):
                response = self.client.patch(f"/api/v1/jobs/{job['id']}", json=change)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(JOBS[str(job["id"])]["retention_days"], 14)


class SessionTypeFilterTests(_Api):
    def setUp(self) -> None:
        super().setUp()
        for index, task_type in enumerate(("backup", "restore", "verify", "delete", "backup")):
            TASKS[f"s{index}"] = {"id": f"s{index}", "task_type": task_type, "status": "completed", "created_at": f"2026-10-11T0{index}:00:00+00:00", "result": {}}

    def test_sessions_can_be_filtered_by_type(self) -> None:
        for session_type, expected in (("backup", {"s0", "s4"}), ("restore", {"s1"}), ("verify", {"s2"}), ("delete", {"s3"})):
            with self.subTest(session_type=session_type):
                response = self.client.get("/api/v1/sessions", params={"type": session_type})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual({item["id"] for item in response.json()["items"]}, expected)

    def test_the_type_and_run_filters_apply_together_and_survive_paging(self) -> None:
        for session_id in ("s0", "s1", "s4"):
            TASKS[session_id]["run_id"] = "run-1"
        TASKS["s2"]["run_id"] = "run-2"

        for params, expected in (
            ({"type": "backup", "run_id": "run-1"}, {"s0", "s4"}),
            ({"type": "restore", "run_id": "run-1"}, {"s1"}),
            ({"type": "verify", "run_id": "run-1"}, set()),
            ({"run_id": "run-1"}, {"s0", "s1", "s4"}),
            ({"type": "backup"}, {"s0", "s4"}),
        ):
            with self.subTest(**params):
                response = self.client.get("/api/v1/sessions", params=params)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual({item["id"] for item in response.json()["items"]}, expected)

        first = self.client.get("/api/v1/sessions", params={"type": "backup", "run_id": "run-1", "limit": 1}).json()
        self.assertEqual(first["total"], 2)
        self.assertIn("type=backup", first["next"])
        self.assertIn("run_id=run-1", first["next"])
        second = self.client.get(first["next"]).json()
        self.assertEqual({item["id"] for item in first["items"] + second["items"]}, {"s0", "s4"})

    def test_an_unknown_type_is_refused_instead_of_ignored(self) -> None:
        response = self.client.get("/api/v1/sessions", params={"type": "garbage"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.client.get("/api/v1/sessions").json()["items"]), 5)


class DashboardRecentActivityTests(_Api):
    def test_each_session_is_labelled_by_its_own_type_and_name(self) -> None:
        JOBS["job-1"] = {"id": "job-1", "name": "Nightly"}
        TASKS["b"] = {"id": "b", "task_type": "backup", "job_id": "job-1", "status": "completed", "resource_type": "virtual_machine", "parameters": {"vm_name": "web-01"}, "result": {}}
        TASKS["r"] = {"id": "r", "task_type": "restore", "status": "completed", "resource_type": "virtual_machine", "parameters": {"vm_name": "web-01", "target_vm_name": "web-01-restored"}, "result": {}}
        TASKS["d"] = {"id": "d", "task_type": "delete", "status": "completed", "resource_type": "virtual_machine", "parameters": {"vm_name": "db-01"}, "result": {}}
        TASKS["v"] = {"id": "v", "task_type": "verify", "status": "completed", "parameters": {}, "result": {}}

        items = {item["id"]: item for item in self.client.get("/api/v1/dashboard/summary").json()["recent_jobs"]}

        self.assertEqual({key: item["type"] for key, item in items.items()}, {"b": "Backup", "r": "Restore", "d": "Delete", "v": "Verify"})
        self.assertEqual(items["b"]["name"], "Nightly")
        self.assertNotIn(items["r"]["name"], {"Unknown Job", "Backup"})
        self.assertEqual(items["d"]["name"], "db-01")
        self.assertEqual(items["v"]["name"], "Verify")


if __name__ == "__main__":
    unittest.main()
