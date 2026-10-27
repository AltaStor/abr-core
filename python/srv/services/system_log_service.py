# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Read-only view of the appliance's own server log, for GET /api/v1/system/logs.

The console's Logs page used to assemble a "system log" client-side out of
alerts and per-session backup lines, which is a different thing entirely: it
could only ever show what the API already returned elsewhere, and never the
appliance's own scheduler/TLS/database chatter. This reads the real file the
server writes (``server.log``, see core/logger_conf.py) and parses its fixed
line format back into structured records.

Everything here is a tail: a rotated log is 10 MB, and no operator wants (nor
should the API move) all of it, so a bounded window from the end of the newest
file backwards is read, oldest lines dropped first.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from python.srv.core.logger_conf import (
    COMPONENT_LOG_FILENAMES,
    COMPONENT_LOG_SUBDIRS,
    SYSTEM_LOG_DIR,
    _resolve_log_dir,
    redact_text,
)

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000

# Enough that DEFAULT_LIMIT lines are almost always in the first file read,
# while never pulling a whole rotated 10 MB log into memory.
TAIL_BYTES = 512 * 1024

# The console's own level vocabulary; "critical" is folded into "error" because
# nothing downstream distinguishes them.
LEVELS = ("debug", "info", "warn", "error")
_LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}

_PYTHON_LEVELS = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "error",
    "FATAL": "error",
}

# Mirrors logger_conf's "file_format":
#   %(asctime)s | %(levelname)-8s | PID:%(process)d | %(name)s:%(funcName)s:%(lineno)d | %(message)s
# `levelname` is space-padded to 8, so every separator is matched loosely.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*"
    r"(?P<level>[A-Z]+)\s*\|\s*"
    r"PID:(?P<pid>\d+)\s*\|\s*"
    r"(?P<origin>[^|]*?)\s*\|\s*"
    r"(?P<message>.*)$"
)


