# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from pypsrp.complex_objects import PSInvocationState

from python.srv.plugins.hyperv import hyperv, hyperv_restore
from python.srv.plugins.hyperv.hyperv import HyperVNode, HyperVUnreachableError

DISK = r"C:\Hyper-V\vm-r\Virtual Hard Disks\disk0.vhdx"
FOLDER = r"C:\Hyper-V\vm-r\Virtual Hard Disks"


def _client(port: int = 5985, ssl: bool = False) -> SimpleNamespace:
    transport = SimpleNamespace(server="hv.example.test", port=port, ssl=ssl, encryption=object())
    return SimpleNamespace(wsman=SimpleNamespace(transport=transport))


class _Pool:
    def __init__(self, connection, close_error: BaseException | None = None) -> None:
        self.connection = connection
        self.close_error = close_error

    def open(self) -> None:
        return None

    def close(self) -> None:
        if self.close_error:
            raise self.close_error


class _Shell:
    def __init__(self, pool, outages: int, lines: list[str]) -> None:
        self.pool = pool
        self.state = PSInvocationState.NOT_STARTED
        self.output: list[str] = []
        self.had_errors = False
        self.outages = outages
        self.lines = lines
        self.polls = 0

    def add_script(self, script) -> None:
        return None

    def add_parameters(self, parameters) -> None:
        return None

    def begin_invoke(self) -> None:
        self.state = PSInvocationState.RUNNING

    def poll_invoke(self, timeout=None) -> None:
        self.polls += 1
        if self.outages:
            self.outages -= 1
            raise requests.ConnectionError("[Errno 111] Connection refused")
        self.output.extend(self.lines)
        self.state = PSInvocationState.COMPLETED


