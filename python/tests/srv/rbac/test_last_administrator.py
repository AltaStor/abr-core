# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import USERS
from python.srv.services import auth_service, rbac
from python.srv.services.api_errors import ApiError

from .rbac_case import ADMIN_PASSWORD, RbacApiCase


class LastAdministratorTests(RbacApiCase):
    def _patch_admin(self, body: dict, *, user: str = "admin"):
        return self.client.patch(
            f"/api/v1/users/{self.user_id(user)}", json=body, headers=self.headers(rbac.ADMINISTRATOR)
        )

    def assert_refused(self, response) -> None:
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "last_administrator")
        admin = USERS[self.user_id("admin")]
        self.assertEqual((admin["role"], admin["enabled"]), (rbac.ADMINISTRATOR, True))

    def test_the_last_administrator_cannot_be_demoted(self) -> None:
        for role in (rbac.BACKUP_ADMINISTRATOR, rbac.AUDITOR):
            with self.subTest(role=role):
                self.assert_refused(self._patch_admin({"role": role}))

    def test_the_last_administrator_cannot_be_disabled(self) -> None:
        self.assert_refused(self._patch_admin({"enabled": False}))

    def test_the_last_administrator_cannot_be_deleted(self) -> None:
        response = self.client.delete(
            f"/api/v1/users/{self.user_id('admin')}", headers=self.headers(rbac.ADMINISTRATOR)
        )
        self.assert_refused(response)

    def test_a_disabled_administrator_does_not_count(self) -> None:
        self.create_user("second", rbac.ADMINISTRATOR)
        self.assertEqual(self._patch_admin({"enabled": False}, user="second").status_code, 200)
        self.assert_refused(self._patch_admin({"role": rbac.AUDITOR}))

    def test_an_administrator_can_be_demoted_disabled_or_deleted_while_another_remains(self) -> None:
        self.create_user("second", rbac.ADMINISTRATOR)
        self.assertEqual(self._patch_admin({"role": rbac.AUDITOR}).status_code, 200)
        self.assertEqual(USERS[self.user_id("admin")]["role"], rbac.AUDITOR)
        # The demotion holds at once: the former Administrator's session can
        # no longer manage users.
        response = self.client.delete(
            f"/api/v1/users/{self.user_id('second')}", headers=self.headers(rbac.ADMINISTRATOR)
        )
        self.assertEqual(response.status_code, 403)

    def test_the_last_administrator_may_still_change_other_fields(self) -> None:
        response = self._patch_admin({"fullName": "Root", "role": rbac.ADMINISTRATOR, "enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["fullName"], "Root")

    def test_other_roles_can_be_removed_freely(self) -> None:
        response = self.client.delete(
            f"/api/v1/users/{self.user_id('audrey')}", headers=self.headers(rbac.ADMINISTRATOR)
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_disabling_a_user_ends_their_sessions_and_sign_in(self) -> None:
        self.create_user("second", rbac.ADMINISTRATOR)
        second = self.login("second", "RolePassw0rd!")
        self.assertEqual(self._patch_admin({"enabled": False}, user="second").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/jobs", headers={"Authorization": f"Bearer {second}"}).status_code, 401)
        login = self.client.post("/api/v1/auth/login", json={"name": "second", "password": "RolePassw0rd!"})
        self.assertEqual((login.status_code, login.json()["error"]["code"]), (403, "account_disabled"))

    def test_a_catalog_import_cannot_leave_no_administrator(self) -> None:
        admin = dict(USERS[self.user_id("admin")])
        demoted = {**admin, "role": rbac.AUDITOR}
        with self.assertRaises(ApiError) as caught:
            auth_service.ensure_import_keeps_an_administrator({"users": {admin["id"]: demoted}}, [])
        self.assertEqual(caught.exception.code, "last_administrator")
        # A user from before roles existed imports as an Administrator.
        legacy = {key: value for key, value in admin.items() if key not in ("role", "enabled")}
        auth_service.ensure_import_keeps_an_administrator({"users": {admin["id"]: legacy}}, [])

    def test_the_bootstrap_administrator_holds_the_administrator_role(self) -> None:
        response = self.client.post("/api/v1/auth/login", json={"name": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(response.json()["user"]["role"], rbac.ADMINISTRATOR)
        self.assertEqual(response.json()["permissions"], rbac.allowed_functions(rbac.ADMINISTRATOR))


class RoleImmutabilityApiTests(RbacApiCase):
    def test_no_interface_changes_or_deletes_a_role(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                response = self.client.request(
                    method, "/api/v1/roles/auditor", json={"name": "x"}, headers=self.headers(rbac.ADMINISTRATOR)
                )
                self.assertEqual(response.status_code, 405)

    def test_a_user_needs_a_known_role(self) -> None:
        for body, code in (({}, "invalid_user"), ({"role": "superuser"}, "invalid_role")):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/v1/users",
                    json={"name": "nobody", "password": "Passw0rd!x", **body},
                    headers=self.headers(rbac.ADMINISTRATOR),
                )
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, code))
        self.assertFalse(any(user["name"] == "nobody" for user in USERS.values()))


if __name__ == "__main__":
    unittest.main()
