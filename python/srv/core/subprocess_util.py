# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Resource-bounded subprocess execution for the data mover.

The orchestrator spawns ``abr-vmdk-tool`` once per disk. Historically these were
run with ``subprocess.run(..., capture_output=True)``, which buffers the entire
stdout/stderr of the mover in RAM. A chatty mover on a large disk can emit
hundreds of MB to GBs of progress text, and with several backups in flight that
output is the dominant memory sink that drives the host into OOM (see the
resource-exhaustion incident: two concurrent backups taking the VM down).

``run_capped`` is a drop-in replacement for the ``subprocess.run`` calls used by
the backup/restore services. It:

* streams stdout/stderr to temp files (RAM stays flat regardless of volume),
* returns only a bounded tail of each stream as ``.stdout`` / ``.stderr`` so the
  existing diagnostics/logging code keeps working,
* preserves ``TimeoutExpired`` semantics (with the captured tail attached),
* optionally wraps the command with ``nice``/``ionice`` so a backup cannot
  starve the co-located API + MongoDB for CPU/IO,
* optionally reports the tool's own "NN% (block)" progress lines live via an
  ``on_progress`` callback, instead of callers only ever seeing the
  before/after milestones around the whole subprocess call.

Limits are configurable via environment variables (defaults in defaults.env):

* ``BACKUP_TOOL_MAX_CAPTURE_BYTES`` — max bytes kept per stream.
* ``BACKUP_TOOL_NICE``             — ``nice`` increment, "" disables.
* ``BACKUP_TOOL_IONICE_CLASS``     — ``ionice`` class, "" disables.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .server_defaults import env_int

logger = logging.getLogger(__name__)

_TRUNCATION_NOTICE = "...[output truncated: showing last {kept} of {total} bytes]...\n"
# abr-vmdk-tool reports a bare percentage ("42%"); other movers append a block
# index ("42% (1234)"). The index is therefore OPTIONAL -- requiring it meant
# the real tool's output never matched, on_progress never fired, and the console
# progress bar sat at each stage's base value until the stage ended.
_PROGRESS_LINE_PATTERN = re.compile(rb"(\d{1,3})%(?:\s*\((\d+)\))?")


DATA_PATH_ACTIVITY_BYTES = 64 * 1024
DATA_PATH_NOTICE_SECONDS = 30


class DataPathStalled(RuntimeError):
    def __init__(self, idle_seconds: float, stdout: str | bytes = "", stderr: str | bytes = "") -> None:
        super().__init__(f"no data moved for {int(idle_seconds)}s")
        self.idle_seconds = idle_seconds
        self.stdout = stdout
        self.stderr = stderr


def _process_io_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/io", encoding="ascii") as handle:
            counters = dict(line.split(":", 1) for line in handle if ":" in line)
        return int(counters["rchar"]) + int(counters["wchar"])
    except (OSError, KeyError, ValueError):
        return None


class _StallWatch:
    def __init__(
        self,
        pid: int,
        stall_seconds: float,
        on_stall: Callable[[float], None] | None,
    ) -> None:
        self.pid = pid
        self.stall_seconds = stall_seconds
        self.on_stall = on_stall
        self.notice_seconds = DATA_PATH_NOTICE_SECONDS
        self.mark = _process_io_bytes(pid)
        self.active_at = time.monotonic()
        self.notices = 0

    def check(self) -> None:
        now = time.monotonic()
        counter = _process_io_bytes(self.pid)
        if counter is None or self.mark is None or counter - self.mark >= DATA_PATH_ACTIVITY_BYTES:
            self.mark = counter
            self.active_at = now
            self.notices = 0
            return
        idle = now - self.active_at
        if idle >= self.stall_seconds:
            raise DataPathStalled(idle)
        if self.on_stall is not None and idle >= self.notice_seconds * (self.notices + 1):
            self.notices += 1
            try:
                self.on_stall(idle)
            except Exception:
                logger.warning("on_stall callback raised; ignoring.", exc_info=True)


