# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.storage_tool import CREDENTIAL_FIELD_NAMES, SECRET_MASK, mask_credential_fields


class MaskCredentialFieldsTests(unittest.TestCase):
    def test_masks_every_known_credential_field_name(self) -> None:
        for field in CREDENTIAL_FIELD_NAMES:
            data = {field: "real-secret-value"}
            mask_credential_fields(data)
            self.assertEqual(data[field], SECRET_MASK)

    def test_recurses_into_nested_dicts(self) -> None:
        data = {"metadata": {"credentials": {"username": "svc", "password": "hunter2"}}}
        mask_credential_fields(data)
        self.assertEqual(data["metadata"]["credentials"]["password"], SECRET_MASK)
        self.assertEqual(data["metadata"]["credentials"]["username"], "svc")

    def test_falsy_values_are_left_alone_not_turned_into_a_lie(self) -> None:
        data = {"password": None, "token": ""}
        mask_credential_fields(data)
        self.assertIsNone(data["password"])
        self.assertEqual(data["token"], "")

    def test_unrelated_keys_are_never_touched(self) -> None:
        data = {"access_key": "AKIA-real", "secret_key": "real-secret", "name": "probe"}
        mask_credential_fields(data)
        self.assertEqual(data["access_key"], "AKIA-real")
        self.assertEqual(data["secret_key"], "real-secret")
        self.assertEqual(data["name"], "probe")

    def test_mutates_in_place_and_returns_the_same_object(self) -> None:
        data = {"password": "hunter2"}
        result = mask_credential_fields(data)
        self.assertIs(result, data)


if __name__ == "__main__":
    unittest.main()
