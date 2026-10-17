# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock

from python.cli.progress_watch import run_with_elapsed_indicator, session_watch_succeeded, watch_session


class _FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class WatchSessionTests(unittest.TestCase):
    def test_polls_until_terminal_state_and_returns_final_payload(self) -> None:
        responses = [
            {"state": "running", "progress": 10, "logs": [{"message": "Creating backup snapshot"}]},
            {"state": "running", "progress": 55, "logs": [{"message": "Backing up disk 1/1: 55%"}]},
            {"state": "completed", "progress": 100, "logs": [{"message": "Task completed"}]},
        ]
        api_client = MagicMock()
        api_client.get.side_effect = responses

        stream = _FakeStream(tty=False)
        result = watch_session(api_client, "sess-1", poll_seconds=0, stream=stream)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(api_client.get.call_count, 3)
        api_client.get.assert_called_with("api/v1/sessions/sess-1")

    def test_stops_on_every_terminal_state(self) -> None:
        for terminal_state in ("completed", "failed", "stopped", "canceled"):
            api_client = MagicMock()
            api_client.get.return_value = {"state": terminal_state, "progress": None, "logs": []}
            result = watch_session(api_client, "sess-1", poll_seconds=0, stream=_FakeStream(tty=False))
            self.assertEqual(result["state"], terminal_state)
            self.assertEqual(api_client.get.call_count, 1)

    def test_tty_stream_redraws_a_single_line(self) -> None:
        responses = [
            {"state": "running", "progress": 20, "logs": [{"message": "step one"}]},
            {"state": "completed", "progress": 100, "logs": [{"message": "done"}]},
        ]
        api_client = MagicMock()
        api_client.get.side_effect = responses
        stream = _FakeStream(tty=True)

        watch_session(api_client, "sess-1", poll_seconds=0, stream=stream)

        output = stream.getvalue()
        self.assertGreaterEqual(output.count("\r"), 2)
        self.assertIn(" 20%", output)
        self.assertIn("100%", output)
        self.assertIn("step one", output)
        self.assertIn("done", output)

    def test_non_tty_stream_prints_newline_separated_updates_on_terminal_state(self) -> None:
        api_client = MagicMock()
        api_client.get.return_value = {"state": "failed", "progress": 40, "logs": [{"message": "boom"}]}
        stream = _FakeStream(tty=False)

        watch_session(api_client, "sess-1", poll_seconds=0, stream=stream)

        self.assertIn(" 40%", stream.getvalue())
        self.assertIn("boom", stream.getvalue())
        self.assertTrue(stream.getvalue().endswith("\n"))

    def test_live_message_field_is_shown_instead_of_the_stale_last_log_line(self) -> None:
        api_client = MagicMock()
        api_client.get.side_effect = [
            {
                "state": "running",
                "progress": 61,
                "message": "Restoring disk 1/1: 61%",
                "logs": [{"message": "Preparing VM restore for demo-vm"}],
            },
            {"state": "completed", "progress": 100, "logs": []},
        ]
        stream = _FakeStream(tty=True)

        watch_session(api_client, "sess-1", poll_seconds=0, stream=stream)
        self.assertIn("Restoring disk 1/1: 61%", stream.getvalue())

    def test_missing_or_non_numeric_progress_renders_placeholder_not_a_crash(self) -> None:
        api_client = MagicMock()
        api_client.get.return_value = {"state": "completed", "progress": None, "logs": []}
        result = watch_session(api_client, "sess-1", poll_seconds=0, stream=_FakeStream(tty=False))
        self.assertEqual(result["state"], "completed")


class SessionWatchSucceededTests(unittest.TestCase):
    def test_completed_is_the_only_successful_state(self) -> None:
        self.assertTrue(session_watch_succeeded({"state": "completed"}))

    def test_every_other_terminal_state_is_unsuccessful(self) -> None:
        for state in ("failed", "stopped", "canceled"):
            self.assertFalse(session_watch_succeeded({"state": state}), state)

    def test_missing_state_is_unsuccessful(self) -> None:
        self.assertFalse(session_watch_succeeded({}))


class RunWithElapsedIndicatorTests(unittest.TestCase):
    def test_non_tty_stream_just_calls_fn_directly(self) -> None:
        calls: list[str] = []

        def fn():
            calls.append("ran")
            return "value"

        result = run_with_elapsed_indicator(fn, stream=_FakeStream(tty=False))
        self.assertEqual(result, "value")
        self.assertEqual(calls, ["ran"])

    def test_tty_stream_still_returns_the_function_result(self) -> None:
        result = run_with_elapsed_indicator(lambda: 42, label="Working", stream=_FakeStream(tty=True))
        self.assertEqual(result, 42)

    def test_exception_from_fn_propagates_unchanged(self) -> None:
        def boom():
            raise ValueError("kaboom")

        with self.assertRaises(ValueError):
            run_with_elapsed_indicator(boom, stream=_FakeStream(tty=True))

    def test_tty_stream_writes_an_elapsed_time_indicator(self) -> None:
        import time

        stream = _FakeStream(tty=True)
        run_with_elapsed_indicator(lambda: time.sleep(0.3), stream=stream)
        self.assertIn("elapsed", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