def _log_dirs() -> list[Path]:
    """Where a server log may live, most-specific first.

    Same order the support bundle searches, so the page and the bundle can never
    disagree about which file is "the" server log on a given install.
    """
    dirs: list[Path] = []
    explicit = os.environ.get("ALTASTOR_LOG_DIR")
    if explicit:
        dirs.append(Path(explicit))
    dirs.append(Path(SYSTEM_LOG_DIR) / COMPONENT_LOG_SUBDIRS["srv"])
    dirs.append(Path(_resolve_log_dir("srv")))

    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        key = os.path.abspath(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _rotation_index(path: Path, base: str) -> int:
    """RotatingFileHandler's suffix as a number — `server.log` is 0 (newest),
    `server.log.1` is 1, and anything unparseable sorts last."""
    if path.name == base:
        return 0
    suffix = path.name[len(base) :].lstrip(".")
    return int(suffix) if suffix.isdigit() else 1 << 30


def log_files() -> tuple[list[Path], list[Path]]:
    """The server's log files newest-first, plus the directories searched.

    Only the plain rotation series is returned: `.fault` files hold raw
    faulthandler tracebacks in no particular format, and feeding those through
    the line parser would produce a screen of unattributed continuation text.
    """
    base = COMPONENT_LOG_FILENAMES["srv"]
    searched = _log_dirs()
    for directory in searched:
        try:
            found = [
                path
                for path in directory.glob(base + "*")
                if path.is_file() and not path.name.endswith(".fault")
            ]
        except OSError:
            continue
        if found:
            return sorted(found, key=lambda p: _rotation_index(p, base)), searched
    return [], searched


def _read_tail(path: Path) -> tuple[str, bool]:
    """The last TAIL_BYTES of `path`, secrets redacted, plus whether the read
    started mid-file (in which case the first, partial line is dropped)."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        truncated = size > TAIL_BYTES
        handle.seek(size - TAIL_BYTES if truncated else 0)
        raw = handle.read()

    if truncated:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline != -1 else b""

    return redact_text(raw.decode("utf-8", errors="replace")), truncated


def _iso_timestamp(stamp: str) -> str:
    """`logger_conf` formats with no timezone, so the file's wall-clock time is
    local time. Stamping the appliance's own offset onto it is what lets the
    console render it in the reader's zone instead of guessing UTC."""
    try:
        naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp
    return naive.astimezone().isoformat()


def _source_of(logger: str) -> str:
    """The chip the console groups lines by: the leaf of the logger name, which
    is the module that actually emitted the line (`job_scheduler`, `task_pool`,
    `vcenter_backup`) rather than the `python.srv.…` package path it lives in."""
    leaf = logger.rsplit(".", 1)[-1].strip()
    return leaf or logger.strip() or "srv"


def _entry_id(timestamp: str, origin: str, message: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{timestamp}|{origin}|{message}".encode("utf-8")).hexdigest()
    # The ordinal keeps two identical lines logged in the same second distinct,
    # which they legitimately can be (a retry loop logging the same warning).
    # It counts only such repeats, so a line keeps its ID from one read to the
    # next while the tail window slides down a growing file.
    return f"{digest[:16]}-{ordinal}"


def parse_log_text(text: str) -> list[dict[str, Any]]:
    """Parse a chunk of server log into records, oldest first.

    A line that doesn't match the format is a continuation — a traceback body,
    or a logged message that itself spans lines — and belongs to the record
    above it. Continuations before the first parsed record have lost their
    header (the tail cut into the middle of a traceback) and are dropped.
    """
    entries: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        match = _LINE_RE.match(raw_line)
        if match is None:
            if entries and raw_line.strip():
                entries[-1]["message"] += "\n" + raw_line
            continue

        origin = match.group("origin")
        logger, _, tail = origin.partition(":")
        function, _, line_number = tail.partition(":")
        entries.append(
            {
                "id": "",  # assigned below, once the message is complete
                "ts": _iso_timestamp(match.group("ts")),
                "level": _PYTHON_LEVELS.get(match.group("level").upper(), "info"),
                "source": _source_of(logger),
                "logger": logger,
                "function": function or None,
                "line": int(line_number) if line_number.isdigit() else None,
                "pid": int(match.group("pid")),
                "message": match.group("message"),
            }
        )

    repeats: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        key = (entry["ts"], entry["logger"], entry["message"])
        repeats[key] = repeats.get(key, -1) + 1
        entry["id"] = _entry_id(*key, repeats[key])
    return entries


def _normalize_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _min_rank(level: str | None) -> int:
    if not level:
        return 0
    return _LEVEL_RANK.get(str(level).strip().lower(), 0)


def read_system_log(*, limit: int | None = None, level: str | None = None) -> dict[str, Any]:
    """Newest-first window of the appliance's server log.

    `level` is a FLOOR, not an exact match: "warn" answers warnings and errors,
    which is what an operator triaging a failed night actually wants. Files are
    walked newest-first and stop as soon as `limit` matching records are in
    hand, so the common case reads one tail of one file.
    """
    limit = _normalize_limit(limit)
    floor = _min_rank(level)

    files, searched = log_files()
    collected: list[dict[str, Any]] = []
    read_files: list[str] = []
    unreadable: list[str] = []
    seen_ids: set[str] = set()
    more_available = False

    for path in files:
        if len(collected) >= limit:
            more_available = True
            break
        try:
            text, truncated = _read_tail(path)
        except OSError as exc:
            unreadable.append(f"{path}: {exc}")
            continue

        read_files.append(str(path))
        entries = parse_log_text(text)
        for entry in entries:
            # The same line in two files (logged in the second a file rotated)
            # still needs its own ID.
            while entry["id"] in seen_ids:
                entry["id"] += "r"
            seen_ids.add(entry["id"])
        if floor:
            entries = [e for e in entries if _LEVEL_RANK.get(e["level"], 1) >= floor]
        # Newest-first overall: within a file, later lines are newer.
        entries.reverse()
        collected.extend(entries)
        if truncated and len(collected) < limit:
            # The window ran out before the caller's limit did, so there IS more
            # history — it just isn't in the bytes we agreed to read.
            more_available = True

    if len(collected) > limit:
        collected = collected[:limit]
        more_available = True

    return {
        "entries": collected,
        "total": len(collected),
        "limit": limit,
        "level": LEVELS[floor] if floor else None,
        "has_more": more_available,
        "files": read_files,
        "searched": [str(d) for d in searched],
        "warnings": unreadable,
    }
