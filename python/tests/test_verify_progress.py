# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.core.storage_tool import (
    VERIFY_PROGRESS_POLL_SECONDS,
    StorageToolError,
    verify_disk_tool_backup,
)
from python.srv.core.subprocess_util import CompletedProcess
from python.srv.services import abr_v41


def _disk_tool_stub(*, percentages=(25, 50, 75, 100), listing_names=None, corrupt_names=()):
    names = list(listing_names) if listing_names is not None else [f"0{index}-disk.vmdk" for index in range(1, 10)]

    def _side_effect(command, **kwargs):
        subcommand = command[0] if command else ""
        if subcommand == "list":
            body = "Backups in 'repo':\n" + "".join(f"  {name}\n" for name in names)
            return CompletedProcess(args=command, returncode=0, stdout=body, stderr="")
        if subcommand == "verify":
            name = command[command.index("--name") + 1]
            on_progress = kwargs.get("on_progress")
            for percent in percentages:
                if on_progress is not None:
                    on_progress(percent, 0)
            if name in corrupt_names:
                raise StorageToolError("backup is corrupt: 1 of 4 data ranges failed verification")
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    return _side_effect


class _VerifyFixture(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        self._repo_root = tempfile.mkdtemp(prefix="abr-verify-progress-")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        shutil.rmtree(self._repo_root, ignore_errors=True)
        BACKUPS.clear()
        TASKS.clear()
        REPOSITORIES.clear()

    def _chain_with_disks(self, disk_count: int, *, execution_types=("ActiveFull",)) -> str:
        repository = abr_v41.create_repository_record(
            {"name": "VerifyRepo", "type": "local", "path": str(Path(self._repo_root))}
        )
        chain_id = ""
        parent_point_id = None
        for order, execution_type in enumerate(execution_types, start=1):
            point = abr_v41.normalize_backup_point(
                {
                    "id": f"point-{order}",
                    "job_id": "job-1",
                    "vm_id": "vm-1",
                    "vm_name": "VM-1",
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": repository["type"],
                    "execution_type": execution_type,
                    "parent_point_id": parent_point_id,
                    "created_at": f"2026-06-2{order}T12:00:00+00:00",
                    "artifact": {
                        "disks": [
                            {
                                "disk_key": f"disk-{index}",
                                "storage_mode": "local",
                                "abr_path": "vm-1/task-1",
                                "abr_backup_name": f"0{index}-disk.vmdk",
                            }
                            for index in range(1, disk_count + 1)
                        ]
                    },
                }
            )
            abr_v41.persist_backup(point)
            parent_point_id = str(point["id"])
            chain_id = str(point["chain_id"])
        return chain_id


class VerifyChainProgressTests(_VerifyFixture):
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_the_movers_percentages_reach_the_progress_callback(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub(percentages=(20, 40, 60, 80, 100))
        chain_id = self._chain_with_disks(1)
        updates: list[dict] = []

        report = abr_v41.verify_backup_chain(chain_id, progress_callback=updates.append)

        self.assertEqual(report["result"], "valid")
        percentages = [update["progress"] for update in updates]
        self.assertEqual(percentages[0], 0)
        self.assertEqual(percentages[-1], 100)
        self.assertTrue([value for value in percentages if 0 < value < 100], percentages)
        self.assertEqual(percentages, sorted(percentages))

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_each_artifact_owns_an_equal_slice_of_the_bar(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub(percentages=(50, 100))
        chain_id = self._chain_with_disks(4)
        updates: list[dict] = []

        abr_v41.verify_backup_chain(chain_id, progress_callback=updates.append)

        percentages = [update["progress"] for update in updates]
        self.assertEqual(percentages, sorted(percentages))
        self.assertLessEqual(max(value for value in percentages[:-1]), 99)
        self.assertEqual(percentages[-1], 100)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_the_progress_message_names_the_percentage(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub(percentages=(50, 100))
        chain_id = self._chain_with_disks(1)
        updates: list[dict] = []

        abr_v41.verify_backup_chain(chain_id, progress_callback=updates.append)

        middle = [update["message"] for update in updates if 0 < update["progress"] < 100]
        self.assertTrue(any("50%" in message for message in middle), middle)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_without_a_callback_still_returns_the_same_report(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub()
        chain_id = self._chain_with_disks(2)

        report = abr_v41.verify_backup_chain(chain_id)

        self.assertEqual(report["result"], "valid")
        self.assertEqual(report["checked_artifacts"], 2)
        self.assertEqual(report["errors"], [])
        self.assertNotIn("canceled", report)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_a_stop_drops_the_artifacts_that_have_not_started(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub(percentages=(100,))
        chain_id = self._chain_with_disks(8)

        report = abr_v41.verify_backup_chain(chain_id, should_cancel=lambda: True)

        self.assertEqual(report["result"], "canceled")
        self.assertTrue(report["canceled"])
        self.assertLess(report["checked_artifacts"], 8)


class VerifySessionTests(_VerifyFixture):
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_a_waiting_call_returns_the_report_and_a_finished_session(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub()
        chain_id = self._chain_with_disks(2)

        payload = abr_v41.verify_backup_chain_request(chain_id, wait=True)

        self.assertEqual(payload["result"], "valid")
        session = abr_v41.get_session(payload["session_id"])
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["type"], "verify")
        self.assertEqual(session["progress"], 100)
        self.assertEqual(session["verify"]["result"], "valid")
        self.assertEqual(session["vm_name"], "VM-1")
        self.assertIsNone(session["job"])
        self.assertIsNone(session["resolved_backup_type"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_a_chain_of_several_backup_types_is_verified_end_to_end(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub()
        chain_id = self._chain_with_disks(1, execution_types=("ActiveFull", "Incremental"))

        payload = abr_v41.verify_backup_chain_request(chain_id, wait=True)
        session = abr_v41.get_session(payload["session_id"])

        self.assertEqual(payload["result"], "valid")
        self.assertEqual(session["verify"]["checked_restore_points"], 2)
        self.assertEqual(session["verify"]["checked_artifacts"], 2)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_the_verify_session_never_counts_as_work_of_the_job_it_names(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub()
        chain_id = self._chain_with_disks(1)

        payload = abr_v41.verify_backup_chain_request(chain_id, wait=True)
        task = TASKS[payload["session_id"]]

        self.assertIsNone(task["job_id"])
        self.assertIsNone(abr_v41.get_session(payload["session_id"])["job"])
        self.assertEqual(abr_v41._active_sessions_for_job("job-1"), [])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_a_non_waiting_call_hands_back_a_session_to_follow(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub()
        chain_id = self._chain_with_disks(2)

        started = abr_v41.verify_backup_chain_request(chain_id, wait=False)

        self.assertEqual(started["result"], "running")
        session_id = started["session_id"]
        session = self._wait_for_session_state(session_id, {"completed", "failed", "stopped"})
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["progress"], 100)
        self.assertEqual(session["verify"]["checked_artifacts"], 2)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_a_corrupt_artifact_fails_the_session_and_names_the_damage(self, run_disk_tool_mock) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub(corrupt_names={"01-disk.vmdk"})
        chain_id = self._chain_with_disks(2)

        payload = abr_v41.verify_backup_chain_request(chain_id, wait=True)

        self.assertEqual(payload["result"], "invalid")
        session = abr_v41.get_session(payload["session_id"])
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["verify"]["result"], "invalid")
        self.assertIn("corrupt", session["verify"]["errors"][0]["message"])
        self.assertTrue(session["errors"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_the_session_is_listed_while_it_runs_and_never_queued(self, run_disk_tool_mock) -> None:
        release = threading.Event()
        seen_states: list[str] = []

        def _side_effect(command, **kwargs):
            if command and command[0] == "verify":
                on_progress = kwargs.get("on_progress")
                if on_progress is not None:
                    on_progress(50, 0)
                release.wait(timeout=10)
            listing = "Backups in 'repo':\n" + "".join(f"  0{index}-disk.vmdk\n" for index in range(1, 10))
            return CompletedProcess(args=command, returncode=0, stdout=listing, stderr="")

        run_disk_tool_mock.side_effect = _side_effect
        chain_id = self._chain_with_disks(1)

        started = abr_v41.verify_backup_chain_request(chain_id, wait=False)
        session_id = started["session_id"]
        try:
            self._wait_for(lambda: abr_v41.get_session(session_id)["progress"] > 0)
            seen_states.append(abr_v41.get_session(session_id)["state"])
            listed = abr_v41.list_sessions_page()
            self.assertIn(session_id, [item["id"] for item in listed["items"]])
        finally:
            release.set()

        self.assertEqual(seen_states, ["running"])
        self._wait_for_session_state(session_id, {"completed", "failed", "stopped"})

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_stopping_the_session_stops_the_verify(self, run_disk_tool_mock) -> None:
        release = threading.Event()
        started_lock = threading.Lock()
        started_names: list[str] = []

        def _side_effect(command, **kwargs):
            if command and command[0] == "verify":
                with started_lock:
                    started_names.append(command[command.index("--name") + 1])
                    first_batch = len(started_names) <= 3
                on_progress = kwargs.get("on_progress")
                if on_progress is not None:
                    on_progress(50, 0)
                if first_batch:
                    release.wait(timeout=10)
                else:
                    time.sleep(0.5)
            listing = "Backups in 'repo':\n" + "".join(f"  0{index}-disk.vmdk\n" for index in range(1, 10))
            return CompletedProcess(args=command, returncode=0, stdout=listing, stderr="")

        run_disk_tool_mock.side_effect = _side_effect
        chain_id = self._chain_with_disks(8)

        started = abr_v41.verify_backup_chain_request(chain_id, wait=False)
        session_id = started["session_id"]
        self._wait_for(lambda: abr_v41.get_session(session_id)["progress"] > 0)
        stopped = abr_v41.stop_session(session_id)
        self.assertEqual(stopped["state"], "stopping")
        release.set()

        session = self._wait_for_session_state(session_id, {"completed", "failed", "stopped"})
        self.assertEqual(session["state"], "stopped")
        self.assertEqual(session["verify"]["result"], "canceled")
        self.assertLess(session["verify"]["checked_artifacts"], 8)
        self.assertLess(len(started_names), 8)

    def test_a_missing_chain_is_a_404_before_any_session_is_created(self) -> None:
        with self.assertRaises(abr_v41.ApiError) as ctx:
            abr_v41.verify_backup_chain_request("backup::nope", wait=False)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(TASKS, {})

    def _wait_for(self, predicate, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition was never met")

    def _wait_for_session_state(self, session_id: str, states: set[str], timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session = abr_v41.get_session(session_id)
            if session["state"] in states:
                return session
            time.sleep(0.02)
        raise AssertionError(f"session never reached {states}")


class StorageToolVerifyProgressTests(unittest.TestCase):
    @patch("python.srv.core.storage_tool.run_capped")
    @patch("python.srv.core.storage_tool.default_disk_tool_path")
    def test_verify_forwards_the_progress_callback_to_the_subprocess(
        self, tool_path_mock, run_capped_mock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sh") as launcher:
            tool_path_mock.return_value = Path(launcher.name)
            run_capped_mock.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            reported: list[tuple[int, int]] = []

            verify_disk_tool_backup(
                {"type": "local", "path": "/repo"},
                "vm-1/task-1",
                "01-disk.vmdk",
                on_progress=lambda percent, block: reported.append((percent, block)),
            )

        self.assertIn("on_progress", run_capped_mock.call_args.kwargs)
        run_capped_mock.call_args.kwargs["on_progress"](42, 0)
        self.assertEqual(reported, [(42, 0)])
        self.assertEqual(
            run_capped_mock.call_args.kwargs["progress_poll_seconds"],
            VERIFY_PROGRESS_POLL_SECONDS,
        )
        self.assertLess(VERIFY_PROGRESS_POLL_SECONDS, 1.0)

    @patch("python.srv.core.storage_tool.run_capped")
    @patch("python.srv.core.storage_tool.default_disk_tool_path")
    def test_a_call_without_progress_keeps_the_plain_wait_path(
        self, tool_path_mock, run_capped_mock
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sh") as launcher:
            tool_path_mock.return_value = Path(launcher.name)
            run_capped_mock.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

            verify_disk_tool_backup({"type": "local", "path": "/repo"}, "vm-1/task-1", "01-disk.vmdk")

        self.assertNotIn("on_progress", run_capped_mock.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
