# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv_backup, vhdx_tool
from python.srv.plugins.hyperv.hyperv import HyperVError


STORAGE_ARGS = ["--mode", "s3", "--folder", "bucket", "--path", "et/vm/task-1", "--endpoint", "http://s3.example.test:9000"]
STORAGE_ENV = {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"}

UNREACHABLE_LINES = [
    "S3 bucket 'bucket' is not accessible at endpoint http://s3.example.test:9000: "
    "Encountered network error when sending http request. Create the bucket or enable bucket auto-creation.",
    "Failed to initialize backup manager!",
    "Error: failed to initialize 's3' backup storage at 'bucket'",
    "{",
    '  "command": "info",',
    '  "status": "error",',
    '  "error": "failed to initialize \'s3\' backup storage at \'bucket\'"',
    "}",
]
UNREACHABLE_RESULT = {"command": "info", "status": "error", "error": "failed to initialize 's3' backup storage at 'bucket'"}


class FakeNode:
    def __init__(self, info_run: dict) -> None:
        self.info_run = info_run
        self.calls: list[tuple] = []

    def ensure_vm_host_connected(self, vm_id, hint=None):
        return {"name": "et-vm", "computer": "NODE-A", "state": "Off", "version": "12.0"}

    def get_service_type(self):
        return "Cluster"

    def ensure_disk_tool(self, node):
        return r"C:\Program Files\AltaStor\abr-vhdx-tool.exe"

    def run_disk_tool(self, node, args, *, env=None, on_progress=None, should_cancel=None, timeout=None):
        self.calls.append(("run_disk_tool", node, list(args), dict(env or {})))
        return self.info_run

    def create_recovery_checkpoint(self, vm_id, consistency="application"):
        self.calls.append(("create_recovery_checkpoint", vm_id, consistency))
        raise HyperVError("checkpoint refused by the test")


class RepositoryCheckBeforeCheckpointTests(unittest.TestCase):
    def _run(self, info_run: dict) -> tuple[FakeNode, Exception | None, unittest.mock.MagicMock]:
        node = FakeNode(info_run)
        task = {"id": "task-1", "hypervisor_id": "inv-1", "parameters": {}}
        repository = {"id": "repo-1", "name": "repo", "type": "s3"}
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(patch.object(hyperv_backup, "_get_hypervisor", return_value={"name": "HyperV-test"}))
            stack.enter_context(patch.object(hyperv_backup, "connection_settings", return_value={"host": "hv.example.test"}))
            stack.enter_context(
                patch.object(
                    hyperv_backup,
                    "_resolve_backup_target",
                    return_value=({"external_id": "vm-1", "name": "et-vm", "payload": {}}, []),
                )
            )
            stack.enter_context(patch.object(hyperv_backup, "_resolve_repository", return_value=repository))
            stack.enter_context(patch.object(hyperv_backup.vhdx_tool, "ensure_hyperv_repository"))
            stack.enter_context(
                patch.object(hyperv_backup.vhdx_tool, "storage_invocation", return_value=(STORAGE_ARGS, STORAGE_ENV))
            )
            stack.enter_context(
                patch.object(
                    hyperv_backup,
                    "_resolve_backup_storage_layout",
                    return_value=(root, root / "task-1", "et/vm/task-1"),
                )
            )
            stack.enter_context(patch.object(hyperv_backup, "ensure_private_directory"))
            stack.enter_context(patch.object(hyperv_backup, "_previous_manifest", return_value=(None, None)))
            stack.enter_context(patch.object(hyperv_backup, "_repository_label", return_value="repo (S3 bucket bucket)"))
            stack.enter_context(patch.object(hyperv_backup, "resolve_job_backup_mode", return_value=None))
            stack.enter_context(patch.object(hyperv_backup, "encryption_changed_since_last_backup", return_value=False))
            stack.enter_context(patch.object(hyperv_backup, "_remove_stranded_checkpoints"))
            stack.enter_context(patch.object(hyperv_backup, "cancel_requested", return_value=False))
            discard = stack.enter_context(patch.object(hyperv_backup, "_discard_task_backups", return_value=[]))
            stack.enter_context(patch.object(hyperv_backup, "open_node", return_value=contextlib.nullcontext(node)))
            error = None
            with self.assertLogs(hyperv_backup.logger, level="ERROR"):
                try:
                    hyperv_backup.run_hyperv_backup(task)
                except Exception as exc:
                    error = exc
        return node, error, discard

    def test_unreachable_repository_fails_before_any_checkpoint(self) -> None:
        node, error, discard = self._run({"exit_code": 1, "result": UNREACHABLE_RESULT, "lines": UNREACHABLE_LINES})

        self.assertIsInstance(error, hyperv_backup.HyperVBackupError)
        self.assertIn("NODE-A cannot reach repository repo (S3 bucket bucket)", str(error))
        self.assertIn("is not accessible at endpoint http://s3.example.test:9000", str(error))
        self.assertEqual(
            node.calls,
            [("run_disk_tool", "NODE-A", ["info", "--json", *STORAGE_ARGS], STORAGE_ENV)],
        )
        discard.assert_called_once()
        self.assertEqual(discard.call_args.args[2], [])

    def test_reachable_repository_goes_on_to_the_checkpoint(self) -> None:
        node, error, _ = self._run(
            {"exit_code": 0, "result": {"command": "info", "status": "ok"}, "lines": ["{", "}"]}
        )

        self.assertIsInstance(error, hyperv_backup.HyperVBackupError)
        self.assertIn("could not checkpoint", str(error))
        self.assertEqual([call[0] for call in node.calls], ["run_disk_tool", "create_recovery_checkpoint"])


class FailureMessageTests(unittest.TestCase):
    def test_tool_diagnostics_lead_the_error(self) -> None:
        message = vhdx_tool.failure_message(UNREACHABLE_RESULT, UNREACHABLE_LINES, 1)

        self.assertEqual(
            message,
            "S3 bucket 'bucket' is not accessible at endpoint http://s3.example.test:9000: Encountered network "
            "error when sending http request. Create the bucket or enable bucket auto-creation.; "
            "Failed to initialize backup manager!; failed to initialize 's3' backup storage at 'bucket'",
        )

    def test_only_the_last_diagnostic_lines_are_kept(self) -> None:
        lines = [f"note {number}" for number in range(10)] + ["Error: broken"]

        message = vhdx_tool.failure_message(None, lines, 1)

        self.assertEqual(message, "note 5; note 6; note 7; note 8; note 9; broken")

    def test_no_output_falls_back_to_the_exit_code(self) -> None:
        self.assertEqual(vhdx_tool.failure_message(None, [], 3), "exit code 3")

    def test_lost_change_tracking_is_read_from_the_tool_error_only(self) -> None:
        lost = {"status": "error", "error": "resilient change tracking does not know this id; start a new chain"}
        unrelated = {"status": "error", "error": "failed to initialize 's3' backup storage at 'bucket'"}
        noisy_lines = ["resilient change tracking id rctX:1 opened", "Error: failed to initialize 's3' backup storage"]

        self.assertTrue(vhdx_tool.change_tracking_lost(lost, [], 1))
        self.assertFalse(vhdx_tool.change_tracking_lost(unrelated, noisy_lines, 1))


if __name__ == "__main__":
    unittest.main()
