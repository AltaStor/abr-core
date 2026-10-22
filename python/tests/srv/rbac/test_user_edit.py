# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import USERS
from python.srv.services import rbac

from .rbac_case import ADMIN_PASSWORD, USER_PASSWORD, RbacApiCase

NEW_PASSWORD = "N3w-Passw0rd!"


class AdministratorEditsUsersTests(RbacApiCase):
    def _patch(self, name: str, body: dict, *, as_role: str = rbac.ADMINISTRATOR):
        return self.client.patch(f"/api/v1/users/{self.user_id(name)}", json=body, headers=self.headers(as_role))

    def _login_status(self, name: str, password: str) -> int:
        return self.client.post("/api/v1/auth/login", json={"name": name, "password": password}).status_code

    def test_a_renamed_user_signs_in_with_the_new_name_and_the_same_password(self) -> None:
        response = self._patch("olga", {"name": "olga.k"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "olga.k")
        self.assertEqual(self._login_status("olga.k", USER_PASSWORD), 200)
        self.assertEqual(self._login_status("olga", USER_PASSWORD), 401)
        # The session opened before the rename goes on working.
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers(rbac.BACKUP_OPERATOR)).status_code, 200)

    def test_a_renamed_user_can_still_change_their_own_password(self) -> None:
        self._patch("olga", {"name": "olga.k"})
        token = self.client.post("/api/v1/auth/login", json={"name": "olga.k", "password": USER_PASSWORD}).json()["token"]

        response = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": USER_PASSWORD, "new_password": NEW_PASSWORD},
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("passwd_name", USERS[self.user_id("olga.k")])
        self.assertEqual(self._login_status("olga.k", NEW_PASSWORD), 200)

    def test_a_name_another_user_has_is_refused(self) -> None:
        response = self._patch("olga", {"name": "Remy"})

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "user_exists")

    def test_a_new_password_replaces_the_old_one_and_ends_the_users_sessions(self) -> None:
        response = self._patch("olga", {"password": NEW_PASSWORD})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers(rbac.BACKUP_OPERATOR)).status_code, 401)
        self.assertEqual(self._login_status("olga", USER_PASSWORD), 401)
        self.assertEqual(self._login_status("olga", NEW_PASSWORD), 200)
        record = self.audit(function_id="user.edit", result="allowed")[-1]
        self.assertEqual(record["details"]["changes"]["password"], "********")
        self.assertNotIn(NEW_PASSWORD, str(record))

    def test_a_rename_and_a_new_password_together(self) -> None:
        self.assertEqual(self._patch("olga", {"name": "olga.k", "password": NEW_PASSWORD}).status_code, 200)

        self.assertEqual(self._login_status("olga.k", NEW_PASSWORD), 200)

    def test_a_new_password_must_meet_the_policy(self) -> None:
        response = self._patch("olga", {"password": "short"})

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._login_status("olga", USER_PASSWORD), 200)

    def test_an_administrators_own_password_needs_the_current_one(self) -> None:
        response = self._patch("admin", {"password": NEW_PASSWORD})

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "own_password")
        self.assertEqual(self._login_status("admin", ADMIN_PASSWORD), 200)

    def test_only_a_role_that_may_edit_users_renames_or_resets_them(self) -> None:
        for body in ({"name": "olga.k"}, {"password": NEW_PASSWORD}):
            with self.subTest(body=body):
                self.assertEqual(self._patch("olga", body, as_role=rbac.BACKUP_ADMINISTRATOR).status_code, 403)
        self.assertEqual(self._login_status("olga", USER_PASSWORD), 200)


if __name__ == "__main__":
    unittest.main()