class RunDiskToolTests(unittest.TestCase):
    def _run(self, outages: int, window: int = 300, close_error: BaseException | None = None):
        node = HyperVNode.__new__(HyperVNode)
        client = _client()
        shells: list[_Shell] = []
        notices: list[str] = []
        lines = ['{"command": "restore", "status": "ok"}', "ABR_TOOL_EXIT=0"]

        def make_shell(pool):
            shells.append(_Shell(pool, outages, lines))
            return shells[-1]

        clock = iter(range(0, 100000, 10))
        with (
            patch.object(HyperVNode, "ensure_disk_tool", return_value=r"C:\tool\abr-vhdx-tool.exe"),
            patch.object(HyperVNode, "_client_for", return_value=client),
            patch.object(HyperVNode, "_bundle_folder", return_value=None),
            patch("pypsrp.powershell.RunspacePool", side_effect=lambda connection: _Pool(connection, close_error)),
            patch("pypsrp.powershell.PowerShell", side_effect=make_shell),
            patch.object(hyperv, "reconnect_seconds", return_value=window),
            patch.object(hyperv.time, "sleep"),
            patch.object(hyperv.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            result = node.run_disk_tool("HV-NODE-02", ["restore", "--json"], on_notice=notices.append)
        return result, notices, client, shells[0]

    def test_a_short_loss_of_the_connection_is_followed_through_to_the_tools_result(self) -> None:
        result, notices, client, shell = self._run(outages=3)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(shell.polls, 4)
        self.assertIsNone(client.wsman.transport.encryption)
        self.assertIn("Lost the connection to HV-NODE-02", notices[0])
        self.assertIn("is back after", notices[1])

    def test_a_connection_that_stays_down_ends_the_run_naming_the_host_and_port(self) -> None:
        with self.assertRaises(HyperVUnreachableError) as caught:
            self._run(outages=1000, window=60)

        message = str(caught.exception)
        self.assertIn("Hyper-V host hv.example.test did not answer WinRM over HTTP on port 5985", message)
        self.assertIn("did not come back within 60 s", message)

    def test_closing_the_runspace_on_a_dead_connection_does_not_hide_the_reason(self) -> None:
        with self.assertRaises(HyperVUnreachableError):
            self._run(outages=1000, window=60, close_error=requests.ConnectionError("refused"))


class ScriptConnectionLossTests(unittest.TestCase):
    def test_a_script_on_a_dropped_connection_fails_naming_the_host(self) -> None:
        node = HyperVNode.__new__(HyperVNode)
        client = _client(port=5986, ssl=True)
        client.execute_ps = lambda script: (_ for _ in ()).throw(requests.ConnectionError("Max retries exceeded"))

        with patch.object(HyperVNode, "_client_for", return_value=client), self.assertRaises(HyperVUnreachableError) as caught:
            node._run("Get-VM", "HV-NODE-02")

        self.assertIn("did not answer WinRM over HTTPS on port 5986", str(caught.exception))


class _Node:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def resolve_node(self, name):
        return "HV-NODE-02"

    def destroy_vm(self, vm_id):
        self.calls.append(("destroy_vm", vm_id))

    def stop_disk_tool(self, node, paths):
        self.calls.append(("stop_disk_tool", list(paths)))
        return ["abr-vhdx-tool (process 4242, started 2026-11-03T10:00:00)"]

    def remove_files(self, node, paths):
        self.calls.append(("remove_files", list(paths)))
        return []

    def remove_empty_folders(self, node, folders):
        self.calls.append(("remove_empty_folders", list(folders)))
        return []


class UndoWhileTheHostIsDownTests(unittest.TestCase):
    def _undo(self, attempts: list, window: int = 300, created_vm_id: str | None = None):
        notes: list[str] = []
        clock = iter(range(0, 100000, 10))

        def open_node(connection):
            attempt = attempts.pop(0) if attempts else attempts_left
            if isinstance(attempt, BaseException):
                raise attempt
            return contextlib.nullcontext(attempt)

        attempts_left = HyperVUnreachableError("hv.example.test", [5985], protocol="http")
        with (
            patch.object(hyperv_restore, "open_node", side_effect=open_node),
            patch.object(hyperv_restore, "reconnect_seconds", return_value=window),
            patch.object(hyperv_restore.time, "sleep"),
            patch.object(hyperv_restore.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(hyperv_restore, "_undo_note", side_effect=lambda cb, warn, stage, progress, message: notes.append(message)),
        ):
            left = hyperv_restore._undo(
                {"host": "hv.example.test"}, "hv-node-02", created_vm_id, [], [DISK], [FOLDER], "", None
            )
        return left, notes

    def test_the_cleanup_waits_for_the_host_then_stops_the_tool_and_removes_what_it_wrote(self) -> None:
        calls: list = []
        unreachable = HyperVUnreachableError("hv.example.test", [5985], protocol="http")

        left, notes = self._undo([unreachable, unreachable, _Node(calls)])

        self.assertEqual(left, "")
        self.assertEqual(calls, [("stop_disk_tool", [DISK]), ("remove_files", [DISK]), ("remove_empty_folders", [FOLDER])])
        self.assertIn("trying to undo the restore again", notes[0])
        self.assertIn("Stopped abr-vhdx-tool (process 4242", notes[1])

    def test_a_host_that_never_answers_leaves_a_list_of_everything_left_behind(self) -> None:
        left, notes = self._undo([], window=60, created_vm_id="vm-9")

        self.assertIn("Could not reach the Hyper-V host within 60 s", left)
        self.assertIn("the VM vm-9 the restore had created", left)
        self.assertIn(f"the file(s) {DISK} (abr-vhdx-tool may still be writing them)", left)
        self.assertIn(f"the folder(s) {FOLDER}", left)
        self.assertEqual(notes[-1], left)

    def test_a_connection_that_drops_midway_does_not_redo_the_steps_already_done(self) -> None:
        calls: list = []

        class _Dropping(_Node):
            def remove_files(self, node, paths):
                if not any(call[0] == "remove_files" for call in calls):
                    calls.append(("remove_files", list(paths)))
                    raise HyperVUnreachableError("hv.example.test", [5985], "the connection dropped", protocol="http")
                return super().remove_files(node, paths)

        left, _notes = self._undo([_Dropping(calls), _Dropping(calls)], created_vm_id="vm-9")

        self.assertEqual(left, "")
        self.assertEqual(
            [call[0] for call in calls],
            ["destroy_vm", "stop_disk_tool", "remove_files", "remove_files", "remove_empty_folders"],
        )


if __name__ == "__main__":
    unittest.main()
