# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from python.srv.api._routing import function_id_of
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import AUDIT_RECORDS, JOBS, USERS
from python.srv.services import audit_service, rbac

from .rbac_case import ADMIN_PASSWORD, ROLE_USERS, USER_PASSWORD, RbacApiCase

# Functions the sweep does not run when they are allowed: they change the
# caller's own session or write files outside the catalog (the HTTPS
# certificate, a support bundle). Their denials are still checked.
NOT_RUN_WHEN_ALLOWED = {"auth.logout", "auth.extend_session", "auth.change_password", "tls.edit", "system.support_bundle"}
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def api_routes():
    """Every routed API operation, with its full path."""

    def walk(routes, prefix=""):
        for route in routes:
            if hasattr(route, "original_router"):
                yield from walk(route.original_router.routes, prefix + route.include_context.prefix)
            elif isinstance(route, APIRoute):
                yield prefix + route.path, route

    return list(walk(app.routes))


class RouteCoverageTests(unittest.TestCase):
    def test_every_api_route_executes_a_known_function(self) -> None:
        for path, route in api_routes():
            if not path.startswith("/api/"):
                continue
            with self.subTest(path=path, methods=sorted(route.methods)):
                self.assertIn(function_id_of(route), rbac.FUNCTIONS_BY_ID)

    def test_every_route_but_sign_in_is_authorized_before_it_runs(self) -> None:
        for path, route in api_routes():
            function_id = function_id_of(route)
            if function_id is None or function_id in rbac.PUBLIC_FUNCTIONS:
                continue
            with self.subTest(path=path):
                names = [dependency.call.__name__ for dependency in route.dependant.dependencies]
                self.assertIn(f"authorize_{function_id.replace('.', '_')}", names)


class PermissionSweepTests(RbacApiCase):
    def test_every_role_gets_exactly_its_matrix_on_every_route(self) -> None:
        # An allowed request with an empty body may still fail inside the
        # function; all that matters here is that it got that far.
        client = TestClient(app, raise_server_exceptions=False)
        for path, route in api_routes():
            function_id = function_id_of(route)
            if function_id is None or function_id in rbac.PUBLIC_FUNCTIONS:
                continue
            url = re.sub(r"\{[^}]+\}", "no-such-id", path)
            for method in sorted(route.methods):
                for role in ROLE_USERS:
                    allowed = rbac.role_allows(role, function_id)
                    if allowed and function_id in NOT_RUN_WHEN_ALLOWED:
                        continue
                    with self.subTest(method=method, path=path, role=role):
                        body = None if method == "GET" else {}
                        response = client.request(method, url, json=body, headers=self.headers(role))
                        if allowed:
                            self.assertNotEqual(response.status_code, 403, response.text)
                            continue
                        self.assertEqual(response.status_code, 403, response.text)
                        error = response.json()["error"]
                        self.assertEqual(error["code"], "permission_denied")
                        self.assertEqual(error["details"]["function_id"], function_id)
                        self.assertEqual(error["details"]["role"], role)


