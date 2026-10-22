# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypsrp.complex_objects import PSInvocationState

from python.srv.plugins.hyperv import vhdx_tool
from python.srv.plugins.hyperv.hyperv import HyperVNode

RUN_TOOL = Path(vhdx_tool.__file__).resolve().parent / "scripts" / "run_tool.ps1"


def _smb_repository(root: str, **share: str) -> dict:
    return {"id": "repo-1", "name": "HV-SMB", "type": "smb", "path": root, **share}


class ShareInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="et-test-share-")

    def test_the_tool_writes_through_the_share_with_the_repositorys_account(self) -> None:
        repository = _smb_repository(
            self.root, server="192.0.2.90", share="hv-backups", username="abr-share", password="s3cret"
        )

        args, env = vhdx_tool.storage_invocation(
            repository, abr_path=f"{self.root}/job-1/vm-1/session-1", create_folder=True
        )

        self.assertEqual(
            args,
            ["--mode", "file", "--folder", r"\\192.0.2.90\hv-backups\job-1\vm-1\session-1", "--create-folder"],
        )
        self.assertEqual(
            env,
            {"ABR_SMB_SHARE": r"\\192.0.2.90\hv-backups", "ABR_SMB_USER": "abr-share", "ABR_SMB_PASSWORD": "s3cret"},
        )
        self.assertNotIn("s3cret", " ".join(args))

    def test_a_share_that_goes_on_into_a_folder_is_opened_at_its_root(self) -> None:
        repository = _smb_repository(self.root, server="backup01", share="hv/altastor", username="abr-share", password="x")

        args, env = vhdx_tool.storage_invocation(repository, abr_path=f"{self.root}/job-1")

        self.assertEqual(args[args.index("--folder") + 1], r"\\backup01\hv\altastor\job-1")
        self.assertEqual(env["ABR_SMB_SHARE"], r"\\backup01\hv")

    def test_a_folder_outside_the_repository_is_refused(self) -> None:
        repository = _smb_repository(self.root, server="backup01", share="hv", username="abr-share", password="x")

        with self.assertRaises(vhdx_tool.VhdxToolError):
            vhdx_tool.storage_invocation(repository, abr_path="/somewhere/else")

    def test_a_local_repository_is_refused_even_with_a_share_left_on_it(self) -> None:
        repository = {"id": "repo-1", "name": "HV-Local", "type": "local", "path": self.root, "server": "backup01", "share": "hv"}

        with self.assertRaises(vhdx_tool.VhdxToolError):
            vhdx_tool.storage_invocation(repository, abr_path=self.root)


class RunToolOpensTheShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = RUN_TOOL.read_text(encoding="utf-8")

    def test_the_share_is_opened_with_wnetaddconnection2_from_a_securestring(self) -> None:
        self.assertIn("[securestring]$SharePassword", self.script)
        self.assertIn("WNetAddConnection2W", self.script)
        self.assertIn("Marshal.SecureStringToGlobalAllocUnicode(password)", self.script)
        self.assertIn("Marshal.ZeroFreeGlobalAllocUnicode(secret)", self.script)
        self.assertIn("[AbrSmbConnection]::Add($share, $account, $SharePassword)", self.script)

    def test_no_command_that_puts_the_password_on_a_command_line_or_needs_an_interactive_logon(self) -> None:
        for command in ("New-SmbMapping", "net use", "New-PSDrive", "CreateProcessWithLogonW"):
            with self.subTest(command=command):
                self.assertNotIn(command, self.script)
        self.assertEqual(self.script.count("$SharePassword"), 3)

    def test_the_share_password_never_reaches_the_tools_environment(self) -> None:
        self.assertIn("-notlike 'ABR_SMB_*'", self.script)
        self.assertNotIn("ABR_SMB_PASSWORD", self.script)

    def test_the_connection_is_removed_however_the_run_ends(self) -> None:
        finally_block = self.script[self.script.rindex("} finally {"):]

        self.assertIn("if ($share) { [AbrSmbConnection]::Remove($share) }", finally_block)

    def test_an_account_without_a_domain_signs_in_against_the_share_server(self) -> None:
        self.assertRegex(self.script, r"if \(-not \(\$account\.Contains\('\\'\) -or \$account\.Contains\('@'\)\)\)")
        self.assertIn("(($share.TrimStart('\\') -split '\\\\')[0]) + '\\' + $account", self.script)

    def test_the_script_stays_ascii_for_windows_powershell_5(self) -> None:
        RUN_TOOL.read_bytes().decode("ascii")


