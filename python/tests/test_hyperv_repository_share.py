# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from python.srv.plugins.hyperv import vhdx_tool

RUN_TOOL = Path(vhdx_tool.__file__).resolve().parent / "scripts" / "run_tool.ps1"


def _local_repository(root: str, **share: str) -> dict:
    return {"id": "repo-1", "name": "HV-Local", "type": "local", "path": root, **share}


class ShareInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="et-test-share-")

    def test_the_tool_writes_through_the_share_with_the_repositorys_account(self) -> None:
        repository = _local_repository(
            self.root, server="192.168.12.90", share="hv-backups", username="abr-share", password="s3cret"
        )

        args, env = vhdx_tool.storage_invocation(
            repository, abr_path=f"{self.root}/job-1/vm-1/session-1", create_folder=True
        )

        self.assertEqual(
            args,
            ["--mode", "file", "--folder", r"\\192.168.12.90\hv-backups\job-1\vm-1\session-1", "--create-folder"],
        )
        self.assertEqual(
            env,
            {"ABR_SMB_SHARE": r"\\192.168.12.90\hv-backups", "ABR_SMB_USER": "abr-share", "ABR_SMB_PASSWORD": "s3cret"},
        )
        self.assertNotIn("s3cret", " ".join(args))

    def test_a_share_that_goes_on_into_a_folder_is_opened_at_its_root(self) -> None:
        repository = _local_repository(self.root, server="backup01", share="hv/altastor", username="abr-share", password="x")

        args, env = vhdx_tool.storage_invocation(repository, abr_path=f"{self.root}/job-1")

        self.assertEqual(args[args.index("--folder") + 1], r"\\backup01\hv\altastor\job-1")
        self.assertEqual(env["ABR_SMB_SHARE"], r"\\backup01\hv")

    def test_a_folder_outside_the_repository_is_refused(self) -> None:
        repository = _local_repository(self.root, server="backup01", share="hv", username="abr-share", password="x")

        with self.assertRaises(vhdx_tool.VhdxToolError):
            vhdx_tool.storage_invocation(repository, abr_path="/somewhere/else")


class RunToolOpensTheShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = RUN_TOOL.read_text(encoding="utf-8")

    def test_the_share_is_opened_by_starting_the_tool_with_network_only_credentials(self) -> None:
        self.assertIn("CreateProcessWithLogonW", self.script)
        self.assertRegex(self.script, r"LOGON_NETCREDENTIALS_ONLY\s*=\s*2")

    def test_no_mapping_that_a_winrm_session_signed_in_over_ntlm_cannot_hold(self) -> None:
        for command in ("New-SmbMapping", "net use", "New-PSDrive"):
            with self.subTest(command=command):
                self.assertNotIn(command, self.script)

    def test_the_share_password_reaches_neither_the_tool_nor_its_environment(self) -> None:
        handed_on = re.findall(r"\$environment\['([A-Z_]+)'\]\s*=", self.script)

        self.assertNotIn("ABR_SMB_PASSWORD", handed_on)
        self.assertIn("ABR_SMB_*", self.script)
        self.assertRegex(self.script, r"Start\(\$user, \$domain, \[string\]\$ToolEnv\['ABR_SMB_PASSWORD'\]")

    def test_the_wrapper_runs_from_a_file_so_the_command_line_stays_short(self) -> None:
        self.assertIn("-File", self.script)
        self.assertNotIn("-EncodedCommand", self.script)

    def test_a_stopped_run_ends_the_tool_and_leaves_no_files(self) -> None:
        finally_block = self.script[self.script.rindex("} finally {"):]

        self.assertIn("taskkill.exe /T /F /PID $processId", finally_block)
        self.assertIn("Remove-Item -LiteralPath $outFile, $exitFile, $runnerFile", finally_block)


if __name__ == "__main__":
    unittest.main()
