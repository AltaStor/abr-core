# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""A running API (in memory, no catalog database) with one signed-in user per
built-in role, for the RBAC and audit tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import AUDIT_RECORDS, JOBS, SESSIONS, USERS
from python.srv.services import auth_service, rbac

ADMIN_PASSWORD = "Sup3rSecretPW"
USER_PASSWORD = "RolePassw0rd!"
BOOTSTRAP_ENV = {"ABR_BOOTSTRAP_ADMIN_USER": "admin", "ABR_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD}
ROLE_USERS = {
    rbac.ADMINISTRATOR: "admin",
    rbac.BACKUP_ADMINISTRATOR: "bart",
    rbac.BACKUP_OPERATOR: "olga",
    rbac.RESTORE_OPERATOR: "remy",
    rbac.AUDITOR: "audrey",
}


def reset_catalog() -> None:
    USERS.clear()
    SESSIONS.clear()
    JOBS.clear()
    AUDIT_RECORDS.clear()
    auth_service.reset_login_lockout_state_for_testing()


class RbacApiCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_catalog()
        env = patch.dict("os.environ", BOOTSTRAP_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self.addCleanup(reset_catalog)
        self.addCleanup(self.client_cm.__exit__, None, None, None)
        self.tokens = {rbac.ADMINISTRATOR: self.login("admin", ADMIN_PASSWORD)}
        for role, name in ROLE_USERS.items():
            if role != rbac.ADMINISTRATOR:
                self.create_user(name, role)
                self.tokens[role] = self.login(name, USER_PASSWORD)
        AUDIT_RECORDS.clear()

    def login(self, name: str, password: str) -> str:
        response = self.client.post("/api/v1/auth/login", json={"name": name, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def headers(self, role: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}", **extra}

    def create_user(self, name: str, role: str, *, as_role: str = rbac.ADMINISTRATOR) -> dict:
        response = self.client.post(
            "/api/v1/users",
            json={"name": name, "password": USER_PASSWORD, "role": role},
            headers=self.headers(as_role),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def user_id(self, name: str) -> str:
        return next(user["id"] for user in USERS.values() if user["name"] == name)

    def audit(self, **match) -> list[dict]:
        """Audit records, oldest first, whose fields equal ``match``."""
        return [record for record in AUDIT_RECORDS if all(record.get(key) == value for key, value in match.items())]
