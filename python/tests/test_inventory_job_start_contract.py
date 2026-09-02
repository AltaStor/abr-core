# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


class CliHelpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def _parse_help(self, argv: list[str]) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(argv)
        return stream.getvalue()

    def test_inventory_help_explains_automatic_discovery(self) -> None:
        add_help = self._parse_help(["inventory", "add", "--help"])
        rescan_help = self._parse_help(["inventory", "rescan", "--help"])

        # `inventory add` no longer instructs a mandatory manual rescan; it tells
        # the user discovery happens automatically when VM names are first used.
        self.assertIn("runs automatically the first time you reference VM names", add_help)
        self.assertNotIn("run inventory rescan before using VM names", add_help)
        # `inventory rescan` remains available for explicit re-discovery.
        self.assertIn("re-discover on demand", rescan_help)

    def test_job_start_help_shows_full_alias_and_examples(self) -> None:
        help_output = self._parse_help(["job", "start", "--help"])

        self.assertIn("--full, --force-full", help_output)
        self.assertIn("Force a full backup instead of an incremental backup", help_output)
        self.assertIn('abr job start --name "Job-SQL-Server" --full', help_output)
        self.assertIn("abr job start --id <job-id> --full", help_output)

        by_full = self.parser.parse_args(["job", "start", "--name", "job01", "--full"])
        by_force_full = self.parser.parse_args(["job", "start", "--name", "job01", "--force-full"])

        self.assertTrue(by_full.full)
        self.assertTrue(by_force_full.full)


class InventoryDiscoveryAndJobStartEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "local", "path": "/tmp/repo-1"}

    def _create_inventory(self) -> dict[str, object]:
        return abr_v41.create_inventory_record(
            {
                "name": "vcenter-main",
                "type": "hyper-v",
                "address": "192.168.10.10",
                "username": "administrator",
                "password": "secret",
            }
        )

    def _discover_vm(self, inventory: dict[str, object], *, vm_id: str = "vm-101", vm_name: str = "web-01") -> None:
        RESOURCES_BY_HV[str(inventory["id"])] = {
            ("virtual_machine", vm_id): {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": vm_name,
                "payload": {"path": f"/Production/{vm_name}"},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }

    def test_job_create_auto_discovers_after_inventory_add(self) -> None:
        # Regression: creating a job by VM name immediately after `inventory add`
        # must succeed without a manual `inventory rescan` — discovery is lazily
        # triggered inside job create.
        inventory = self._create_inventory()
        self.assertEqual(RESOURCES_BY_HV.get(str(inventory["id"]), {}), {})

        job = abr_v41.create_backup_job(
            {
                "name": "Nightly Backup",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
            }
        )

        self.assertEqual(job["vm_names"], ["vm-1"])
        self.assertTrue(job["vm_ids"])
        # Discovery results were persisted, so the inventory is now scanned.
        self.assertTrue(abr_v41._resolve_inventory_vm_records(str(inventory["id"])))

    def test_job_create_unknown_vm_fails_without_rescan_guidance(self) -> None:
        # A VM name that does not exist even after automatic discovery must fail
        # clearly, and must not tell the user to run a manual rescan.
        inventory = self._create_inventory()

        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_backup_job(
                {
                    "name": "Nightly Backup",
                    "vms": ["no-such-vm"],
                    "inventory_id": inventory["id"],
                    "repo_id": "repo-1",
                }
            )

        self.assertEqual(ctx.exception.code, "vm_not_found")
        self.assertNotIn("inventory rescan", ctx.exception.message)

    def test_inventory_add_triggers_background_discovery_when_enabled(self) -> None:
        # When the server enables auto-discovery on add, `inventory add` kicks
        # off discovery in the background so VM names are usable without a
        # manual rescan. The add call itself must not block on it.
        original = abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD
        abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD = True
        try:
            inventory = self._create_inventory()
            # Deterministically wait for the background scan instead of sleeping.
            abr_v41._inventory_discovery_lock(str(inventory["id"]))
            for thread in threading.enumerate():
                if thread.name == f"inventory-discovery-{inventory['id']}":
                    thread.join(timeout=5)
        finally:
            abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD = original

        self.assertTrue(abr_v41._resolve_inventory_vm_records(str(inventory["id"])))

    def test_job_start_uses_first_run_incremental_then_forced_full(self) -> None:
        inventory = self._create_inventory()
        self._discover_vm(inventory)
        job = abr_v41.create_backup_job(
            {
                "name": "Nightly Backup",
                "vms": ["web-01"],
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
            }
        )

        first_run = abr_v41.start_job_record(str(job["id"]))
        self.assertEqual(first_run["items"][0]["resolved_backup_type"], "ActiveFull")
        self.assertEqual(first_run["items"][0]["resolved_backup_reason"], "FirstRun")

        BACKUPS["rp-1"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-1",
                "job_id": job["id"],
                "job_name": job["name"],
                "vm_id": "vm-101",
                "vm_name": "web-01",
                "inventory_id": inventory["id"],
                "inventory_name": inventory["name"],
                "repository_id": "repo-1",
                "repository_name": "Repo One",
                "execution_type": "ActiveFull",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "available",
            }
        )

        second_run = abr_v41.start_job_record(str(job["id"]))
        self.assertEqual(second_run["items"][0]["resolved_backup_type"], "Incremental")
        self.assertEqual(second_run["items"][0]["resolved_backup_reason"], "StandardIncremental")

        forced_run = abr_v41.start_job_record(str(job["id"]), force_full=True)
        self.assertEqual(forced_run["items"][0]["resolved_backup_type"], "ActiveFull")
        self.assertEqual(forced_run["items"][0]["resolved_backup_reason"], "ForceFull")


if __name__ == "__main__":
    unittest.main()
