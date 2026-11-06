# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.storage_tool import (
    CREDENTIAL_FIELD_NAMES,
    SECRET_MASK,
    ensure_private_directory,
    mask_credential_fields,
    remove_empty_parents,
)


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


class RepositoryFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_folders_go_up_to_the_repository_root_which_stays(self) -> None:
        (self.root / "job" / "vm").mkdir(parents=True)

        remove_empty_parents(self.root / "job" / "vm", below=self.root)

        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.root.is_dir())

    def test_a_folder_that_still_holds_something_stops_the_walk(self) -> None:
        (self.root / "job" / "vm-a").mkdir(parents=True)
        (self.root / "job" / "vm-b").mkdir()

        remove_empty_parents(self.root / "job" / "vm-a", below=self.root)

        self.assertEqual(sorted(path.name for path in (self.root / "job").iterdir()), ["vm-b"])

    def test_a_folder_outside_the_repository_is_never_touched(self) -> None:
        outside = self.root.parent / "elsewhere" / "vm"
        outside.mkdir(parents=True)

        remove_empty_parents(outside, below=self.root)
        remove_empty_parents(self.root, below=self.root)

        self.assertTrue(outside.is_dir())
        self.assertTrue(self.root.is_dir())

    def test_a_parent_removed_while_the_folder_is_made_is_made_again(self) -> None:
        target = self.root / "job" / "vm" / "task"
        real = storage_tool._create_private_directories
        calls: list[int] = []

        def racing(path: Path) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError(str(path.parent))
            real(path)

        with patch.object(storage_tool, "_create_private_directories", side_effect=racing):
            ensure_private_directory(target)

        self.assertTrue(target.is_dir())
        self.assertEqual(len(calls), 2)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o750")


if __name__ == "__main__":
    unittest.main()
