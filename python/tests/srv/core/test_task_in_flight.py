# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from python.srv.core import task_pool


class TaskInFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = task_pool.TaskProcessPool(worker_count=1)
        self.pool._executor = task_pool.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool._executor.shutdown, wait=True)
        self.release = threading.Event()

    def _wait_gone(self, task_id: str) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not task_pool.task_in_flight(task_id):
                return True
            time.sleep(0.01)
        return False

    def _run(self, task_id: str, outcome) -> None:
        def run_task(snapshot, _seconds, _report):
            self.release.wait(5)
            return outcome()

        with patch.object(task_pool, "run_task", side_effect=run_task), patch.object(self.pool, "_finish_task"):
            self.pool._submit_claimed_task({"id": task_id, "task_type": "verify"})
            self.assertTrue(task_pool.task_in_flight(task_id))
            self.release.set()
            self.assertTrue(self._wait_gone(task_id))

    def test_a_task_is_in_flight_from_its_dispatch_until_its_worker_returns(self) -> None:
        self.assertFalse(task_pool.task_in_flight("t-1"))
        self._run("t-1", lambda: {"status": "completed"})

    def test_a_task_whose_worker_raises_leaves_the_flight_too(self) -> None:
        def boom():
            raise RuntimeError("worker died")

        self._run("t-2", boom)

    def test_a_dispatch_that_cannot_be_submitted_is_not_left_in_flight(self) -> None:
        self.pool._executor.shutdown(wait=True)
        with self.assertRaises(RuntimeError):
            self.pool._submit_claimed_task({"id": "t-3", "task_type": "verify"})

        self.assertFalse(task_pool.task_in_flight("t-3"))


if __name__ == "__main__":
    unittest.main()
