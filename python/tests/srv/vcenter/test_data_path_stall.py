# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import patch

from python.srv.core import subprocess_util
from python.srv.core.subprocess_util import DataPathStalled, run_capped
from python.srv.plugins.vcenter import vcenter_backup

MOVES_THEN_STOPS = "import sys, time\nsys.stdout.write('x' * 200000); sys.stdout.flush()\ntime.sleep(30)\n"
KEEPS_MOVING = (
    "import sys, time\n"
    "for _ in range(25):\n"
    "    sys.stdout.write('y' * 200000); sys.stdout.flush(); time.sleep(0.1)\n"
)


class StallWatchTests(unittest.TestCase):
    def test_a_mover_that_stops_moving_data_is_stopped_and_reported(self) -> None:
        started = time.monotonic()

        with self.assertRaises(DataPathStalled) as caught:
            run_capped([sys.executable, "-c", MOVES_THEN_STOPS], stall_seconds=1.5, progress_poll_seconds=0.1, nice=False)

        self.assertLess(time.monotonic() - started, 10)
        self.assertGreaterEqual(caught.exception.idle_seconds, 1.5)

    def test_waiting_is_announced_before_the_mover_is_given_up(self) -> None:
        notices: list[float] = []

        with patch.object(subprocess_util, "DATA_PATH_NOTICE_SECONDS", 0.5), self.assertRaises(DataPathStalled):
            run_capped(
                [sys.executable, "-c", MOVES_THEN_STOPS],
                stall_seconds=1.8,
                on_stall=notices.append,
                progress_poll_seconds=0.1,
                nice=False,
            )

        self.assertGreaterEqual(len(notices), 2)
        self.assertTrue(all(0.5 <= idle < 1.8 for idle in notices), notices)

    def test_a_mover_that_keeps_moving_data_runs_to_the_end(self) -> None:
        completed = run_capped([sys.executable, "-c", KEEPS_MOVING], stall_seconds=1.0, progress_poll_seconds=0.1, nice=False)

        self.assertEqual(completed.returncode, 0)

    def test_without_a_stall_limit_a_quiet_process_is_left_alone(self) -> None:
        completed = run_capped([sys.executable, "-c", "import time; time.sleep(1.5)"], nice=False)

        self.assertEqual(completed.returncode, 0)


class VCenterDataPathTests(unittest.TestCase):
    def _run(self, side_effect):
        events: list[dict] = []
        with patch.object(vcenter_backup, "run_capped", side_effect=side_effect) as run, patch.object(
            vcenter_backup, "env_int", return_value=180
        ):
            try:
                result = vcenter_backup._run_watched_disk_tool(
                    ["abr-vmdk-tool", "backup"],
                    data_host="198.51.100.52",
                    disk_label="disk 1/5",
                    datastore_path="[ds] vm/vm.vmdk",
                    progress_callback=events.append,
                    check=False,
                )
            except vcenter_backup.VCenterBackupError as exc:
                result = exc
        return run, events, result

    def test_a_lost_data_path_names_the_host_and_the_disk(self) -> None:
        _, _, error = self._run(DataPathStalled(181.0, stderr="Disk opened using transport mode 'nbdssl'"))

        self.assertIsInstance(error, vcenter_backup.VCenterBackupError)
        self.assertEqual(
            str(error),
            "Lost the connection to ESXi host 198.51.100.52 while backing up disk 1/5 ([ds] vm/vm.vmdk): no data "
            "reached the backup server for 181s, so the backup was stopped. Check the network between the backup "
            "server and the host",
        )

    def test_the_watch_reports_waiting_into_the_session(self) -> None:
        def stall_once(command, *, stall_seconds, on_stall, **kwargs):
            on_stall(30.4)
            return "done"

        run, events, result = self._run(stall_once)

        self.assertEqual(result, "done")
        self.assertEqual(run.call_args.kwargs["stall_seconds"], 180)
        self.assertEqual(events[-1]["message"], "No data from ESXi host 198.51.100.52 for 30s while backing up disk 1/5; waiting up to 180s for the connection to come back")
        self.assertNotIn("progress", events[-1])


class LostConnectionMessageTests(unittest.TestCase):
    def test_the_movers_own_connection_error_names_the_host(self) -> None:
        message = vcenter_backup._disk_failure_message(
            {"id": "repo-1", "name": "local", "type": "local", "path": "/srv/backups"},
            "Disk '[ds] vm/vm.vmdk' opened using transport mode 'nbdssl'\n"
            "Error: failed to read disk data: The server refused connection",
            needed_bytes=None,
            needed_is_disk_size=True,
            free_before=None,
            data_host="198.51.100.52",
        )

        self.assertEqual(
            message, "Lost the connection to ESXi host 198.51.100.52 (failed to read disk data: The server refused connection)"
        )

    def test_other_errors_are_not_taken_for_a_lost_connection(self) -> None:
        error = "Error: VixDiskLib_Open failed: The file is locked"

        message = vcenter_backup._disk_failure_message(
            None, error, needed_bytes=None, needed_is_disk_size=True, free_before=None, data_host="esx"
        )

        self.assertEqual(message, error)


if __name__ == "__main__":
    unittest.main()
