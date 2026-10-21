# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import dataclasses
import unittest

from python.srv.services import rbac
from python.srv.services.api_errors import ApiError

# The RBAC permission matrix, written out: for each function, the roles that
# are allowed it. A Administrator, B Backup Administrator, O Backup Operator,
# R Restore Operator, U Auditor. Every other pair is disabled.
EXPECTED_MATRIX = {
    "auth.login": "ABORU",
    "auth.logout": "ABORU",
    "auth.whoami": "ABORU",
    "auth.change_password": "ABORU",
    "auth.extend_session": "ABORU",
    "dashboard.view": "ABORU",
    "system.view": "ABORU",
    "system.logs": "ABU",
    "system.support_bundle": "A",
    "system.export": "A",
    "system.import": "A",
    "system.clock_confirm": "A",
    "tls.view": "AU",
    "tls.edit": "A",
    "user.view": "AU",
    "user.create": "A",
    "user.edit": "A",
    "user.delete": "A",
    "role.view": "AU",
    "audit.view": "AU",
    "repository.view": "ABORU",
    "repository.create": "AB",
    "repository.edit": "AB",
    "repository.delete": "AB",
    "repository.rescan": "AB",
    "repository.verify": "AB",
    "repository.apply_immutability": "AB",
    "inventory.view": "ABORU",
    "inventory.create": "AB",
    "inventory.edit": "AB",
    "inventory.delete": "AB",
    "inventory.rescan": "ABR",
    "inventory.test": "ABR",
    "job.view": "ABOU",
    "job.create": "AB",
    "job.edit": "AB",
    "job.delete": "AB",
    "job.start": "ABO",
    "job.retention": "AB",
    "session.view": "ABORU",
    "session.stop": "ABOR",
    "backup.view": "ABORU",
    "backup.verify": "ABOR",
    "backup.delete": "AB",
    "backup.import": "AB",
    "restore.view": "AR",
    "restore.start": "AR",
}
ROLE_LETTERS = {
    "A": rbac.ADMINISTRATOR,
    "B": rbac.BACKUP_ADMINISTRATOR,
    "O": rbac.BACKUP_OPERATOR,
    "R": rbac.RESTORE_OPERATOR,
    "U": rbac.AUDITOR,
}


def _user(role: str | None, *, enabled: bool = True) -> dict:
    return {"id": "u1", "name": "someone", "role": role, "enabled": enabled}


class FunctionRegistryTests(unittest.TestCase):
    def test_function_ids_are_unique_and_well_formed(self) -> None:
        ids = [function.id for function in rbac.FUNCTIONS]
        self.assertEqual(len(ids), len(set(ids)))
        for function_id in ids:
            self.assertRegex(function_id, rbac.FUNCTION_ID_PATTERN)

    def test_every_function_has_a_known_category_and_a_name(self) -> None:
        for function in rbac.FUNCTIONS:
            self.assertIn(function.category, rbac.CATEGORIES, function.id)
            self.assertTrue(function.name, function.id)

    def test_the_matrix_decides_every_function(self) -> None:
        self.assertEqual(set(EXPECTED_MATRIX), set(rbac.FUNCTIONS_BY_ID))


