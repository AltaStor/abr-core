# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from python.srv.core.mongo_db_wrapper import load_system_state, persist_system_state
from python.srv.core.server_defaults import env_int

LOGGER = logging.getLogger(__name__)

STATE_ID = "clock"
WATERMARK_INTERVAL_SECONDS = 300
RUNNING = "running"
STARTUP = "startup"
CONFIRM_COMMAND = "abr system clock confirm"

_lock = threading.Lock()
_state: dict[str, Any] = {}
_last_wall: datetime | None = None
_last_monotonic: float | None = None
_last_persisted: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def jump_threshold() -> timedelta:
    return timedelta(hours=env_int("BACKUPSERVER_CLOCK_JUMP_HOURS"))


def span_text(span: timedelta) -> str:
    seconds = round(span.total_seconds() / 60) * 60
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    if days and hours:
        return f"{days} day(s) {hours} hour(s)"
    if days:
        return f"{days} day(s)"
    return f"{max(hours, 1)} hour(s)"


def _persist() -> None:
    try:
        persist_system_state(dict(_state))
    except Exception as exc:
        LOGGER.warning("Could not record the server clock state in the catalog: %s", exc)


def _block(*, trusted: datetime, current: datetime, source: str) -> None:
    jump = current - trusted
    _state.update(
        {
            "blocked": True,
            "detected_at": _iso(current),
            "last_trusted_at": _iso(trusted),
            "jump_seconds": round(jump.total_seconds()),
            "source": source,
            "confirmed_at": None,
            "confirmed_by": None,
        }
    )
    LOGGER.error("%s", _message_locked())


def _message_locked() -> str | None:
    if not _state.get("blocked"):
        return None
    jump = timedelta(seconds=int(_state.get("jump_seconds") or 0))
    if _state.get("source") == STARTUP:
        how = (
            f"between two runs of the server: it last ran at {_state.get('last_trusted_at')} and started at "
            f"{_state.get('detected_at')}, so it was off that long or its clock was moved"
        )
    else:
        how = (
            f"while the server was running: it read {_state.get('detected_at')} where "
            f"{_state.get('last_trusted_at')} was due"
        )
    return (
        f"Retention is blocked: the server clock moved forward by {span_text(jump)} {how}. Restore points "
        f"would look older than they are, so retention deletes nothing until an Administrator confirms the "
        f"time is right ({CONFIRM_COMMAND})."
    )


def load() -> dict[str, Any]:
    global _last_wall, _last_monotonic, _last_persisted
    now, monotonic = _now(), time.monotonic()
    try:
        stored = load_system_state(STATE_ID) or {}
    except Exception as exc:
        LOGGER.warning("Could not read the server clock state from the catalog: %s", exc)
        stored = {}
    with _lock:
        _state.clear()
        _state.update(stored)
        _state["id"] = STATE_ID
        last_known = _parse(_state.get("last_known_at"))
        if _state.get("blocked"):
            LOGGER.error("%s", _message_locked())
        elif last_known is not None and now - last_known > jump_threshold():
            _block(trusted=last_known, current=now, source=STARTUP)
        if not _state.get("blocked"):
            _state["last_known_at"] = _iso(now)
            if last_known is None:
                LOGGER.info("Server time %s recorded; later starts are checked against it.", _iso(now))
            else:
                LOGGER.info(
                    "Server time %s checked against the last known time %s; retention trusts it.",
                    _iso(now),
                    _iso(last_known),
                )
        _last_wall, _last_monotonic, _last_persisted = now, monotonic, monotonic
        _persist()
        return _status_locked(now)


def tick() -> None:
    global _last_wall, _last_monotonic, _last_persisted
    now, monotonic = _now(), time.monotonic()
    with _lock:
        if _last_wall is not None and _last_monotonic is not None and not _state.get("blocked"):
            due = _last_wall + timedelta(seconds=monotonic - _last_monotonic)
            if now - due > jump_threshold():
                _block(trusted=due, current=now, source=RUNNING)
                _persist()
                _last_persisted = monotonic
        _last_wall, _last_monotonic = now, monotonic
        if _state.get("blocked"):
            return
        if _last_persisted is None or monotonic - _last_persisted >= WATERMARK_INTERVAL_SECONDS:
            _state["id"] = STATE_ID
            _state["last_known_at"] = _iso(now)
            _persist()
            _last_persisted = monotonic


def retention_block() -> str | None:
    with _lock:
        return _message_locked()


def confirm(user: str | None) -> dict[str, Any]:
    global _last_wall, _last_monotonic, _last_persisted
    now, monotonic = _now(), time.monotonic()
    with _lock:
        was_blocked = bool(_state.get("blocked"))
        _state.update(
            {
                "id": STATE_ID,
                "blocked": False,
                "last_known_at": _iso(now),
                "confirmed_at": _iso(now),
                "confirmed_by": user,
            }
        )
        _last_wall, _last_monotonic, _last_persisted = now, monotonic, monotonic
        _persist()
        status = _status_locked(now)
    if was_blocked:
        LOGGER.warning("%s confirmed the server time %s; retention deletes expired restore points again.", user, _iso(now))
    else:
        LOGGER.info("%s confirmed the server time %s; retention was not blocked.", user, _iso(now))
    return {**status, "was_blocked": was_blocked}


def status() -> dict[str, Any]:
    with _lock:
        return _status_locked(_now())


def _status_locked(now: datetime) -> dict[str, Any]:
    return {
        "blocked": bool(_state.get("blocked")),
        "server_time": _iso(now),
        "last_known_at": _state.get("last_known_at"),
        "detected_at": _state.get("detected_at"),
        "last_trusted_at": _state.get("last_trusted_at"),
        "jump_seconds": _state.get("jump_seconds"),
        "source": _state.get("source"),
        "threshold_hours": int(jump_threshold().total_seconds() // 3600),
        "confirmed_at": _state.get("confirmed_at"),
        "confirmed_by": _state.get("confirmed_by"),
        "message": _message_locked(),
    }


def forget() -> None:
    global _last_wall, _last_monotonic, _last_persisted
    with _lock:
        _state.clear()
        _last_wall = _last_monotonic = _last_persisted = None
