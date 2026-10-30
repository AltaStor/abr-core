# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.storage_tool import chain_source_names, disk_tool_restores_chains
from python.srv.plugins.hyperv import hyperv_restore, vhdx_tool
from python.srv.plugins.vcenter import vcenter_restore

TOOL = Path("/opt/altastor-backup/tools/abr-vmdk-tool/abr-vmdk-tool.sh")
S3_REPOSITORY = {"id": "repo-s3", "type": "s3", "s3": {"bucket": "bucket", "region": "us-east-1"}}
LOCAL_REPOSITORY = {"id": "repo-local", "type": "local", "path": "/srv/backups"}


def _layer(path: str, name: str, mode: str) -> dict:
    return {"abr_path": path, "abr_backup_name": name, "store_mode": mode, "source": f"{path}/{name}"}


S3_CHAIN = [
    _layer("prefix/job/vm/task-full", "01-vm.vmdk", "full"),
    _layer("prefix/job/vm/task-inc1", "01-vm.vmdk", "incremental"),
    _layer("prefix/job/vm/task-inc2", "01-vm.vmdk", "incremental"),
]
LOCAL_CHAIN = [
    _layer("/srv/backups/job/vm/task-full", "01-vm.vmdk", "full"),
    _layer("/srv/backups/job/vm/task-inc1", "01-vm.vmdk", "incremental"),
]


class ChainSourceNamesTests(unittest.TestCase):
    def test_s3_layers_are_named_from_their_common_prefix(self) -> None:
        self.assertEqual(
            chain_source_names([layer["abr_path"] for layer in S3_CHAIN], [layer["abr_backup_name"] for layer in S3_CHAIN]),
            ("prefix/job/vm", ["task-full/01-vm.vmdk", "task-inc1/01-vm.vmdk", "task-inc2/01-vm.vmdk"]),
        )

    def test_local_layers_keep_their_absolute_root(self) -> None:
        self.assertEqual(
            chain_source_names([layer["abr_path"] for layer in LOCAL_CHAIN], ["a.vmdk", "b.vmdk"]),
            ("/srv/backups/job/vm", ["task-full/a.vmdk", "task-inc1/b.vmdk"]),
        )

    def test_layers_written_under_two_layouts_meet_at_the_repository(self) -> None:
        self.assertEqual(
            chain_source_names(["prefix/by-name/vm/t1", "prefix/by-id/vm/t2"], ["d", "d"]),
            ("prefix", ["by-name/vm/t1/d", "by-id/vm/t2/d"]),
        )

    def test_layers_with_nothing_in_common_name_themselves_in_full(self) -> None:
        self.assertEqual(chain_source_names(["a/t1", "b/t2"], ["d", "d"]), ("", ["a/t1/d", "b/t2/d"]))

    def test_the_same_folder_twice_needs_no_relative_part(self) -> None:
        self.assertEqual(chain_source_names(["/r/t", "/r/t"], ["full", "inc"]), ("/r/t", ["full", "inc"]))


class ToolChainSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        storage_tool._tool_help_answers.clear()
        self.addCleanup(storage_tool._tool_help_answers.clear)

    def _launcher(self, help_text: str) -> Path:
        launcher = self.folder / "abr-vmdk-tool.sh"
        launcher.write_text(f"#!/bin/sh\necho '{help_text}'\n", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        return launcher

    def test_a_tool_whose_restore_help_names_the_chain_option_restores_chains(self) -> None:
        self.assertTrue(disk_tool_restores_chains(self._launcher("--source-names LIST Comma-separated chain")))

    def test_an_older_tool_restores_one_backup_at_a_time(self) -> None:
        self.assertFalse(disk_tool_restores_chains(self._launcher("-n, --name NAME Backup name")))

    def test_the_restore_and_retention_answers_are_kept_apart(self) -> None:
        launcher = self._launcher("--retention-till DATE")

        self.assertTrue(storage_tool.disk_tool_supports_retention_till(launcher))
        self.assertFalse(disk_tool_restores_chains(launcher))


class VCenterRestoreRunsTests(unittest.TestCase):
    def _runs(self, layers: list[dict], *, chains: bool = True, repository: dict = S3_REPOSITORY) -> list[dict]:
        with patch.object(vcenter_restore, "disk_tool_restores_chains", return_value=chains):
            return vcenter_restore._abr_restore_runs(layers, tool_path=TOOL, repository=repository)

    def test_a_chain_restores_in_one_run_from_its_common_folder(self) -> None:
        runs = self._runs(S3_CHAIN)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["abr_path"], "prefix/job/vm")
        self.assertEqual(runs[0]["source_names"], ["task-full/01-vm.vmdk", "task-inc1/01-vm.vmdk", "task-inc2/01-vm.vmdk"])
        self.assertEqual(runs[0]["store_mode"], "full")
        self.assertIn("2 incremental(s)", runs[0]["source"])

    def test_a_local_chain_restores_in_one_run_too(self) -> None:
        runs = self._runs(LOCAL_CHAIN, repository=LOCAL_REPOSITORY)

        self.assertEqual([run["abr_path"] for run in runs], ["/srv/backups/job/vm"])

    def test_a_chain_that_starts_with_an_incremental_keeps_its_force(self) -> None:
        runs = self._runs(S3_CHAIN[1:])

        self.assertEqual(runs[0]["store_mode"], "incremental")

    def test_without_the_chain_option_every_layer_restores_on_its_own(self) -> None:
        self.assertEqual(self._runs(S3_CHAIN, chains=False), S3_CHAIN)

    def test_a_single_full_backup_still_restores_by_name(self) -> None:
        self.assertEqual(self._runs(S3_CHAIN[:1]), S3_CHAIN[:1])

    def test_a_name_with_a_comma_cannot_go_into_the_list(self) -> None:
        layers = [dict(S3_CHAIN[0], abr_backup_name="01-vm,a.vmdk"), S3_CHAIN[1]]

        self.assertEqual(self._runs(layers), layers)

    def test_local_layers_under_two_roots_restore_layer_by_layer(self) -> None:
        layers = [_layer("/srv/a/t1", "d", "full"), _layer("/mnt/b/t2", "d", "incremental")]

        self.assertEqual(self._runs(layers, repository=LOCAL_REPOSITORY), layers)

    def test_the_chain_command_takes_the_list_in_place_of_a_name(self) -> None:
        run = self._runs(S3_CHAIN)[0]
        command = vcenter_restore._build_abr_restore_command(
            tool_path=TOOL,
            repository=S3_REPOSITORY,
            abr_path=run["abr_path"],
            backup_name=run["abr_backup_name"],
            remote_datastore_path="[ds] vm/vm.vmdk",
            host="vc.local",
            port=443,
            username="svc",
            password="secret",
            thumbprint="AA:BB",
            vm_moref="vm-1",
            transport_mode=None,
            force=False,
            source_names=run["source_names"],
        )

        self.assertNotIn("--name", command)
        self.assertNotIn("--force", command)
        self.assertEqual(
            command[command.index("--source-names") + 1], "task-full/01-vm.vmdk,task-inc1/01-vm.vmdk,task-inc2/01-vm.vmdk"
        )
        self.assertEqual(command[command.index("--path") + 1], "prefix/job/vm")


class HyperVRestoreRunsTests(unittest.TestCase):
    def _runs(self, layers: list[dict], repository: dict, *, chains: bool = True) -> list[dict]:
        with patch.object(vhdx_tool, "restores_chains", return_value=chains):
            return hyperv_restore._restore_runs(layers, repository)

    def test_an_s3_chain_goes_to_the_host_tool_in_one_run(self) -> None:
        runs = self._runs(S3_CHAIN, S3_REPOSITORY)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["source_names"], ["task-full/01-vm.vmdk", "task-inc1/01-vm.vmdk", "task-inc2/01-vm.vmdk"])

    def test_a_share_chain_is_named_with_windows_separators(self) -> None:
        runs = self._runs(LOCAL_CHAIN, {"id": "repo-smb", "type": "smb", "path": "/srv/backups"})

        self.assertEqual(runs[0]["abr_path"], "/srv/backups/job/vm")
        self.assertEqual(runs[0]["source_names"], ["task-full\\01-vm.vmdk", "task-inc1\\01-vm.vmdk"])

    def test_an_older_host_tool_restores_layer_by_layer(self) -> None:
        self.assertEqual(self._runs(S3_CHAIN, S3_REPOSITORY, chains=False), S3_CHAIN)

    def test_a_single_backup_is_restored_by_name(self) -> None:
        self.assertEqual(self._runs(S3_CHAIN[:1], S3_REPOSITORY), S3_CHAIN[:1])


class HostToolChainSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tool = Path(tmp.name) / "abr-vhdx-tool.exe"
        vhdx_tool._tool_mentions.clear()
        self.addCleanup(vhdx_tool._tool_mentions.clear)

    def _restores_chains(self, content: bytes) -> bool:
        self.tool.write_bytes(content)
        with patch.object(vhdx_tool, "local_tool_path", return_value=self.tool):
            return vhdx_tool.restores_chains()

    def test_a_host_tool_whose_restore_takes_a_list_restores_chains(self) -> None:
        self.assertTrue(self._restores_chains(b"MZ\0Error: restore takes either --name or --source-names, not both\0"))

    def test_copy_alone_taking_a_list_is_not_enough(self) -> None:
        self.assertFalse(self._restores_chains(b"MZ\0--source-names LIST copy only\0"))


if __name__ == "__main__":
    unittest.main()
