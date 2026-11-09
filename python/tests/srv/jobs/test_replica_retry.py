# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import requests
from pypsrp.exceptions import WinRMTransportError
from pyVmomi import vim

from python.srv.core.mongo_db_wrapper import TASKS
from python.srv.core.subprocess_util import ProcessCancelled
from python.srv.core.task_executor import TaskStopRequested
from python.srv.plugins.hyperv.hyperv import HyperVUnreachableError
from python.srv.services.internal.replication import replica_update
from python.srv.services.internal.replication.replica_update import run_replication
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
    ReplicationValidationError,
)
from python.tests.srv.jobs import test_replica_rebuild
from python.tests.srv.jobs.test_replica_update import (
    JOB_ID,
    OUR_SNAPSHOT,
    SESSION_ID,
    FakeNode,
    _UpdateCase,
)

UPDATE = "python.srv.services.internal.replication.replica_update"


def _caused_by(outer: BaseException, inner: BaseException) -> BaseException:
    outer.__cause__ = inner
    return outer


class TemporaryErrorTests(unittest.TestCase):
    def test_errors_a_retry_can_get_past(self) -> None:
        cases = {
            "connection reset": ConnectionResetError(104, "Connection reset by peer"),
            "timeout": TimeoutError("timed out"),
            "requests connection": requests.ConnectionError("HTTPSConnectionPool: Max retries exceeded"),
            "requests timeout": requests.Timeout("read timed out"),
            "WinRM transport": WinRMTransportError("http", 503, "Service Unavailable"),
            "Hyper-V unreachable": HyperVUnreachableError("hv", [], "no node answered"),
            "vSphere host connect": vim.fault.HostConnectFault(msg="cannot contact the host"),
            "target cannot be reached": ReplicationError("The target environment vc-dr cannot be reached: [Errno 113] No route to host"),
            "VMware tool ended": RuntimeError("Updating disk [ds] r/01.vmdk failed: the data mover ended unexpectedly: abr-vmdk-tool was terminated by signal 9"),
            "Hyper-V tool ended": RuntimeError("abr-vhdx-tool on hv-01 ended unexpectedly (exit code -1073741510) while restoring disk 1"),
            "NBD network": RuntimeError("Restore failed: NBD_ERR_NETWORK_CONNECT"),
            "S3 endpoint": RuntimeError("Could not connect to the endpoint URL: https://s3.example.test"),
            "S3 slow down": RuntimeError("SlowDown: Please reduce your request rate"),
            "wrapped network error": _caused_by(RuntimeError("Restore of disk 1 failed"), ConnectionAbortedError("aborted")),
        }
        for name, exc in cases.items():
            with self.subTest(name):
                self.assertTrue(replica_update.is_temporary(exc))

    def test_errors_a_retry_cannot_fix(self) -> None:
        cases = {
            "replica changed": ReplicationValidationError("Replica was changed: et-test is powered on. Create a new Replication Job"),
            "validation that mentions a timeout": ReplicationValidationError("the source point timed out of retention"),
            "no point": ReplicationValidationError("Source backup job x has no restore point of vm that can be replicated"),
            "stop": TaskStopRequested("Session stop requested"),
            "cancel of the tool": ProcessCancelled("cancellation requested"),
            "stop wrapping a network error": _caused_by(TaskStopRequested("Session stop requested"), ConnectionResetError()),
            "busy replica": ReplicationError("et-test is still busy with RemoveSnapshot_Task on the target after 1800s"),
            "tool reported a failure": RuntimeError("Updating disk failed: Error: the key does not decrypt backup 'disk.vmdk'"),
            "no space": RuntimeError("Restore failed: no space left on the datastore"),
        }
        for name, exc in cases.items():
            with self.subTest(name):
                self.assertFalse(replica_update.is_temporary(exc))


