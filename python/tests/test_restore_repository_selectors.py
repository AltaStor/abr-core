# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

MISSING_ID = "11111111-2222-3333-4444-555555555555"


class RestoreRepositorySelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        reachable = patch(
            "python.srv.services.abr_v41.assert_inventory_registrable", return_value=None
        )
        reachable.start()
        self.addCleanup(reachable.stop)
        not_direct_esxi = patch("python.srv.services.abr_v41.is_direct_esxi_connection", return_value=False)
        not_direct_esxi.start()
        self.addCleanup(not_direct_esxi.stop)
        for catalog in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            catalog.clear()
        self.inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[self.inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        self.holder = abr_v41.create_repository_record(
            {"name": "RepoA", "type": "local", "path": tempfile.mkdtemp()}
        )
        self.other = abr_v41.create_repository_record(
            {"name": "RepoB", "type": "local", "path": tempfile.mkdtemp()}
        )

    def tearDown(self) -> None:
        for catalog in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            catalog.clear()

    def _seed_point(self, placements: list[dict[str, str]] | None = None) -> None:
        if placements is None:
            placements = [
                {
                    "repository_id": self.holder["id"],
                    "repository_name": self.holder["name"],
                    "manifest_path": "/tmp/backups/tstvm/x.json",
                    "backup_root": "/tmp/backups/tstvm",
                }
            ]
        BACKUPS["rp-1"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-1",
                "chain_id": "backup-1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "src-inv",
                "inventory_name": "src",
                "repository_id": self.holder["id"],
                "repository_name": self.holder["name"],
                "execution_type": "ActiveFull",
                "created_at": "2026-05-26T20:00:00+00:00",
                "artifact": {
                    "manifest_path": "/tmp/backups/tstvm/x.json",
                    "backup_root": "/tmp/backups/tstvm",
                },
                "placements": placements,
                "status": "available",
            }
        )

    def _start(self, **selector: str) -> dict[str, object]:
        payload = {
            "point_id": "rp-1",
            "name": "restoredtstvm",
            "target_inventory_id": self.inventory["id"],
            "target_host": "192.168.12.37",
            "target_datastore": "tstds",
        }
        payload.update(selector)
        return abr_v41.restore_start(payload)

    def _failure(self, **selector: str) -> ApiError:
        with self.assertRaises(ApiError) as ctx:
            self._start(**selector)
        return ctx.exception

    def test_both_selectors_start_a_restore_from_the_holding_repository(self) -> None:
        self._seed_point()
        self.assertTrue(self._start(repo=self.holder["name"])["restore_session_id"])
        TASKS.clear()
        self.assertTrue(self._start(repo_id=self.holder["id"])["restore_session_id"])

    def _second_inventory(self) -> dict[str, object]:
        other = abr_v41.create_inventory_record(
            {"name": "other", "type": "vsphere", "address": "203.0.113.38", "username": "root", "password": "secret"}
        )
        RESOURCES_BY_HV[other["id"]][("host", "esx-02.local")] = {
            "resource_type": "host",
            "external_id": "esx-02.local",
            "name": "esx-02.local",
            "payload": {"datastores": ["otherds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._seed_point()
        BACKUPS["rp-1"]["inventory_id"] = self.inventory["id"]
        return other

    def _restore_into(self, **target: str) -> dict[str, object]:
        payload = {"point_id": "rp-1", "name": "restoredtstvm", **target}
        return abr_v41.restore_start(payload)

    def test_a_target_inventory_given_by_name_is_used_instead_of_the_source_one(self) -> None:
        other = self._second_inventory()

        started = self._restore_into(target_inventory="other", target_host="esx-02.local", target_datastore="otherds")

        self.assertEqual(TASKS[started["restore_session_id"]]["hypervisor_id"], other["id"])

    def test_a_name_and_an_id_of_different_inventories_still_conflict(self) -> None:
        self._second_inventory()

        with self.assertRaises(ApiError) as ctx:
            self._restore_into(
                target_inventory="other",
                target_inventory_id=self.inventory["id"],
                target_host="esx-02.local",
                target_datastore="otherds",
            )

        self.assertEqual(ctx.exception.code, "selector_mismatch")
        self.assertEqual(
            ctx.exception.message,
            "Inventory selector mismatch: --target-inventory and --target-inventory-id refer to different records",
        )
        self.assertEqual(list(TASKS), [])

    def test_other_commands_still_name_their_own_selector_options(self) -> None:
        other = self._second_inventory()

        with self.assertRaises(ApiError) as ctx:
            abr_v41.resolve_inventory(name="other", record_id=self.inventory["id"])
        self.assertEqual(
            ctx.exception.message, "Inventory selector mismatch: --name and --id refer to different records"
        )
        self.assertEqual(abr_v41.resolve_inventory(name="other")["id"], other["id"])

    def test_the_restore_help_does_not_offer_an_id_where_only_a_name_is_looked_up(self) -> None:
        import argparse

        from python.cli.parsing.parser_builder import build_parser

        def subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
            action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            return action.choices[name]

        start = subparser(subparser(build_parser(), "restore"), "start")
        helps = {option: action.help for action in start._actions for option in action.option_strings}
        self.assertEqual(helps["--target-inventory"], "Target inventory name")
        self.assertEqual(helps["--target-inventory-id"], "Target inventory ID")

    def test_without_a_target_inventory_the_source_one_is_used(self) -> None:
        self._second_inventory()

        started = self._restore_into(target_host="192.168.12.37", target_datastore="tstds")

        self.assertEqual(TASKS[started["restore_session_id"]]["hypervisor_id"], self.inventory["id"])

    def test_a_restore_onto_a_direct_esxi_inventory_is_refused_before_anything_is_created(self) -> None:
        self._seed_point()
        with patch.object(abr_v41, "is_direct_esxi_connection", return_value=True) as check:
            error = self._failure()

        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "esx_direct_connection_unsupported")
        self.assertEqual(
            error.message,
            "A direct ESXi connection is not supported. Register the vCenter Server that manages this host instead.",
        )
        self.assertEqual(list(TASKS), [])
        stored = check.call_args.args[0]
        self.assertEqual(stored["id"], self.inventory["id"])
        self.assertEqual(stored["metadata"]["credentials"]["password"], "secret")

    def test_an_id_that_resolves_to_nothing_answers_repository_not_found(self) -> None:
        self._seed_point()
        for label, repo_id in (("unknown uuid", MISSING_ID), ("not a uuid", "not-a-uuid")):
            with self.subTest(label):
                error = self._failure(repo_id=repo_id)
                self.assertEqual(error.status_code, 404)
                self.assertEqual(error.code, "repository_not_found")
                self.assertIn("Repository not found", error.message)

    def test_the_two_selectors_answer_a_missing_repository_identically(self) -> None:
        self._seed_point()
        by_name = self._failure(repo="no-such-repo")
        by_id = self._failure(repo_id=MISSING_ID)
        self.assertEqual(
            (by_name.status_code, by_name.code), (by_id.status_code, by_id.code)
        )

    def test_the_two_selectors_answer_a_wrong_repository_on_the_same_rung(self) -> None:
        self._seed_point()
        by_name = self._failure(repo=self.other["name"])
        by_id = self._failure(repo_id=self.other["id"])
        self.assertEqual(by_name.status_code, by_id.status_code)
        self.assertEqual(by_id.status_code, 404)
        self.assertEqual(by_name.code, "restore_point_not_in_repository")
        self.assertEqual(by_id.code, "repository_mismatch")
        self.assertIn("does not belong", by_id.message)

    def test_does_not_belong_is_never_said_about_a_repository_that_is_absent(self) -> None:
        self._seed_point()
        for label, repo_id in (("unknown uuid", MISSING_ID), ("not a uuid", "not-a-uuid")):
            with self.subTest(label):
                self.assertNotIn("does not belong", self._failure(repo_id=repo_id).message)

    def test_a_missing_repository_is_told_apart_from_a_wrong_one(self) -> None:
        self._seed_point()
        missing = self._failure(repo_id=MISSING_ID)
        wrong = self._failure(repo_id=self.other["id"])
        self.assertNotEqual(missing.code, wrong.code)

    def test_a_backup_copy_can_be_restored_through_either_selector(self) -> None:
        self._seed_point(
            placements=[
                {
                    "repository_id": self.holder["id"],
                    "repository_name": self.holder["name"],
                    "manifest_path": "/tmp/backups/tstvm/x.json",
                    "backup_root": "/tmp/backups/tstvm",
                },
                {
                    "repository_id": self.other["id"],
                    "repository_name": self.other["name"],
                    "manifest_path": "/tmp/backups/tstvm/copy.json",
                    "backup_root": "/tmp/backups/tstvm",
                },
            ]
        )
        self.assertTrue(self._start(repo=self.other["name"])["restore_session_id"])
        TASKS.clear()
        self.assertTrue(self._start(repo_id=self.other["id"])["restore_session_id"])

    def test_the_remaining_selector_mismatches_still_answer_on_their_own_rung(self) -> None:
        self._seed_point()
        for label, selector, code in (
            ("backup chain", {"backup_id": "no-such-chain"}, "backup_mismatch"),
            ("source inventory", {"inventory_id": "no-such-inventory"}, "inventory_mismatch"),
            ("restore point", {"restore_point_id": "rp-other"}, "restore_point_mismatch"),
        ):
            with self.subTest(label):
                error = self._failure(**selector)
                self.assertEqual(error.status_code, 409)
                self.assertEqual(error.code, code)

    def test_an_empty_selector_is_not_treated_as_a_repository(self) -> None:
        self._seed_point()
        for label, selector in (
            ("no selector", {}),
            ("empty name", {"repo": ""}),
            ("empty id", {"repo_id": ""}),
            ("blank id", {"repo_id": "   "}),
        ):
            with self.subTest(label):
                TASKS.clear()
                self.assertTrue(self._start(**selector)["restore_session_id"])


if __name__ == "__main__":
    unittest.main()