class DeniedOperationTests(RbacApiCase):
    def setUp(self) -> None:
        super().setUp()
        JOBS["job-1"] = {"id": "job-1", "name": "nightly"}

    def test_a_denied_operation_is_not_executed_and_is_audited(self) -> None:
        response = self.client.delete("/api/v1/jobs/job-1", headers=self.headers(rbac.BACKUP_OPERATOR))
        self.assertEqual(response.status_code, 403)
        self.assertIn("job-1", JOBS)

        [entry] = self.audit(function_id="job.delete")
        self.assertEqual(entry["result"], audit_service.DENIED)
        self.assertEqual((entry["user"], entry["role"]), ("olga", rbac.BACKUP_OPERATOR))
        self.assertEqual(entry["target"], {"type": "job", "id": "job-1", "name": "nightly"})
        self.assertEqual(entry["error"]["code"], "permission_denied")
        self.assertRegex(entry["timestamp"], TIMESTAMP)
        self.assertEqual(entry["status"], 403)

    def test_an_allowed_operation_runs_and_is_audited(self) -> None:
        response = self.client.delete("/api/v1/jobs/job-1", headers=self.headers(rbac.BACKUP_ADMINISTRATOR))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("job-1", JOBS)
        [entry] = self.audit(function_id="job.delete")
        self.assertEqual((entry["result"], entry["role"]), (audit_service.ALLOWED, rbac.BACKUP_ADMINISTRATOR))
        self.assertEqual(entry["target"]["name"], "nightly")

    def test_a_failed_operation_is_audited_as_failed(self) -> None:
        response = self.client.get("/api/v1/jobs/missing", headers=self.headers(rbac.AUDITOR))
        self.assertEqual(response.status_code, 404)
        [entry] = self.audit(function_id="job.view")
        self.assertEqual(entry["result"], audit_service.FAILED)
        self.assertEqual(entry["error"]["code"], response.json()["error"]["code"])

    def test_a_user_cannot_create_users_without_the_permission(self) -> None:
        response = self.client.post(
            "/api/v1/users",
            json={"name": "mallory", "password": "Mall0ryPass", "role": rbac.ADMINISTRATOR},
            headers=self.headers(rbac.AUDITOR),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any(user["name"] == "mallory" for user in USERS.values()))
        [entry] = self.audit(function_id="user.create")
        self.assertEqual(entry["result"], audit_service.DENIED)
        self.assertEqual(entry["parameters"]["body"]["password"], audit_service.MASK)

    def test_a_role_change_applies_to_the_next_request(self) -> None:
        olga = self.headers(rbac.BACKUP_OPERATOR)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=olga).status_code, 200)
        self.client.patch(
            f"/api/v1/users/{self.user_id('olga')}",
            json={"role": rbac.RESTORE_OPERATOR},
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        self.assertEqual(self.client.get("/api/v1/jobs", headers=olga).status_code, 403)
        self.assertEqual(self.audit(function_id="job.view")[-1]["role"], rbac.RESTORE_OPERATOR)

    def test_a_user_without_a_valid_role_can_do_nothing(self) -> None:
        USERS[self.user_id("olga")] = {**USERS[self.user_id("olga")], "role": "superuser"}
        response = self.client.get("/api/v1/dashboard/summary", headers=self.headers(rbac.BACKUP_OPERATOR))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["details"]["reason"], "no_role")
        login = self.client.post("/api/v1/auth/login", json={"name": "olga", "password": USER_PASSWORD})
        self.assertEqual((login.status_code, login.json()["error"]["code"]), (403, "role_invalid"))


