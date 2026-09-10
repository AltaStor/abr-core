# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import patch

from python.srv.core import subprocess_util
from python.srv.core.subprocess_util import ProcessCancelled, run_capped


class RunCappedRegressionTests(unittest.TestCase):
    def test_plain_call_returns_stdout_and_returncode(self) -> None:
        result = run_capped(["echo", "hello"], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_check_true_raises_on_nonzero_exit(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            run_capped(["python3", "-c", "import sys; sys.exit(3)"], timeout=5, check=True)

    def test_timeout_kills_process_and_attaches_captured_tail(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            run_capped(["python3", "-u", "-c", "print('partial'); import time; time.sleep(10)"], timeout=0.5)
        self.assertIn("partial", ctx.exception.output)

    def test_output_tail_is_bounded(self) -> None:
        result = run_capped(
            ["python3", "-c", "print('x' * 5000)"],
            timeout=5,
            max_capture_bytes=100,
        )
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 200)  # tail + truncation notice
        self.assertIn("truncated", result.stdout)


class RunCappedCancellationTests(unittest.TestCase):
    def test_a_cancelled_run_stops_the_child_while_it_reports_progress(self) -> None:
        script = (
            "import time\n"
            "for pct in range(0, 101, 10):\n"
            "    print(f'{pct}% (1)')\n"
            "    time.sleep(0.3)\n"
        )
        started = time.monotonic()
        with self.assertRaises(ProcessCancelled):
            run_capped(
                ["python3", "-u", "-c", script],
                timeout=30,
                on_progress=lambda pct, idx: None,
                progress_poll_seconds=0.05,
                should_cancel=lambda: True,
            )
        self.assertLess(time.monotonic() - started, 5)

    def test_a_cancelled_run_stops_the_child_with_no_progress_reporter(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProcessCancelled):
            run_capped(
                ["python3", "-c", "import time; time.sleep(30)"],
                timeout=30,
                progress_poll_seconds=0.05,
                should_cancel=lambda: True,
            )
        self.assertLess(time.monotonic() - started, 5)

    def test_a_progress_callback_that_raises_on_a_stop_cancels_the_run(self) -> None:
        script = (
            "import time\n"
            "for pct in range(0, 101, 10):\n"
            "    print(f'{pct}% (1)')\n"
            "    time.sleep(0.3)\n"
        )

        def raise_on_progress(percent: int, block_index: int) -> None:
            raise RuntimeError("Session stop requested")

        with self.assertRaises(ProcessCancelled):
            run_capped(
                ["python3", "-u", "-c", script],
                timeout=30,
                on_progress=raise_on_progress,
                progress_poll_seconds=0.05,
                should_cancel=lambda: True,
            )

    def test_a_run_nobody_cancelled_finishes_normally(self) -> None:
        result = run_capped(
            ["echo", "hello"],
            timeout=10,
            progress_poll_seconds=0.05,
            should_cancel=lambda: False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")


class RunCappedProgressTests(unittest.TestCase):
    def test_on_progress_receives_percent_and_block_in_order(self) -> None:
        script = (
            "import time\n"
            "for i, pct in enumerate([10, 55, 100], start=1):\n"
            "    print(f'{pct}% ({i})')\n"
            "    time.sleep(0.4)\n"
        )
        updates: list[tuple[int, int]] = []
        result = run_capped(
            ["python3", "-u", "-c", script],
            timeout=10,
            on_progress=lambda pct, idx: updates.append((pct, idx)),
            progress_poll_seconds=0.05,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(updates, [(10, 1), (55, 2), (100, 3)])

    def test_on_progress_deduplicates_repeated_lines(self) -> None:
        script = "import time\nprint('50% (1)')\ntime.sleep(0.4)\nprint('50% (1)')\ntime.sleep(0.4)\nprint('100% (2)')\n"
        updates: list[tuple[int, int]] = []
        run_capped(
            ["python3", "-u", "-c", script],
            timeout=10,
            on_progress=lambda pct, idx: updates.append((pct, idx)),
            progress_poll_seconds=0.05,
        )
        self.assertEqual(updates, [(50, 1), (100, 2)])

    def test_final_stdout_capture_is_unaffected_by_progress_parsing(self) -> None:
        script = "print('20% (1)')\nprint('a marker line')\nprint('100% (2)')\n"
        result = run_capped(
            ["python3", "-c", script],
            timeout=5,
            on_progress=lambda pct, idx: None,
            progress_poll_seconds=0.05,
        )
        self.assertIn("a marker line", result.stdout)
        self.assertIn("100% (2)", result.stdout)

    def test_a_raising_progress_callback_does_not_abort_the_run(self) -> None:
        script = "print('10% (1)')\nprint('100% (2)')\n"

        def boom(_pct, _idx):
            raise RuntimeError("callback bug")

        result = run_capped(
            ["python3", "-u", "-c", script],
            timeout=5,
            on_progress=boom,
            progress_poll_seconds=0.05,
        )
        self.assertEqual(result.returncode, 0)

    def test_timeout_with_progress_callback_still_attaches_captured_tail(self) -> None:
        script = "print('10% (1)'); import time; time.sleep(10)"
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            run_capped(
                ["python3", "-u", "-c", script],
                timeout=0.5,
                on_progress=lambda pct, idx: None,
                progress_poll_seconds=0.05,
            )
        self.assertIn("10% (1)", ctx.exception.output)

    def test_bare_percent_without_block_index_is_reported(self) -> None:
        # The real abr-vmdk-tool prints "42%" with no "(block)" suffix. Requiring
        # the suffix meant its output never matched and the console bar froze at
        # each stage's base value.
        script = (
            "import sys, time\n"
            "for pct in (10, 55, 100):\n"
            "    sys.stdout.write(f'{pct}%\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.3)\n"
        )
        updates: list[tuple[int, int]] = []
        result = run_capped(
            ["python3", "-u", "-c", script],
            timeout=10,
            on_progress=lambda pct, idx: updates.append((pct, idx)),
            progress_poll_seconds=0.05,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(updates, [(10, 0), (55, 0), (100, 0)])

    def test_carriage_return_separated_updates_arrive_live(self) -> None:
        # The mover separates updates with CR and only emits a newline at the
        # very end ("20%\r41%\r...\n"). Splitting on newline alone held every
        # update back until the process exited, so the bar only moved at stage
        # boundaries. Assert they arrive WHILE the process is still running.
        script = (
            "import sys, time\n"
            "for pct in (20, 41, 62, 83):\n"
            "    sys.stdout.write(f'{pct}%\\r')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.3)\n"
            "sys.stdout.write('100%\\n')\n"
        )
        updates: list[int] = []
        result = run_capped(
            ["python3", "-u", "-c", script],
            timeout=10,
            on_progress=lambda pct, _idx: updates.append(pct),
            progress_poll_seconds=0.05,
        )
        self.assertEqual(result.returncode, 0)
        # Every step seen, in order — not one lump at the end.
        self.assertEqual(updates, [20, 41, 62, 83, 100])

    def test_partial_percent_token_is_not_reported_early(self) -> None:
        # A chunk ending mid-token ("...\r4" of "42%") must not be read as "4%".
        script = (
            "import sys, time\n"
            "sys.stdout.write('7%\\r4')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.5)\n"
            "sys.stdout.write('2%\\n')\n"
        )
        updates: list[int] = []
        run_capped(
            ["python3", "-u", "-c", script],
            timeout=10,
            on_progress=lambda pct, _idx: updates.append(pct),
            progress_poll_seconds=0.05,
        )
        self.assertEqual(updates, [7, 42])

    def test_progress_prefix_is_unbuffered_not_line_buffered(self) -> None:
        # -oL flushes on newline only, which never arrives mid-transfer for a
        # CR-separated reporter — the updates would sit in the child's buffer.
        with patch("python.srv.core.subprocess_util.shutil.which", return_value="/usr/bin/stdbuf"):
            self.assertEqual(subprocess_util._progress_prefix(), ["stdbuf", "-o0"])

    def test_progress_prefix_added_to_argv_only_when_on_progress_is_set(self) -> None:
        captured_cmds: list[list[str]] = []

        class _FakeProc:
            args = ["fake"]
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return _FakeProc()

        with patch("python.srv.core.subprocess_util.subprocess.Popen", side_effect=fake_popen):
            run_capped(["true"], timeout=5, nice=False)
            run_capped(["true"], timeout=5, nice=False, on_progress=lambda p, i: None)

        self.assertEqual(captured_cmds[0], ["true"])
        self.assertEqual(captured_cmds[1][-1], "true")
        self.assertIn("stdbuf", captured_cmds[1])

    def test_progress_prefix_degrades_gracefully_when_stdbuf_is_missing(self) -> None:
        subprocess_util._WARNED_NO_STDBUF = False
        with patch("python.srv.core.subprocess_util.shutil.which", return_value=None):
            prefix = subprocess_util._progress_prefix()
        self.assertEqual(prefix, [])


if __name__ == "__main__":
    unittest.main()