class BuiltInRoleTests(unittest.TestCase):
    def test_the_five_roles_exist(self) -> None:
        self.assertEqual(
            {role.id: role.name for role in rbac.ROLES},
            {
                "administrator": "Administrator",
                "backup_administrator": "Backup Administrator",
                "backup_operator": "Backup Operator",
                "restore_operator": "Restore Operator",
                "auditor": "Auditor",
            },
        )

    def test_each_role_has_exactly_the_matrix_permissions(self) -> None:
        for function_id, letters in EXPECTED_MATRIX.items():
            for letter, role_id in ROLE_LETTERS.items():
                with self.subTest(function=function_id, role=role_id):
                    self.assertEqual(rbac.role_allows(role_id, function_id), letter in letters)
                    self.assertEqual(rbac.check(_user(role_id), function_id).allowed, letter in letters)

    def test_every_role_function_pair_is_explicit(self) -> None:
        self.assertEqual(len(rbac.PERMISSIONS), len(rbac.ROLES) * len(rbac.FUNCTIONS))
        for (role_id, function_id), permission in rbac.PERMISSIONS.items():
            self.assertEqual((permission.role_id, permission.function_id), (role_id, function_id))

    def test_the_administrator_is_allowed_every_function(self) -> None:
        self.assertEqual(rbac.allowed_functions(rbac.ADMINISTRATOR), [function.id for function in rbac.FUNCTIONS])

    def test_the_auditor_is_allowed_only_reads(self) -> None:
        for function_id in rbac.allowed_functions(rbac.AUDITOR):
            self.assertIn(rbac.FUNCTIONS_BY_ID[function_id].category, (rbac.READ, rbac.SELF_SERVICE), function_id)

    def test_roles_and_permissions_cannot_be_changed(self) -> None:
        role = rbac.ROLES_BY_ID[rbac.AUDITOR]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            role.name = "Superuser"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            rbac.ROLES_BY_ID["superuser"] = role  # type: ignore[index]
        with self.assertRaises(TypeError):
            rbac.PERMISSIONS[(rbac.AUDITOR, "job.delete")] = rbac.Permission(rbac.AUDITOR, "job.delete", True)  # type: ignore[index]
        self.assertFalse(rbac.role_allows(rbac.AUDITOR, "job.delete"))


class AuthorizationCheckTests(unittest.TestCase):
    def test_a_missing_permission_is_disabled(self) -> None:
        self.assertFalse(rbac.role_allows(rbac.AUDITOR, "job.something_new"))
        self.assertFalse(rbac.role_allows("nobody", "job.view"))

    def test_an_unknown_function_is_denied_even_to_the_administrator(self) -> None:
        decision = rbac.check(_user(rbac.ADMINISTRATOR), "job.unheard_of")
        self.assertEqual((decision.allowed, decision.reason), (False, "unknown_function"))

    def test_a_user_without_a_valid_role_is_denied(self) -> None:
        for role in (None, "", "superuser", "Administrator "):
            with self.subTest(role=role):
                decision = rbac.check(_user(role), "dashboard.view")
                self.assertEqual((decision.allowed, decision.reason), (False, "no_role"))

    def test_a_disabled_account_is_denied_whatever_its_role(self) -> None:
        decision = rbac.check(_user(rbac.ADMINISTRATOR, enabled=False), "job.view")
        self.assertEqual((decision.allowed, decision.reason), (False, "account_disabled"))

    def test_no_user_is_denied(self) -> None:
        self.assertFalse(rbac.is_allowed(None, "dashboard.view"))

    def test_require_raises_the_permission_denied_error(self) -> None:
        with self.assertRaises(ApiError) as caught:
            rbac.require(_user(rbac.BACKUP_OPERATOR), "job.delete")
        error = caught.exception
        self.assertEqual((error.status_code, error.code), (403, "permission_denied"))
        self.assertEqual(error.details["function_id"], "job.delete")
        self.assertEqual(error.details["role"], rbac.BACKUP_OPERATOR)
        self.assertIn("Backup Operator", error.message)

    def test_require_answers_the_decision_when_allowed(self) -> None:
        self.assertTrue(rbac.require(_user(rbac.BACKUP_OPERATOR), "job.start").allowed)

    def test_a_role_resolves_from_its_id_or_name(self) -> None:
        for value in ("backup_operator", "Backup Operator", "backup-operator", "BACKUP_OPERATOR"):
            self.assertEqual(rbac.resolve_role(value).id, rbac.BACKUP_OPERATOR, value)
        self.assertIsNone(rbac.resolve_role("operator"))

    def test_a_user_from_before_roles_becomes_an_enabled_administrator(self) -> None:
        legacy = {"id": "u1", "name": "old"}
        self.assertTrue(rbac.with_default_role(legacy))
        self.assertEqual((legacy["role"], legacy["enabled"]), (rbac.ADMINISTRATOR, True))
        self.assertFalse(rbac.with_default_role(legacy))
        self.assertFalse(rbac.with_default_role({"role": rbac.AUDITOR, "enabled": False}))


if __name__ == "__main__":
    unittest.main()