class AuditLogTests(RbacApiCase):
    def test_user_and_role_changes_are_audited_with_what_changed(self) -> None:
        self.client.patch(
            f"/api/v1/users/{self.user_id('audrey')}",
            json={"role": rbac.BACKUP_OPERATOR, "enabled": False},
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        self.client.delete(f"/api/v1/users/{self.user_id('remy')}", headers=self.headers(rbac.ADMINISTRATOR))
        [edit] = self.audit(function_id="user.edit")
        self.assertEqual(edit["target"]["name"], "audrey")
        self.assertEqual(
            edit["details"]["changes"],
            {"role": {"from": rbac.AUDITOR, "to": rbac.BACKUP_OPERATOR}, "enabled": {"from": True, "to": False}},
        )
        [delete] = self.audit(function_id="user.delete")
        self.assertEqual((delete["result"], delete["target"]["name"]), (audit_service.ALLOWED, "remy"))

    def test_sign_ins_are_audited_whatever_their_outcome(self) -> None:
        self.client.post("/api/v1/auth/login", json={"name": "olga", "password": "wrong-password"})
        self.client.post("/api/v1/auth/login", json={"name": "olga", "password": USER_PASSWORD})
        failed, allowed = self.audit(function_id="auth.login")
        self.assertEqual((failed["result"], failed["user"], failed["role"]), ("failed", "olga", rbac.BACKUP_OPERATOR))
        self.assertEqual((allowed["result"], allowed["role"]), ("allowed", rbac.BACKUP_OPERATOR))
        self.assertRegex(allowed["auth_session_id"], r"^[0-9a-f]{16}$")

    def test_credentials_never_reach_the_audit_log(self) -> None:
        self.client.post(
            "/api/v1/repositories",
            json={
                "name": "offsite",
                "type": "s3",
                "bucket": "b",
                "access_key": "AKIAEXAMPLEKEY",
                "secret_key": "S3CR3T-VALUE",
                "endpoint": "http://user:Pa55word@minio.local:9000",
                "encryption_key": "K3Y-MATERIAL",
            },
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "N3wAdminPass"},
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        logged = json.dumps(list(AUDIT_RECORDS))
        for secret in (
            "AKIAEXAMPLEKEY", "S3CR3T-VALUE", "Pa55word", "K3Y-MATERIAL", ADMIN_PASSWORD, "N3wAdminPass",
            USER_PASSWORD, *self.tokens.values(),
        ):
            self.assertNotIn(secret, logged)
        [create] = self.audit(function_id="repository.create")
        self.assertEqual(create["parameters"]["body"]["secret_key"], audit_service.MASK)
        self.assertEqual(create["parameters"]["body"]["name"], "offsite")

    def test_a_background_refresh_is_not_logged_but_its_denial_is(self) -> None:
        refresh = {"X-ABR-Refresh": "1"}
        self.client.get("/api/v1/jobs", headers=self.headers(rbac.AUDITOR, **refresh))
        self.client.get("/api/v1/jobs", headers=self.headers(rbac.RESTORE_OPERATOR, **refresh))
        self.assertEqual([entry["role"] for entry in self.audit(function_id="job.view")], [rbac.RESTORE_OPERATOR])
        # The console asks what its role allows before every refresh.
        self.client.get("/api/v1/auth/me", headers=self.headers(rbac.AUDITOR, **refresh))
        self.assertEqual(self.audit(function_id="auth.whoami"), [])

    def test_the_web_keep_alive_is_not_logged_but_a_duration_change_is(self) -> None:
        for _ in range(3):
            self.assertEqual(
                self.client.post("/api/v1/auth/session/touch", headers=self.headers(rbac.AUDITOR)).status_code, 200
            )
        self.assertEqual(self.audit(function_id="auth.touch_session"), [])
        self.client.put(
            "/api/v1/auth/session/duration", json={"duration_seconds": 600}, headers=self.headers(rbac.AUDITOR)
        )
        self.assertEqual(len(self.audit(function_id="auth.extend_session")), 1)

    def test_records_have_one_format_across_interfaces(self) -> None:
        for client_type in ("web", "cli", None):
            extra = {"X-Client-Type": client_type} if client_type else {}
            if client_type == "cli":
                extra["X-ABR-Command"] = "job create"
            self.client.post("/api/v1/jobs", json={"name": "j"}, headers=self.headers(rbac.AUDITOR, **extra))
        web, cli, api = self.audit(function_id="job.create")
        self.assertEqual([web["interface"], cli["interface"], api["interface"]], ["web", "cli", "api"])
        self.assertEqual(cli["command"], "job create")
        volatile = {"id", "timestamp", "interface", "command"}
        shape = lambda entry: {key: value for key, value in entry.items() if key not in volatile}  # noqa: E731
        self.assertEqual(shape(web), shape(api))
        self.assertEqual(shape(cli), shape(api))

    def test_the_session_a_command_starts_is_recorded(self) -> None:
        JOBS["job-1"] = {"id": "job-1", "name": "nightly"}
        started = {"items": [{"id": "sess-1", "state": "queued"}, {"id": "sess-2", "state": "queued"}]}
        with patch("python.srv.api.jobs.abr_v41.start_job_record", return_value=started):
            response = self.client.post("/api/v1/jobs/job-1/start", json={}, headers=self.headers(rbac.BACKUP_OPERATOR))
        self.assertEqual(response.status_code, 200, response.text)
        [entry] = self.audit(function_id="job.start")
        self.assertEqual(entry["session_id"], "sess-1")
        self.assertEqual(entry["details"]["session_ids"], ["sess-1", "sess-2"])

    def test_the_audit_log_is_readable_by_role_and_filterable(self) -> None:
        self.client.post("/api/v1/jobs", json={"name": "j"}, headers=self.headers(rbac.RESTORE_OPERATOR))
        self.client.get("/api/v1/jobs", headers=self.headers(rbac.AUDITOR))
        denied = self.client.get("/api/v1/audit", params={"result": "denied"}, headers=self.headers(rbac.AUDITOR))
        self.assertEqual(denied.status_code, 200, denied.text)
        self.assertEqual([item["function_id"] for item in denied.json()["items"]], ["job.create"])
        by_area = self.client.get("/api/v1/audit", params={"function": "job."}, headers=self.headers(rbac.AUDITOR))
        self.assertEqual({item["function_id"] for item in by_area.json()["items"]}, {"job.create", "job.view"})
        by_user = self.client.get("/api/v1/audit", params={"user": "remy"}, headers=self.headers(rbac.AUDITOR))
        self.assertEqual({item["user"] for item in by_user.json()["items"]}, {"remy"})
        self.assertEqual(self.client.get("/api/v1/audit", headers=self.headers(rbac.BACKUP_OPERATOR)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
