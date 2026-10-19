# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from python.srv.plugins.hyperv import vhdx_tool
from python.srv.plugins.hyperv.hyperv import HyperVError, HyperVNode
from python.tests.srv.hyperv.test_repository_share import _FakePool, _FakeShell

S3_REPOSITORY = {
    "name": "ObjectRepo",
    "type": "s3",
    "s3": {
        "bucket": "test-bucket",
        "prefix": "lab",
        "endpoint_url": "http://minio.example.test:9000",
        "access_key": "MINIOACCESS",
        "secret_key": "s3-secret-value",
        "path_style": True,
    },
}
RUN_TOOL = Path(vhdx_tool.__file__).resolve().parent / "scripts" / "run_tool.ps1"


class S3RepositoryInvocationTests(unittest.TestCase):
    def test_the_s3_keys_stay_with_the_tools_arguments_for_the_runner_to_split(self) -> None:
        args, env = vhdx_tool.storage_invocation(S3_REPOSITORY, abr_path="lab/vm")

        self.assertEqual(args[args.index("--access-key") + 1], "MINIOACCESS")
        self.assertEqual(args[args.index("--secret-key") + 1], "s3-secret-value")
        self.assertEqual(env, {})


class RunDiskToolSendsSecretsSecurelyTests(unittest.TestCase):
    def _run(self, args: list[str], output: list[str] | None = None) -> tuple[_FakePool, dict]:
        node = HyperVNode("hv.local", "CORP\\svc", "inventory-secret")
        node._client = SimpleNamespace(wsman=object())
        shells: list[_FakeShell] = []

        def shell(pool: _FakePool) -> _FakeShell:
            created = _FakeShell(pool)
            if output is not None:
                created.output = output
            shells.append(created)
            return created

        with patch("pypsrp.powershell.RunspacePool", _FakePool), patch("pypsrp.powershell.PowerShell", shell), patch.object(
            HyperVNode, "ensure_disk_tool", return_value=r"C:\abr\abr-vhdx-tool.exe"
        ), patch.object(HyperVNode, "_bundle_folder", return_value=None):
            result = node.run_disk_tool("", args)
        return _FakePool.last, result

    def test_the_encryption_key_and_the_s3_secret_go_as_securestring_parameters(self) -> None:
        pool, _ = self._run(
            ["backup", "--json", "--key", "enc-key-value", "--mode", "s3", "--access-key", "MINIOACCESS", "--secret-key", "s3-secret-value"]
        )
        shell = pool.shells[0]

        self.assertTrue(pool.keys_exchanged)
        self.assertEqual(sorted(pool.serialized), [("enc-key-value", "SS"), ("s3-secret-value", "SS")])
        self.assertEqual((shell.parameters["Key"], shell.parameters["SecretKey"]), ("<secure SS>", "<secure SS>"))
        self.assertEqual(
            shell.parameters["ToolArgs"], ["backup", "--json", "--mode", "s3", "--access-key", "MINIOACCESS", "--stdin-keys"]
        )
        self.assertNotIn("enc-key-value", repr(shell.parameters["ToolArgs"]) + repr(shell.parameters["ToolEnv"]))

    def test_a_copy_sends_the_source_secrets_the_same_way(self) -> None:
        pool, _ = self._run(["copy", "--source-key", "source-key-value", "--source-secret-key", "source-secret-value"])
        shell = pool.shells[0]

        self.assertEqual(shell.parameters["SourceKey"], "<secure SS>")
        self.assertEqual(shell.parameters["SourceSecretKey"], "<secure SS>")
        self.assertEqual(shell.parameters["ToolArgs"], ["copy", "--stdin-keys"])

    def test_a_run_without_secrets_exchanges_no_keys_and_adds_no_stdin_keys(self) -> None:
        pool, _ = self._run(["info", "--json"])

        self.assertFalse(pool.keys_exchanged)
        self.assertEqual(pool.shells[0].parameters["ToolArgs"], ["info", "--json"])

    def test_a_secret_abr_vhdx_tool_cannot_take_is_refused_before_anything_runs(self) -> None:
        with self.assertRaisesRegex(HyperVError, "takes no password"):
            self._run(["backup", "--password", "nope"])

    def test_a_secret_the_tool_echoes_is_masked_in_what_comes_back(self) -> None:
        _, result = self._run(
            ["verify", "--key", "enc-key-value"],
            output=["Error: key enc-key-value does not decrypt", "ABR_TOOL_EXIT=1"],
        )

        self.assertNotIn("enc-key-value", " ".join(result["lines"]))


class RunToolPipesTheSecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = RUN_TOOL.read_text(encoding="utf-8")

    def test_every_secret_arrives_as_a_securestring(self) -> None:
        for name in ("Key", "SecretKey", "SourceKey", "SourceSecretKey"):
            with self.subTest(name=name):
                self.assertIn(f"[securestring]${name}", self.script)

    def test_the_secrets_are_piped_to_the_tool_in_utf8_and_end_with_an_empty_line(self) -> None:
        self.assertIn("'key' = $Key; 'secret-key' = $SecretKey; 'source-key' = $SourceKey; 'source-secret-key' = $SourceSecretKey", self.script)
        self.assertIn("$global:OutputEncoding = New-Object Text.UTF8Encoding $false", self.script)
        self.assertIn("($stdin + '') | & $Tool @ToolArgs", self.script)
        self.assertIn("$global:OutputEncoding = $savedEncoding", self.script)

    def test_the_plain_text_is_taken_from_unmanaged_memory_and_zeroed(self) -> None:
        self.assertIn("SecureStringToGlobalAllocUnicode($Value)", self.script)
        self.assertIn("ZeroFreeGlobalAllocUnicode($pointer)", self.script)


if __name__ == "__main__":
    unittest.main()
