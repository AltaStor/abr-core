# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable

from .api.client import AltaStorApiClient

TERMINAL_SESSION_STATES = {"completed", "failed", "stopped", "canceled"}
SUCCESSFUL_SESSION_STATES = {"completed"}


def session_watch_succeeded(session: dict[str, Any]) -> bool:
    return str(session.get("state") or "") in SUCCESSFUL_SESSION_STATES


_BAR_WIDTH = 30
_PLAIN_PRINT_INTERVAL_SECONDS = 5.0


def _format_bar(progress: Any) -> str:
    try:
        percent = max(0, min(100, int(progress)))
    except (TypeError, ValueError):
        return "[" + "?" * _BAR_WIDTH + "]  --%"
    filled = int(_BAR_WIDTH * percent / 100)
    return f"[{'#' * filled}{'-' * (_BAR_WIDTH - filled)}] {percent:3d}%"


def _latest_message(session: dict[str, Any]) -> str:
    live_message = session.get("message")
    if live_message:
        return str(live_message)
    
    logs = session.get("logs")
    if isinstance(logs, list) and logs:
        last = logs[-1]
        if isinstance(last, dict) and last.get("message"):
            return str(last["message"])
        
    return str(session.get("state") or "")


def watch_session(
    api_client: AltaStorApiClient,
    session_id: str,
    *,
    poll_seconds: float = 1.0,
    stream: Any = None,
) -> dict[str, Any]:
    if stream is None:
        stream = sys.stderr
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    session: dict[str, Any] = {}
    last_line_length = 0
    last_plain_print = 0.0
    while True:
        session = api_client.get(f"api/v1/sessions/{session_id}")
        state = str(session.get("state") or "")
        line = f"{_format_bar(session.get('progress'))} {_latest_message(session)}"

        if is_tty:
            stream.write("\r" + line.ljust(last_line_length))
            stream.flush()
            last_line_length = len(line)
        else:
            now = time.monotonic()
            if state in TERMINAL_SESSION_STATES or now - last_plain_print >= _PLAIN_PRINT_INTERVAL_SECONDS:
                stream.write(line + "\n")
                stream.flush()
                last_plain_print = now

        if state in TERMINAL_SESSION_STATES:
            if is_tty:
                stream.write("\n")
                stream.flush()
            return session

        time.sleep(poll_seconds)


def run_with_elapsed_indicator(
    fn: Callable[[], Any],
    *,
    label: str = "Working",
    stream: Any = None,
) -> Any:
    if stream is None:
        stream = sys.stderr
    if not bool(getattr(stream, "isatty", lambda: False)()):
        return fn()

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_run, daemon=True)
    start = time.monotonic()
    worker.start()
    line_length = 0
    while not done.wait(timeout=0.2):
        line = f"{label}... {int(time.monotonic() - start)}s elapsed"
        stream.write("\r" + line.ljust(line_length))
        stream.flush()
        line_length = len(line)
    worker.join()
    stream.write("\r" + " " * line_length + "\r")
    stream.flush()

    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
