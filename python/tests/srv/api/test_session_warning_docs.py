# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.api.schemas import SessionDTO
from python.srv.backup_server import app


class SessionWarningShapeTests(unittest.TestCase):
    def test_session_and_verify_warnings_describe_code_message_and_vm_name(self) -> None:
        schemas = app.openapi()["components"]["schemas"]

        for name in ("SessionDTO", "SessionVerifyDTO"):
            with self.subTest(name):
                self.assertIn("#/components/schemas/SessionWarningDTO", str(schemas[name]["properties"]["warnings"]))
        self.assertEqual(sorted(schemas["SessionWarningDTO"]["properties"]), ["code", "message", "vm_name"])

    def test_a_session_keeps_every_warning_as_it_was_stored(self) -> None:
        warnings = [
            {"code": "crash_consistent", "message": "m", "vm_name": "vm"},
            {"code": "damaged_chain", "message": "m", "restore_point_ids": ["p1"]},
            {"message": "an older warning without a code"},
        ]
        session = {"id": "s1", "type": "backup", "state": "completed", "warnings": warnings}

        dumped = SessionDTO.model_validate(session).model_dump(exclude_unset=True)

        self.assertEqual(dumped["warnings"], warnings)


if __name__ == "__main__":
    unittest.main()
