# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from python.srv.services import audit_service, notification_detectors
from python.srv.services.audit_service import AuditContext
from python.srv.services.notification_events import EVENTS_BY_ID

NEW_EVENTS = ("backup.deleted", "repository.removed", "user.created", "user.role_changed", "repository.immutability_changed")


class _ApiError(Exception):
    code = "repository_has_backups"


class PrivilegedActionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[tuple] = []
        for target, name, effect in (
            (notification_detectors, "notify", lambda event, subject, body, **kwargs: self.sent.append((event, subject, body, kwargs))),
            (audit_service, "append_audit_record", lambda entry: None),
        ):
            patcher = patch.object(target, name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _request(self, function_id: str, *, target: dict | None = None, body: dict | None = None, query: dict | None = None,
                 status: int = 200, exc: BaseException | None = None) -> dict | None:
        context = AuditContext(function_id, user="alice", user_id="u-1", role="administrator", target=target)
        with self.assertLogs(audit_service.LOGGER, "INFO"):
            return audit_service.record_request(
                function_id=function_id,
                context=context,
                headers={"x-client-type": "cli"},
                client="10.0.0.5",
                query=query or {},
                body=json.dumps(body).encode() if body is not None else b"",
                status=status,
                response_body=b"{}",
                exc=exc,
            )

    def test_the_new_events_exist_and_are_off_by_default(self) -> None:
        for event in NEW_EVENTS:
            with self.subTest(event):
                self.assertIn(event, EVENTS_BY_ID)
                self.assertFalse(EVENTS_BY_ID[event].default)

    def test_a_deleted_backup_chain_names_the_user_the_chain_and_the_time(self) -> None:
        entry = self._request("backup.delete", target={"type": "backup", "id": "backup::j::vm::r", "name": "sql-01"}, query={"force": "true"})

        [(event, subject, body, kwargs)] = self.sent
        self.assertEqual(event, "backup.deleted")
        self.assertEqual(subject, "Backup sql-01 deleted by alice")
        self.assertIn("User: alice (administrator), through the cli from 10.0.0.5", body)
        self.assertIn("Backup chain: backup::j::vm::r", body)
        self.assertIn("GFS-protected restore points included", body)
        self.assertEqual((kwargs["details"]["user"], kwargs["details"]["at"]), ("alice", entry["timestamp"]))

    def test_a_removed_repository_and_a_created_user_are_reported(self) -> None:
        self._request("repository.delete", target={"type": "repository", "id": "r-1", "name": "Offsite"})
        self._request("user.create", target={"type": "user", "id": "u-2"}, body={"name": "bob", "role": "auditor", "password": "x"})

        self.assertEqual([item[0] for item in self.sent], ["repository.removed", "user.created"])
        self.assertIn('Repository "Offsite" removed by alice', self.sent[0][1])
        self.assertIn("Role: auditor", self.sent[1][2])
        self.assertNotIn("password", json.dumps(self.sent[1][3]["details"]))

    def test_a_role_change_is_reported_and_other_user_edits_are_not(self) -> None:
        self._request("user.edit", target={"type": "user", "id": "u-2", "name": "bob"}, body={"role": "backup_operator"})
        self._request("user.edit", target={"type": "user", "id": "u-2", "name": "bob"}, body={"full_name": "Bob B."})

        [(event, _subject, body, kwargs)] = self.sent
        self.assertEqual(event, "user.role_changed")
        self.assertIn("New role: backup_operator", body)
        self.assertEqual(kwargs["details"]["new_role"], "backup_operator")

    def test_switching_immutability_off_is_reported_and_other_repository_edits_are_not(self) -> None:
        target = {"type": "repository", "id": "r-1", "name": "WORM"}
        self._request("repository.edit", target=target, body={"immutability": {"mode": "none", "retention_days": 0}})
        self._request("repository.edit", target=target, body={"description": "renamed"})
        self._request("repository.edit", target=target, body={"immutability": {"mode": "governance", "retention_days": 30}})

        self.assertEqual([item[0] for item in self.sent], ["repository.immutability_changed"] * 2)
        self.assertIn("New setting: off (no Object Lock)", self.sent[0][2])
        self.assertIn("New setting: governance for 30 day(s)", self.sent[1][2])

    def test_an_s3_repository_edit_as_the_cli_sends_it_is_reported_too(self) -> None:
        self._request("repository.edit", target={"type": "repository", "id": "r-1", "name": "WORM"}, body={"s3": {"immutability": {"mode": "none"}}})

        [(event, _subject, body, kwargs)] = self.sent
        self.assertEqual(event, "repository.immutability_changed")
        self.assertIn("New setting: off (no Object Lock)", body)
        self.assertEqual(kwargs["details"]["immutability"], {"mode": "none"})

    def test_a_refused_or_failed_action_is_not_reported(self) -> None:
        self._request("repository.delete", target={"type": "repository", "id": "r-1", "name": "Offsite"}, status=409, exc=_ApiError())

        self.assertEqual(self.sent, [])

    def test_a_notification_that_breaks_never_breaks_the_request(self) -> None:
        with patch.object(notification_detectors, "privileged_action", side_effect=RuntimeError("boom")), self.assertLogs(audit_service.LOGGER, "ERROR"):
            entry = audit_service.record_request(
                function_id="repository.delete",
                context=AuditContext("repository.delete", user="alice", role="administrator", target={"type": "repository", "id": "r-1"}),
                headers={},
                client=None,
                query={},
                body=b"",
                status=200,
                response_body=b"{}",
                exc=None,
            )

        self.assertEqual(entry["result"], "allowed")


if __name__ == "__main__":
    unittest.main()