class _FakeShell:
    def __init__(self, pool: "_FakePool") -> None:
        self.pool = pool
        self.scripts: list[str] = []
        self.parameters: dict = {}
        self.output = ["ABR_TOOL_EXIT=0"]
        self.had_errors = False
        self.streams = SimpleNamespace(error=[])
        self.state = PSInvocationState.NOT_STARTED
        pool.shells.append(self)

    def add_script(self, script: str) -> None:
        self.scripts.append(script)

    def add_parameters(self, parameters: dict) -> None:
        self.parameters.update(parameters)

    def add_parameter(self, name: str, value: object) -> None:
        self.parameters[name] = value

    def begin_invoke(self) -> None:
        self.state = PSInvocationState.COMPLETED

    def poll_invoke(self, timeout: int = 0) -> None:
        return None


class _FakePool:
    def __init__(self, wsman: object) -> None:
        self.shells: list[_FakeShell] = []
        self.keys_exchanged = False
        self.serialized: list[tuple[object, str]] = []
        _FakePool.last = self

    def __enter__(self) -> "_FakePool":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def exchange_keys(self) -> None:
        self.keys_exchanged = True

    def serialize(self, value: object, meta: object) -> str:
        self.serialized.append((value, meta.tag))
        return f"<secure {meta.tag}>"


class RunDiskToolSendsTheSharePasswordSecurelyTests(unittest.TestCase):
    def _run(self, env: dict) -> _FakePool:
        node = HyperVNode("hv.local", "CORP\\svc", "inventory-secret")
        node._client = SimpleNamespace(wsman=object())
        with patch("pypsrp.powershell.RunspacePool", _FakePool), patch(
            "pypsrp.powershell.PowerShell", _FakeShell
        ), patch.object(HyperVNode, "ensure_disk_tool", return_value=r"C:\abr\abr-vhdx-tool.exe"), patch.object(
            HyperVNode, "_bundle_folder", return_value=None
        ):
            node.run_disk_tool("", ["info", "--json"], env=env)
        return _FakePool.last

    def test_the_repositorys_password_goes_as_a_securestring_parameter_not_in_the_environment(self) -> None:
        pool = self._run({"ABR_SMB_SHARE": r"\\192.0.2.90\hv", "ABR_SMB_USER": "abr-share", "ABR_SMB_PASSWORD": "s3cret"})
        shell = pool.shells[0]

        self.assertTrue(pool.keys_exchanged)
        self.assertEqual(pool.serialized, [("s3cret", "SS")])
        self.assertEqual(shell.parameters["SharePassword"], "<secure SS>")
        self.assertEqual(shell.parameters["ToolEnv"], {"ABR_SMB_SHARE": r"\\192.0.2.90\hv", "ABR_SMB_USER": "abr-share"})
        self.assertNotIn("s3cret", repr(shell.parameters["ToolEnv"]) + repr(shell.parameters["ToolArgs"]))

    def test_a_share_without_its_own_account_is_opened_with_the_inventorys_account(self) -> None:
        pool = self._run({"ABR_SMB_SHARE": r"\\192.0.2.90\hv"})
        shell = pool.shells[0]

        self.assertEqual(pool.serialized, [("inventory-secret", "SS")])
        self.assertEqual(shell.parameters["ToolEnv"]["ABR_SMB_USER"], "CORP\\svc")

    def test_a_run_without_a_share_sends_no_password(self) -> None:
        pool = self._run({"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"})

        self.assertFalse(pool.keys_exchanged)
        self.assertNotIn("SharePassword", pool.shells[0].parameters)


if __name__ == "__main__":
    unittest.main()