class ProcessCancelled(RuntimeError):
    pass

_DEFAULT_PROGRESS_POLL_SECONDS = 1.0


def _max_capture_bytes() -> int:
    return env_int("BACKUP_TOOL_MAX_CAPTURE_BYTES")


def _nice_prefix() -> list[str]:
    """Build an optional ``nice``/``ionice`` prefix, skipping unavailable tools."""
    prefix: list[str] = []

    nice_raw = os.environ["BACKUP_TOOL_NICE"].strip()
    if nice_raw and shutil.which("nice"):
        try:
            int(nice_raw)
            prefix += ["nice", "-n", nice_raw]
        except ValueError:
            logger.warning("Invalid BACKUP_TOOL_NICE=%r; skipping nice.", nice_raw)

    ionice_raw = os.environ["BACKUP_TOOL_IONICE_CLASS"].strip()
    if ionice_raw and shutil.which("ionice"):
        try:
            int(ionice_raw)
            prefix += ["ionice", "-c", ionice_raw]
        except ValueError:
            logger.warning("Invalid BACKUP_TOOL_IONICE_CLASS=%r; skipping ionice.", ionice_raw)

    return prefix


_WARNED_NO_STDBUF = False


def _progress_prefix() -> list[str]:
    global _WARNED_NO_STDBUF
    if shutil.which("stdbuf"):
        # -o0 (unbuffered), NOT -oL: the mover separates progress updates with a
        # carriage return and only emits a newline when the whole transfer ends,
        # and line buffering flushes on newline only -- so -oL held every update
        # in the child's buffer until the run was over.
        return ["stdbuf", "-o0"]
    if not _WARNED_NO_STDBUF:
        logger.warning(
            "stdbuf not found; on_progress updates will only arrive once the "
            "tool's own stdout buffer flushes (typically at exit), not live."
        )
        _WARNED_NO_STDBUF = True
    return []


def _read_tail(handle, max_bytes: int, text: bool):
    handle.flush()
    handle.seek(0, os.SEEK_END)
    total = handle.tell()
    start = max(0, total - max_bytes)
    handle.seek(start)
    data = handle.read()
    truncated = start > 0
    if text:
        out = data.decode("utf-8", errors="replace")
        if truncated:
            out = _TRUNCATION_NOTICE.format(kept=len(data), total=total) + out
        return out
    if truncated:
        return _TRUNCATION_NOTICE.format(kept=len(data), total=total).encode() + data
    return data


def _wait_with_progress(
    proc: subprocess.Popen,
    out_f,
    on_progress: Callable[[int, int], None],
    poll_seconds: float,
    timeout: float | None,
    should_cancel: Callable[[], bool] | None = None,
    stall_watch: _StallWatch | None = None,
) -> None:
    deadline = time.monotonic() + timeout if timeout is not None else None
    pos = 0
    last_reported: tuple[int, int] | None = None
    while True:
        finished = proc.poll() is not None
        out_f.seek(pos)
        chunk = out_f.read()
        if chunk:
            if finished:
                usable = chunk
            else:
                # Progress updates are CR-separated ("20%\r41%\r..."), with a
                # newline only at the very end, so splitting on newline alone
                # held them all back to the end of the run. Cut at the last
                # separator of EITHER kind so a half-written "4" of "42%" is
                # never read as "4%".
                cut = max(chunk.rfind(b"\n"), chunk.rfind(b"\r"))
                usable = chunk[: cut + 1] if cut != -1 else b""
            if usable:
                pos += len(usable)
                match = None
                for match in _PROGRESS_LINE_PATTERN.finditer(usable):
                    pass
                if match is not None:
                    percent = int(match.group(1))
                    block_index = int(match.group(2)) if match.group(2) is not None else 0
                    if (percent, block_index) != last_reported:
                        last_reported = (percent, block_index)
                        try:
                            on_progress(percent, block_index)
                        except Exception:
                            if should_cancel is not None and should_cancel():
                                raise ProcessCancelled("cancellation requested") from None
                            logger.warning("on_progress callback raised; ignoring.", exc_info=True)
        if finished:
            return
        if should_cancel is not None and should_cancel():
            raise ProcessCancelled("cancellation requested")
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if stall_watch is not None:
            stall_watch.check()
        time.sleep(poll_seconds)


