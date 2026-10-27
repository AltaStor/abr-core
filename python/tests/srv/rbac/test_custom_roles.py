# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Custom roles through the API: making, cloning, changing and deleting them,
giving them to users, what they then allow, the rules that stop anyone using a
role to gain access, and the audit records of all of it."""

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import CUSTOM_ROLES, JOBS, USERS, restore_catalog
from python.srv.services import audit_service, rbac

from .rbac_case import USER_PASSWORD, RbacApiCase

READ_JOBS = ["job.view", "session.view"]
# Manages users and roles, and reads jobs, but holds nothing else.
MANAGER_FUNCTIONS = [
    "user.view", "user.create", "user.edit", "user.delete",
    "role.view", "role.create", "role.edit", "role.delete",
    "job.view",
]


class CustomRoleCase(RbacApiCase):
    def create_role(self, name: str, functions: list[str] | None = None, *, as_role: str = rbac.ADMINISTRATOR,
                    headers: dict | None = None, **extra) -> dict:
        body = {"name": name, **extra}
        if functions is not None:
            body["allowed_functions"] = functions
        response = self.client.post("/api/v1/roles", json=body, headers=headers or self.headers(as_role))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def patch_role(self, role_id: str, body: dict, *, headers: dict | None = None):
        return self.client.patch(
            f"/api/v1/roles/{role_id}", json=body, headers=headers or self.headers(rbac.ADMINISTRATOR)
        )

    def sign_in_as(self, name: str, role_id: str) -> dict:
        self.create_user(name, role_id)
        return {"Authorization": f"Bearer {self.login(name, USER_PASSWORD)}"}


class CustomRoleCrudTests(CustomRoleCase):
    def test_a_custom_role_allows_exactly_its_functions_and_the_self_service_ones(self) -> None:
        role = self.create_role("Night Operator", ["job.view", "job.start"], description="Runs jobs at night")

        self.assertTrue(role["id"].startswith(rbac.CUSTOM_ROLE_ID_PREFIX))
        self.assertEqual((role["name"], role["description"], role["built_in"]), ("Night Operator", "Runs jobs at night", False))
        self.assertEqual(set(role["allowed_functions"]), {"job.view", "job.start", *rbac.BASELINE_FUNCTIONS})
        disabled = {item["function_id"] for item in role["permissions"] if not item["allowed"]}
        self.assertEqual(disabled, set(rbac.FUNCTIONS_BY_ID) - set(role["allowed_functions"]))
        self.assertEqual(role["user_count"], 0)

    def test_roles_are_listed_built_in_first_and_found_by_id_or_name(self) -> None:
        role = self.create_role("Night Operator", READ_JOBS)

        listed = self.client.get("/api/v1/roles", headers=self.headers(rbac.AUDITOR)).json()["items"]
        self.assertEqual([item["id"] for item in listed], [*(r.id for r in rbac.ROLES), role["id"]])
        for key in (role["id"], "night operator", "Night-Operator"):
            with self.subTest(key=key):
                response = self.client.get(f"/api/v1/roles/{key}", headers=self.headers(rbac.AUDITOR))
                self.assertEqual((response.status_code, response.json()["id"]), (200, role["id"]))

    def test_a_custom_role_is_renamed_described_and_given_other_functions(self) -> None:
        role = self.create_role("Night Operator", READ_JOBS)

        response = self.patch_role(
            role["id"],
            {"name": "Night Shift", "description": "Nights", "allowed_functions": ["job.view", "job.start"]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["name"], response.json()["description"]), ("Night Shift", "Nights"))
        self.assertTrue(rbac.role_allows(role["id"], "job.start"))
        self.assertFalse(rbac.role_allows(role["id"], "session.view"))

    def test_an_unused_custom_role_is_deleted(self) -> None:
        role = self.create_role("Night Operator", READ_JOBS)

        response = self.client.delete(f"/api/v1/roles/{role['id']}", headers=self.headers(rbac.ADMINISTRATOR))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(role["id"], CUSTOM_ROLES)
        self.assertEqual(
            self.client.get(f"/api/v1/roles/{role['id']}", headers=self.headers(rbac.ADMINISTRATOR)).status_code, 404
        )

    def test_a_role_name_must_be_unique_among_every_role(self) -> None:
        self.create_role("Night Operator", READ_JOBS)
        for name in ("night operator", "NIGHT-OPERATOR", "Administrator", "backup operator", "auditor"):
            with self.subTest(name=name):
                response = self.client.post(
                    "/api/v1/roles", json={"name": name, "allowed_functions": READ_JOBS},
                    headers=self.headers(rbac.ADMINISTRATOR),
                )
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "role_exists"))
        self.assertEqual(len(CUSTOM_ROLES), 1)

    def test_a_role_needs_a_valid_name_and_known_functions(self) -> None:
        for body in (
            {"allowed_functions": READ_JOBS},
            {"name": "   ", "allowed_functions": READ_JOBS},
            {"name": "x" * 65, "allowed_functions": READ_JOBS},
            {"name": "a/b", "allowed_functions": READ_JOBS},
            {"name": "Reader"},
            {"name": "Reader", "allowed_functions": "job.view"},
            {"name": "Reader", "allowed_functions": ["job.view", "job.teleport"]},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/roles", json=body, headers=self.headers(rbac.ADMINISTRATOR))
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "invalid_role"))
        self.assertEqual(CUSTOM_ROLES, {})

    def test_the_self_service_functions_cannot_be_taken_away(self) -> None:
        role = self.create_role("Reader", ["job.view"])

        response = self.patch_role(role["id"], {"allowed_functions": []})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()["allowed_functions"]), set(rbac.BASELINE_FUNCTIONS))

    def test_functions_say_which_every_role_allows(self) -> None:
        items = self.client.get("/api/v1/functions", headers=self.headers(rbac.AUDITOR)).json()["items"]
        self.assertEqual({item["id"] for item in items if item["always_allowed"]}, set(rbac.BASELINE_FUNCTIONS))


class CloneTests(CustomRoleCase):
    def test_a_built_in_role_is_cloned_with_its_functions_and_description(self) -> None:
        clone = self.create_role("", clone_from=rbac.BACKUP_OPERATOR)

        source = rbac.ROLES_BY_ID[rbac.BACKUP_OPERATOR]
        self.assertEqual(clone["name"], "Backup Operator (copy)")
        self.assertEqual(clone["description"], source.description)
        self.assertEqual(clone["allowed_functions"], rbac.allowed_functions(rbac.BACKUP_OPERATOR))
        self.assertEqual(clone["cloned_from"], {"id": rbac.BACKUP_OPERATOR, "name": "Backup Operator"})
        self.assertFalse(clone["built_in"])
        # A second clone gets the next free name.
        self.assertEqual(self.create_role("", clone_from="Backup Operator")["name"], "Backup Operator (copy 2)")

    def test_a_custom_role_is_cloned_and_the_copy_changes_on_its_own(self) -> None:
        original = self.create_role("Night Operator", READ_JOBS)
        copy = self.create_role("Weekend Operator", clone_from=original["id"], description="Weekends")

        self.assertEqual(copy["allowed_functions"], original["allowed_functions"])
        self.assertEqual(copy["description"], "Weekends")
        self.patch_role(copy["id"], {"allowed_functions": ["job.view"]})
        self.assertTrue(rbac.role_allows(original["id"], "session.view"))
        self.assertFalse(rbac.role_allows(copy["id"], "session.view"))

    def test_a_clone_takes_given_functions_over_the_sources(self) -> None:
        clone = self.create_role("Reader", ["job.view"], clone_from=rbac.BACKUP_ADMINISTRATOR)
        self.assertEqual(set(clone["allowed_functions"]), {"job.view", *rbac.BASELINE_FUNCTIONS})

    def test_cloning_an_unknown_role_is_refused(self) -> None:
        response = self.client.post(
            "/api/v1/roles", json={"clone_from": "superuser"}, headers=self.headers(rbac.ADMINISTRATOR)
        )
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "role_not_found"))


class AssignmentTests(CustomRoleCase):
    def setUp(self) -> None:
        super().setUp()
        JOBS["job-1"] = {"id": "job-1", "name": "nightly"}
        self.role = self.create_role("Night Operator", READ_JOBS)

    def test_a_user_holds_a_custom_role_by_id_or_name_and_gets_what_it_allows(self) -> None:
        created = self.client.post(
            "/api/v1/users",
            json={"name": "nina", "password": USER_PASSWORD, "role": "night operator"},
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        self.assertEqual((created.status_code, created.json()["role"]), (200, self.role["id"]))

        login = self.client.post("/api/v1/auth/login", json={"name": "nina", "password": USER_PASSWORD}).json()
        self.assertEqual(set(login["permissions"]), set(self.role["allowed_functions"]))
        nina = {"Authorization": f"Bearer {login['token']}"}
        me = self.client.get("/api/v1/auth/me", headers=nina).json()
        self.assertEqual((me["role"]["id"], me["role"]["name"], me["role"]["built_in"]), (self.role["id"], "Night Operator", False))
        self.assertEqual(self.client.get("/api/v1/jobs", headers=nina).status_code, 200)
        denied = self.client.delete("/api/v1/jobs/job-1", headers=nina)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["details"]["role"], self.role["id"])
        self.assertIn("Night Operator role does not allow job.delete", denied.json()["error"]["message"])
        self.assertIn("job-1", JOBS)

    def test_a_changed_role_applies_to_its_users_next_request(self) -> None:
        nina = self.sign_in_as("nina", self.role["id"])
        self.assertEqual(self.client.post("/api/v1/jobs/job-1/start", json={}, headers=nina).status_code, 403)

        self.patch_role(self.role["id"], {"allowed_functions": ["job.start", "session.view"]})

        self.assertNotEqual(self.client.post("/api/v1/jobs/job-1/start", json={}, headers=nina).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=nina).status_code, 403)
        me = self.client.get("/api/v1/auth/me", headers=nina).json()
        self.assertIn("job.start", me["permissions"])
        self.assertNotIn("job.view", me["permissions"])

    def test_a_role_users_hold_cannot_be_deleted_until_they_get_another(self) -> None:
        self.create_user("nina", self.role["id"])

        refused = self.client.delete(f"/api/v1/roles/{self.role['id']}", headers=self.headers(rbac.ADMINISTRATOR))
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "role_in_use")
        self.assertEqual(refused.json()["error"]["details"]["users"], ["nina"])
        listed = self.client.get(f"/api/v1/roles/{self.role['id']}", headers=self.headers(rbac.ADMINISTRATOR))
        self.assertEqual(listed.json()["user_count"], 1)

        self.client.patch(
            f"/api/v1/users/{self.user_id('nina')}", json={"role": rbac.AUDITOR}, headers=self.headers(rbac.ADMINISTRATOR)
        )
        deleted = self.client.delete(f"/api/v1/roles/{self.role['id']}", headers=self.headers(rbac.ADMINISTRATOR))
        self.assertEqual(deleted.status_code, 200, deleted.text)


class PrivilegeEscalationTests(CustomRoleCase):
    """A user who may manage users and roles, but holds little else."""

    def setUp(self) -> None:
        super().setUp()
        self.manager_role = self.create_role("User Manager", MANAGER_FUNCTIONS)
        self.manager = self.sign_in_as("max", self.manager_role["id"])

    def assert_denied(self, response, reason: str) -> None:
        self.assertEqual(response.status_code, 403, response.text)
        error = response.json()["error"]
        self.assertEqual((error["code"], error["details"]["reason"]), ("permission_denied", reason))

    def test_a_role_cannot_allow_what_the_callers_role_does_not(self) -> None:
        response = self.client.post(
            "/api/v1/roles", json={"name": "Wrecker", "allowed_functions": ["job.view", "job.delete"]},
            headers=self.manager,
        )
        self.assert_denied(response, "exceeds_own_permissions")
        self.assertEqual(response.json()["error"]["details"]["functions"], ["job.delete"])
        self.assertFalse(any(role["name"] == "Wrecker" for role in CUSTOM_ROLES.values()))
        # Within the caller's own functions it is fine.
        self.create_role("Job Reader", ["job.view"], headers=self.manager)

    def test_a_more_powerful_role_cannot_be_cloned(self) -> None:
        response = self.client.post("/api/v1/roles", json={"clone_from": rbac.ADMINISTRATOR}, headers=self.manager)
        self.assert_denied(response, "exceeds_own_permissions")
        self.assertEqual(len(CUSTOM_ROLES), 1)

    def test_a_role_cannot_be_widened_beyond_the_caller(self) -> None:
        reader = self.create_role("Job Reader", ["job.view"], headers=self.manager)
        response = self.patch_role(reader["id"], {"allowed_functions": ["job.view", "job.start"]}, headers=self.manager)
        self.assert_denied(response, "exceeds_own_permissions")
        self.assertFalse(rbac.role_allows(reader["id"], "job.start"))

    def test_a_role_allowing_more_than_the_caller_cannot_be_changed_or_deleted(self) -> None:
        wider = self.create_role("Operator Plus", ["job.view", "job.start"])
        self.assert_denied(self.patch_role(wider["id"], {"name": "Renamed"}, headers=self.manager), "exceeds_own_permissions")
        self.assert_denied(
            self.client.delete(f"/api/v1/roles/{wider['id']}", headers=self.manager), "exceeds_own_permissions"
        )
        self.assertEqual(CUSTOM_ROLES[wider["id"]]["name"], "Operator Plus")

    def test_nobody_changes_or_deletes_their_own_role(self) -> None:
        for response in (
            self.patch_role(self.manager_role["id"], {"allowed_functions": MANAGER_FUNCTIONS[:-1]}, headers=self.manager),
            self.patch_role(self.manager_role["id"], {"name": "Boss"}, headers=self.manager),
            self.client.delete(f"/api/v1/roles/{self.manager_role['id']}", headers=self.manager),
        ):
            self.assert_denied(response, "own_role")
        self.assertEqual(CUSTOM_ROLES[self.manager_role["id"]]["name"], "User Manager")

    def test_nobody_changes_their_own_role(self) -> None:
        reader = self.create_role("Job Reader", ["job.view"], headers=self.manager)
        for headers, user, role in (
            (self.manager, "max", reader["id"]),
            (self.headers(rbac.ADMINISTRATOR), "admin", rbac.AUDITOR),
        ):
            with self.subTest(user=user):
                response = self.client.patch(
                    f"/api/v1/users/{self.user_id(user)}", json={"role": role}, headers=headers
                )
                self.assert_denied(response, "own_role")
        self.assertEqual(USERS[self.user_id("max")]["role"], self.manager_role["id"])
        self.assertEqual(USERS[self.user_id("admin")]["role"], rbac.ADMINISTRATOR)

    def test_a_role_allowing_more_cannot_be_assigned(self) -> None:
        created = self.client.post(
            "/api/v1/users",
            json={"name": "mallory", "password": USER_PASSWORD, "role": rbac.ADMINISTRATOR},
            headers=self.manager,
        )
        self.assert_denied(created, "exceeds_own_permissions")
        self.assertFalse(any(user["name"] == "mallory" for user in USERS.values()))

        self.create_user("nina", self.create_role("Job Reader", ["job.view"])["id"])
        promoted = self.client.patch(
            f"/api/v1/users/{self.user_id('nina')}", json={"role": rbac.BACKUP_ADMINISTRATOR}, headers=self.manager
        )
        self.assert_denied(promoted, "exceeds_own_permissions")
        self.assertNotEqual(USERS[self.user_id("nina")]["role"], rbac.BACKUP_ADMINISTRATOR)

    def test_a_more_powerful_user_cannot_be_taken_over(self) -> None:
        for response in (
            self.client.patch(f"/api/v1/users/{self.user_id('admin')}", json={"password": "Takeover-Pass1"}, headers=self.manager),
            self.client.patch(f"/api/v1/users/{self.user_id('bart')}", json={"enabled": False}, headers=self.manager),
            self.client.delete(f"/api/v1/users/{self.user_id('admin')}", headers=self.manager),
        ):
            self.assert_denied(response, "exceeds_own_permissions")
        self.assertTrue(USERS[self.user_id("bart")]["enabled"])
        self.assertIn(self.user_id("admin"), USERS)

    def test_users_within_the_callers_reach_are_managed(self) -> None:
        reader = self.create_role("Job Reader", ["job.view"], headers=self.manager)
        created = self.client.post(
            "/api/v1/users", json={"name": "nina", "password": USER_PASSWORD, "role": reader["id"]}, headers=self.manager
        )
        self.assertEqual(created.status_code, 200, created.text)
        edited = self.client.patch(
            f"/api/v1/users/{self.user_id('nina')}", json={"password": "N3w-Passw0rd!"}, headers=self.manager
        )
        self.assertEqual(edited.status_code, 200, edited.text)

    def test_roles_without_role_administration_cannot_manage_roles(self) -> None:
        reader = self.create_role("Job Reader", ["job.view"])
        for role in (rbac.AUDITOR, rbac.BACKUP_ADMINISTRATOR, rbac.BACKUP_OPERATOR):
            with self.subTest(role=role):
                for response in (
                    self.client.post("/api/v1/roles", json={"name": "Mine", "allowed_functions": []}, headers=self.headers(role)),
                    self.client.patch(f"/api/v1/roles/{reader['id']}", json={"name": "Mine"}, headers=self.headers(role)),
                    self.client.delete(f"/api/v1/roles/{reader['id']}", headers=self.headers(role)),
                ):
                    self.assert_denied(response, "not_permitted")
        self.assertEqual(CUSTOM_ROLES[reader["id"]]["name"], "Job Reader")


class LastAdministratorThroughCustomRolesTests(CustomRoleCase):
    def test_a_full_copy_of_the_administrator_does_not_count_as_one(self) -> None:
        deputy_role = self.create_role("Deputy", clone_from=rbac.ADMINISTRATOR)
        deputy = self.sign_in_as("deputy", deputy_role["id"])
        admin = self.user_id("admin")

        for body in ({"role": deputy_role["id"]}, {"enabled": False}):
            with self.subTest(body=body):
                response = self.client.patch(f"/api/v1/users/{admin}", json=body, headers=deputy)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "last_administrator"))
        response = self.client.delete(f"/api/v1/users/{admin}", headers=deputy)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "last_administrator"))
        self.assertEqual((USERS[admin]["role"], USERS[admin]["enabled"]), (rbac.ADMINISTRATOR, True))

    def test_no_custom_role_takes_a_built_in_id(self) -> None:
        # A catalog record claiming a built-in ID is never read as a custom role.
        CUSTOM_ROLES[rbac.AUDITOR] = {"id": rbac.AUDITOR, "name": "Sneaky", "allowed_functions": ["user.create"]}
        self.assertFalse(rbac.role_allows(rbac.AUDITOR, "user.create"))
        self.assertEqual(rbac.get_role(rbac.AUDITOR).name, "Auditor")
        CUSTOM_ROLES.pop(rbac.AUDITOR)

    def test_an_import_brings_custom_roles_but_never_a_built_in_one(self) -> None:
        restore_catalog({
            "roles": {
                "custom-0000beef": {"id": "custom-0000beef", "name": "Imported", "allowed_functions": ["job.view"]},
                rbac.AUDITOR: {"id": rbac.AUDITOR, "name": "Auditor", "allowed_functions": ["user.create"]},
            },
        })
        self.assertTrue(rbac.role_allows("custom-0000beef", "job.view"))
        self.assertNotIn(rbac.AUDITOR, CUSTOM_ROLES)
        self.assertFalse(rbac.role_allows(rbac.AUDITOR, "user.create"))

    def test_an_import_replaces_a_same_named_role_and_moves_its_users(self) -> None:
        current = self.create_role("Night Operator", READ_JOBS)
        self.create_user("nina", current["id"])

        restore_catalog({"roles": {"custom-0000beef": {"id": "custom-0000beef", "name": "night operator", "allowed_functions": ["job.view"]}}})

        self.assertNotIn(current["id"], CUSTOM_ROLES)
        self.assertEqual(USERS[self.user_id("nina")]["role"], "custom-0000beef")


class CustomRoleAuditTests(CustomRoleCase):
    def test_creating_cloning_changing_and_deleting_roles_is_audited(self) -> None:
        role = self.create_role("Night Operator", READ_JOBS)
        clone = self.create_role("Night Copy", clone_from=role["id"])
        self.patch_role(role["id"], {"name": "Night Shift", "allowed_functions": ["job.view", "job.start"]})
        self.client.delete(f"/api/v1/roles/{clone['id']}", headers=self.headers(rbac.ADMINISTRATOR))

        created, cloned = self.audit(function_id="role.create")
        self.assertEqual((created["result"], created["target"]), (
            audit_service.ALLOWED, {"type": "role", "id": role["id"], "name": "Night Operator"},
        ))
        self.assertEqual(set(created["details"]["allowed_functions"]), set(role["allowed_functions"]))
        self.assertEqual(cloned["details"]["cloned_from"], {"id": role["id"], "name": "Night Operator"})
        self.assertEqual(cloned["target"]["name"], "Night Copy")

        [edited] = self.audit(function_id="role.edit")
        self.assertEqual(edited["target"]["id"], role["id"])
        self.assertEqual(edited["details"]["changes"]["name"], {"from": "Night Operator", "to": "Night Shift"})
        self.assertEqual(
            edited["details"]["changes"]["permissions"], {"granted": ["job.start"], "revoked": ["session.view"]}
        )

        [deleted] = self.audit(function_id="role.delete")
        self.assertEqual((deleted["result"], deleted["target"]["name"]), (audit_service.ALLOWED, "Night Copy"))

    def test_assigning_a_custom_role_is_audited_with_the_role_names(self) -> None:
        role = self.create_role("Night Operator", READ_JOBS)
        self.create_user("nina", rbac.AUDITOR)
        self.client.patch(
            f"/api/v1/users/{self.user_id('nina')}", json={"role": role["id"]}, headers=self.headers(rbac.ADMINISTRATOR)
        )

        [created] = self.audit(function_id="user.create", result="allowed")
        self.assertEqual(created["details"]["role"]["id"], rbac.AUDITOR)
        [edit] = self.audit(function_id="user.edit")
        self.assertEqual(edit["details"]["changes"]["role"], {"from": rbac.AUDITOR, "to": role["id"]})
        self.assertEqual(edit["details"]["roles"]["to"], {"id": role["id"], "name": "Night Operator", "built_in": False})
        self.assertEqual(edit["details"]["roles"]["from"]["name"], "Auditor")

    def test_refused_role_changes_are_audited_as_denied_or_failed(self) -> None:
        self.client.patch("/api/v1/roles/auditor", json={"name": "x"}, headers=self.headers(rbac.ADMINISTRATOR))
        self.client.post("/api/v1/roles", json={"name": "Mine", "allowed_functions": []}, headers=self.headers(rbac.AUDITOR))
        manager = self.sign_in_as("max", self.create_role("User Manager", MANAGER_FUNCTIONS)["id"])
        self.client.post("/api/v1/roles", json={"clone_from": rbac.ADMINISTRATOR}, headers=manager)

        [built_in] = self.audit(function_id="role.edit")
        self.assertEqual((built_in["result"], built_in["error"]["code"]), (audit_service.FAILED, "built_in_role"))
        denied = self.audit(function_id="role.create", result=audit_service.DENIED)
        self.assertEqual([(entry["user"], entry["role"]) for entry in denied], [
            ("audrey", rbac.AUDITOR), ("max", USERS[self.user_id("max")]["role"]),
        ])


if __name__ == "__main__":
    unittest.main()
