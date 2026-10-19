# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.storage_tool import (
    StorageToolError,
    copy_disk_tool_chain,
    parse_disk_tool_result,
    remove_disk_tool_backup,
    run_disk_tool_process,
    split_disk_tool_secrets,
    stdin_keys_text,
    verify_disk_tool_backup,
)
from python.srv.core.subprocess_util import (
    CompletedProcess,
    DataPathStalled,
    run_capped,
)
from python.srv.plugins.vcenter.vcenter_backup import _build_vddk_args

S3_REPOSITORY = {
    "name": "ObjectRepo",
    "type": "s3",
    "s3": {
        "bucket": "test-bucket",
        "prefix": "lab",
        "region": "us-east-1",
        "endpoint_url": "http://minio.example.test:9000",
        "access_key": "MINIOACCESS",
        "secret_key": "s3-secret-value",
        "path_style": True,
    },
}
SOURCE_S3_REPOSITORY = {
    "name": "SourceRepo",
    "type": "s3",
    "s3": {**S3_REPOSITORY["s3"], "bucket": "test-source-bucket", "secret_key": "source-s3-secret"},
}
LOCAL_REPOSITORY = {"name": "Local", "type": "local", "path": "/srv/backups"}
SECRET_COMMAND = ["tool", "verify", "--key", "enc-key-value", "--secret-key", "s3-secret-value", "--name", "x"]
SECRETS = ("enc-key-value", "s3-secret-value", "source-s3-secret", "source-enc-key", "vcenter-password")


class SplitDiskToolSecretsTests(unittest.TestCase):
    def test_every_secret_flag_moves_to_standard_input_under_its_name(self) -> None:
        argv, secrets = split_disk_tool_secrets(
            ["tool", "copy", "--key", "a", "--secret-key", "b", "--source-key", "c", "--source-secret-key", "d", "--name", "x"]
        )

        self.assertEqual(argv, ["tool", "copy", "--name", "x", "--stdin-keys"])
        self.assertEqual(secrets, [("key", "a"), ("secret-key", "b"), ("source-key", "c"), ("source-secret-key", "d")])
        self.assertEqual(stdin_keys_text(secrets), "key=a\nsecret-key=b\nsource-key=c\nsource-secret-key=d\n\n")

    def test_the_short_key_flag_and_the_vcenter_password_move_too(self) -> None:
        argv, secrets = split_disk_tool_secrets(["tool", "backup", "-k", "a", "--password", "p"])

        self.assertEqual(argv, ["tool", "backup", "--stdin-keys"])
        self.assertEqual(secrets, [("key", "a"), ("password", "p")])

    def test_a_command_without_secrets_is_left_as_it_is(self) -> None:
        command = ["tool", "list", "--mode", "file", "--folder", "/srv/backups"]

        self.assertEqual(split_disk_tool_secrets(command), (command, []))
        self.assertIsNone(stdin_keys_text([]))

    def test_an_empty_value_stays_where_it_was_so_the_tool_still_refuses_it(self) -> None:
        command = ["tool", "verify", "--key", "", "--name", "x"]

        self.assertEqual(split_disk_tool_secrets(command), (command, []))

    def test_a_secret_with_a_line_break_is_refused_rather_than_split(self) -> None:
        with self.assertRaisesRegex(StorageToolError, "key given to the backup tool must not contain a line break"):
            split_disk_tool_secrets(["tool", "backup", "--key", "a\nsecret-key=b"])


class CommandBuildersSendSecretsOnStdinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

        def fake_run(command: list[str], **kwargs) -> CompletedProcess:
            self.calls.append((list(command), kwargs.get("stdin_text")))
            return CompletedProcess(args=command, returncode=0, stdout='{"status": "ok"}', stderr="")

        for target in (
            patch.object(storage_tool, "run_capped", side_effect=fake_run),
            patch.object(storage_tool, "default_disk_tool_path", return_value=Path(sys.executable)),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _assert_no_secret_on(self, argv: list[str]) -> None:
        joined = " ".join(argv)
        for secret in SECRETS:
            self.assertNotIn(secret, joined)
        for flag in ("--key", "-k", "--secret-key", "--source-key", "--source-secret-key", "--password"):
            self.assertNotIn(flag, argv)

    def test_verify_sends_the_encryption_key_and_the_s3_secret_on_stdin(self) -> None:
        verify_disk_tool_backup(S3_REPOSITORY, "lab/vm", "01-disk.vmdk", encryption_key="enc-key-value")

        argv, stdin = self.calls[0]
        self._assert_no_secret_on(argv)
        self.assertIn("--stdin-keys", argv)
        self.assertEqual(argv[argv.index("--access-key") + 1], "MINIOACCESS")
        self.assertEqual(stdin, "key=enc-key-value\nsecret-key=s3-secret-value\n\n")

    def test_copy_sends_both_repositories_secrets_on_stdin(self) -> None:
        copy_disk_tool_chain(
            source_repository=SOURCE_S3_REPOSITORY,
            source_abr_path="lab/src",
            source_names=["01-disk.vmdk"],
            destination_repository=S3_REPOSITORY,
            destination_abr_path="lab/dst",
            backup_name="01-disk.vmdk",
            encryption_key="enc-key-value",
            source_encryption_key="source-enc-key",
        )

        argv, stdin = self.calls[0]
        self._assert_no_secret_on(argv)
        self.assertIn("--stdin-keys", argv)
        self.assertEqual(
            sorted(stdin.strip().splitlines()),
            [
                "key=enc-key-value",
                "secret-key=s3-secret-value",
                "source-key=source-enc-key",
                "source-secret-key=source-s3-secret",
            ],
        )

    def test_a_command_with_no_secret_gets_no_stdin_keys(self) -> None:
        remove_disk_tool_backup(LOCAL_REPOSITORY, "/srv/backups/vm", "01-disk.vmdk")

        argv, stdin = self.calls[0]
        self.assertNotIn("--stdin-keys", argv)
        self.assertIsNone(stdin)

    def test_the_vcenter_password_of_a_disk_command_goes_on_stdin(self) -> None:
        command = [
            "tool",
            "backup",
            *_build_vddk_args(
                vmdk_path="[ds] vm/vm.vmdk",
                host="vc.example.test",
                port=443,
                username="administrator@vsphere.local",
                password="vcenter-password",
                thumbprint="AA:BB",
                vm_spec="moref=vm-1",
            ),
        ]

        argv, secrets = split_disk_tool_secrets(command)

        self._assert_no_secret_on(argv)
        self.assertIn("administrator@vsphere.local", argv)
        self.assertEqual(secrets, [("password", "vcenter-password")])


class RunCappedStdinTests(unittest.TestCase):
    def test_the_lines_reach_the_process_on_its_standard_input(self) -> None:
        completed = run_capped(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().replace(chr(10), '|'))"],
            capture_output=True,
            stdin_text="key=abc\nsecret-key=def\n\n",
            nice=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("key=abc|secret-key=def||", str(completed.stdout))

    def test_a_process_that_never_reads_its_stdin_still_runs_to_the_end(self) -> None:
        completed = run_capped(
            [sys.executable, "-c", "print('done')"], capture_output=True, stdin_text="key=abc\n\n", nice=False
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("done", str(completed.stdout))

    def test_without_secrets_no_pipe_is_opened(self) -> None:
        with patch("python.srv.core.subprocess_util.subprocess.Popen", wraps=subprocess.Popen) as popen:
            run_capped([sys.executable, "-c", "print('done')"], capture_output=True, nice=False)

        self.assertNotIn("stdin", popen.call_args.kwargs)


class SecretsNeverReachLogsTests(unittest.TestCase):

    def test_output_that_echoes_a_secret_comes_back_masked(self) -> None:
        def echo(argv: list[str], **kwargs) -> CompletedProcess:
            return CompletedProcess(argv, 1, "result enc-key-value", "Error: bad key 'enc-key-value' for s3-secret-value")

        completed = run_disk_tool_process(echo, SECRET_COMMAND)

        for secret in ("enc-key-value", "s3-secret-value"):
            self.assertNotIn(secret, completed.stdout + completed.stderr)
        self.assertIn("********", completed.stderr)

    def test_a_timeout_and_a_lost_data_path_are_masked_too(self) -> None:
        def times_out(argv: list[str], **kwargs) -> CompletedProcess:
            raise subprocess.TimeoutExpired(argv, 5, output=b"enc-key-value", stderr=b"s3-secret-value")

        def stalls(argv: list[str], **kwargs) -> CompletedProcess:
            raise DataPathStalled(180, stdout="enc-key-value", stderr="s3-secret-value")

        with self.assertRaises(subprocess.TimeoutExpired) as timeout:
            run_disk_tool_process(times_out, SECRET_COMMAND)
        with self.assertRaises(DataPathStalled) as stall:
            run_disk_tool_process(stalls, SECRET_COMMAND)

        self.assertNotIn(b"enc-key-value", timeout.exception.output)
        self.assertNotIn(b"s3-secret-value", timeout.exception.stderr)
        self.assertNotIn("enc-key-value", " ".join(map(str, timeout.exception.cmd)))
        self.assertNotIn("enc-key-value", str(stall.exception.stdout) + str(stall.exception.stderr))

    def test_an_unreadable_result_names_no_secret_in_the_log_or_the_error(self) -> None:
        completed = CompletedProcess(SECRET_COMMAND, 3, "garbage enc-key-value", "Error: s3-secret-value refused")

        with self.assertLogs("python.srv.core.storage_tool", level="ERROR") as logs, self.assertRaises(
            StorageToolError
        ) as caught:
            parse_disk_tool_result(completed, SECRET_COMMAND)

        for secret in ("enc-key-value", "s3-secret-value"):
            self.assertNotIn(secret, "\n".join(logs.output))
            self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