def _wait_for_cancel(
    proc: subprocess.Popen,
    poll_seconds: float,
    timeout: float | None,
    should_cancel: Callable[[], bool] | None,
    stall_watch: _StallWatch | None = None,
) -> None:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while proc.poll() is None:
        if should_cancel is not None and should_cancel():
            raise ProcessCancelled("cancellation requested")
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if stall_watch is not None:
            stall_watch.check()
        time.sleep(poll_seconds)


@dataclass
class CompletedProcess:
    """Mirror of ``subprocess.CompletedProcess`` for the fields callers use."""

    args: Sequence[str]
    returncode: int
    stdout: str | bytes
    stderr: str | bytes


def _send_stdin(proc: subprocess.Popen, text: str) -> None:
    try:
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass


def run_capped(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = False,
    text: bool = True,
    nice: bool = True,
    max_capture_bytes: int | None = None,
    # Accepted for drop-in compatibility with subprocess.run; capture is always on.
    capture_output: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    progress_poll_seconds: float = _DEFAULT_PROGRESS_POLL_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
    stall_seconds: float | None = None,
    on_stall: Callable[[float], None] | None = None,
    stdin_text: str | None = None,
    **_ignored,
) -> CompletedProcess:
    """Run ``command`` capturing only a bounded tail of stdout/stderr.

    Drop-in for ``subprocess.run(cmd, check=False, capture_output=True,
    text=True, timeout=...)`` but with RAM bounded to ``max_capture_bytes`` per
    stream. Raises ``subprocess.TimeoutExpired`` (with captured tail) on timeout
    and ``subprocess.CalledProcessError`` when ``check`` and a non-zero exit.

    If ``on_progress`` is given, it's called with ``(percent, block_index)``
    each time the tool prints a new "NN% (block)" line, so callers can surface
    the tool's own progress instead of just the two-line
    started/finished milestones ``proc.wait()`` alone allows for.
    """
    cap = max_capture_bytes if max_capture_bytes is not None else _max_capture_bytes()
    cmd = list(command)
    full_cmd = (_nice_prefix() + cmd) if nice else cmd
    if on_progress is not None:
        full_cmd = _progress_prefix() + full_cmd

    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        if stdin_text is None:
            proc = subprocess.Popen(full_cmd, stdout=out_f, stderr=err_f)
        else:
            proc = subprocess.Popen(full_cmd, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f)
            _send_stdin(proc, stdin_text)
        stall_watch = _StallWatch(proc.pid, stall_seconds, on_stall) if stall_seconds else None
        try:
            if on_progress is not None:
                _wait_with_progress(
                    proc, out_f, on_progress, progress_poll_seconds, timeout, should_cancel, stall_watch
                )
            elif should_cancel is not None or stall_watch is not None:
                _wait_for_cancel(proc, progress_poll_seconds, timeout, should_cancel, stall_watch)
            else:
                proc.wait(timeout=timeout)
        except ProcessCancelled:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        except DataPathStalled as exc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise DataPathStalled(
                exc.idle_seconds,
                stdout=_read_tail(out_f, cap, text),
                stderr=_read_tail(err_f, cap, text),
            ) from None
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(
                cmd,
                timeout,
                output=_read_tail(out_f, cap, text),
                stderr=_read_tail(err_f, cap, text),
            )
        stdout = _read_tail(out_f, cap, text)
        stderr = _read_tail(err_f, cap, text)

    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)

    return CompletedProcess(args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr)


def describe_process_end(program: str, returncode: int) -> str:
    if returncode >= 0:
        return f"{program} exited with code {returncode}"
    number = -returncode
    try:
        return f"{program} was terminated by signal {number} ({signal.Signals(number).name})"
    except ValueError:
        return f"{program} was terminated by signal {number}"