class RetryTests(_UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        self.waits: list[int] = []
        for target, value in (
            (f"{UPDATE}.retry_settings", lambda: (3, 10)),
            (f"{UPDATE}._wait_before_retry", lambda _session, minutes, _callback: self.waits.append(minutes)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _connect_failing(self, times: int):
        calls = {"n": 0}

        def connect(_inventory):
            calls["n"] += 1
            if calls["n"] <= times:
                raise ReplicationError("The target environment vc-dr cannot be reached: [Errno 113] No route to host")
            return FakeNode()

        return patch(f"{UPDATE}._connect", connect), calls

    def _messages(self) -> list[str]:
        return [str(update.get("message")) for update in self.updates if update.get("message")]

    def test_a_target_that_answers_again_is_updated_on_the_next_attempt(self) -> None:
        connect, calls = self._connect_failing(1)

        with connect:
            result = self._run()

        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.waits, [10])
        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["message"].endswith("(attempt 2/4)"))
        retry = next(update for update in self.updates if "Attempt 1/4 failed" in str(update.get("message")))
        self.assertEqual(retry["log_level"], "warn")
        self.assertIn("cannot be reached", retry["message"])
        self.assertIn("Retrying in 10 minute(s)", retry["message"])

    def test_a_target_that_stays_unreachable_fails_after_the_last_attempt(self) -> None:
        connect, calls = self._connect_failing(10)

        with connect, self.assertRaisesRegex(ReplicationError, "cannot be reached"):
            self._run()

        self.assertEqual(calls["n"], 4)
        self.assertEqual(self.waits, [10, 10, 10])
        self.assertEqual([m for m in self._messages() if m.startswith("Attempt")][-1][:13], "Attempt 3/4 f")

    def test_a_check_that_fails_is_never_tried_again(self) -> None:
        self.platform.state["powered_on"] = True

        with self.assertRaisesRegex(ReplicationValidationError, "Replica was changed"):
            self._run()

        self.assertEqual(self.waits, [])
        self.assertFalse(any(m.startswith("Attempt") for m in self._messages()))

    def test_a_tool_that_ended_unexpectedly_is_reverted_and_written_again(self) -> None:
        failures = [RuntimeError("Updating disk failed: the data mover ended unexpectedly: abr-vmdk-tool was terminated by signal 9")]
        original = self.platform.write_increments

        def write(*args, **kwargs):
            if failures:
                self.platform.calls.append(("write", args[2]["path"], [layer["point_id"] for layer in args[3]]))
                raise failures.pop()
            return original(*args, **kwargs)

        self.platform.write_increments = write

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(self.waits, [10])
        self.assertEqual(self.platform.calls[:3], [("snapshot", OUR_SNAPSHOT), ("write", "[ds] r/01-000001.vmdk", ["rp-2", "rp-3"]), ("revert", OUR_SNAPSHOT)])
        self.assertEqual(self.platform.calls[-1], ("remove", OUR_SNAPSHOT))
        self.assertEqual(self._replica()["point_id"], "rp-3")

    def test_no_retries_when_they_are_turned_off(self) -> None:
        connect, calls = self._connect_failing(1)

        with connect, patch(f"{UPDATE}.retry_settings", lambda: (0, 10)), self.assertRaisesRegex(ReplicationError, "cannot be reached"):
            self._run()

        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.waits, [])


class RetrySettingsTests(unittest.TestCase):
    def test_zero_turns_the_retries_off_and_retries_at_once(self) -> None:
        with patch.dict("os.environ", {"ALTASTOR_REPLICA_RETRY_COUNT": "0", "ALTASTOR_REPLICA_RETRY_INTERVAL_MINUTES": "0"}):
            self.assertEqual(replica_update.retry_settings(), (0, 0))

    def test_the_defaults_are_three_tries_ten_minutes_apart(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            for name in ("ALTASTOR_REPLICA_RETRY_COUNT", "ALTASTOR_REPLICA_RETRY_INTERVAL_MINUTES"):
                os.environ.pop(name, None)
            self.assertEqual(replica_update.retry_settings(), (3, 10))

    def test_a_negative_value_falls_back_to_the_default(self) -> None:
        with patch.dict("os.environ", {"ALTASTOR_REPLICA_RETRY_COUNT": "-1", "ALTASTOR_REPLICA_RETRY_INTERVAL_MINUTES": "5"}):
            self.assertEqual(replica_update.retry_settings(), (3, 5))


class WaitBeforeRetryTests(unittest.TestCase):
    def test_a_stop_while_waiting_ends_the_session_as_stopped(self) -> None:
        with patch("python.srv.core.task_executor.cancel_requested", return_value=True), self.assertRaises(TaskStopRequested):
            replica_update._wait_before_retry("s-1", 10, None)

    def test_the_wait_keeps_the_session_alive(self) -> None:
        beats: list[dict] = []
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        with (
            patch("python.srv.core.task_executor.cancel_requested", return_value=False),
            patch(f"{UPDATE}.time.monotonic", monotonic),
            patch(f"{UPDATE}.time.sleep", sleep),
        ):
            replica_update._wait_before_retry("s-1", 3, beats.append)

        self.assertGreaterEqual(clock["now"], 180)
        self.assertEqual(len(beats), 2)
        self.assertTrue(all(beat.get("log_entry") is False for beat in beats))


if __name__ == "__main__":
    unittest.main()


class RebuildRetryTests(test_replica_rebuild._RebuildCase):
    def setUp(self) -> None:
        super().setUp()
        for target, value in (
            (f"{UPDATE}.retry_settings", lambda: (3, 0)),
            (f"{UPDATE}._wait_before_retry", lambda _session, _minutes, _callback: None),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self) -> dict:
        TASKS[SESSION_ID] = {"id": SESSION_ID, "job_id": JOB_ID, "task_type": "replication", "status": "running", "created_at": "2026-11-09T00:00:00", "result": {}}

        def report(update: dict) -> None:
            self.updates.append(update)
            TASKS[SESSION_ID]["result"].update({key: value for key, value in update.items() if key != "log_entry"})

        return run_replication({"id": SESSION_ID, "job_id": JOB_ID}, progress_callback=report)

    def test_a_drop_after_the_switch_is_finished_by_the_next_attempt_from_its_own_plan(self) -> None:
        drops = [ConnectionResetError(104, "Connection reset by peer")]
        original = self.platform.delete_disk_files

        def delete(node, host, paths):
            if drops:
                raise drops.pop()
            return original(node, host, paths)

        self.platform.delete_disk_files = delete

        result = self._run()

        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(self._calls("swap")), 1)
        self.assertIn(("delete files", ["[ds] r/01.vmdk", "[ds] r/02.vmdk"]), self.platform.calls)
        self.assertEqual(self._replica()["point_id"], "rp-3")
        self.assertEqual(TASKS[SESSION_ID]["result"]["swap_plan"], {})
        self.assertEqual(self.platform.staged, {})
