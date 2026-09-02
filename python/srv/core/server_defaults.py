# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULTS_ENV_PATH = Path(__file__).resolve().parents[1] / "defaults.env"


def parse_defaults_env(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Server defaults file not found or unreadable: {path}") from exc

    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Malformed line {lineno} in {path}: {raw_line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            raise RuntimeError(f"Malformed line {lineno} in {path}: {raw_line!r}")
        values[key] = value.strip()

    if not values:
        raise RuntimeError(f"Server defaults file has no variables: {path}")
    return values


_DEFAULTS: dict[str, str] = {}


def apply_server_defaults(path: Path = DEFAULTS_ENV_PATH) -> dict[str, str]:
    global _DEFAULTS
    values = parse_defaults_env(path)
    _DEFAULTS = values
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def shipped_default(name: str) -> str | None:
    if name in _DEFAULTS:
        return _DEFAULTS[name]
    try:
        return parse_defaults_env(DEFAULTS_ENV_PATH).get(name)
    except RuntimeError:
        return None


def env_int(name: str, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError(f"{value} is below the minimum of {minimum}")
        return value
    except (TypeError, ValueError) as exc:
        fallback_raw = _DEFAULTS.get(name)
        try:
            fallback = int(fallback_raw)
            if fallback < minimum:
                raise ValueError
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Invalid {name}={raw!r} ({exc}) and no valid fallback in {DEFAULTS_ENV_PATH}"
            ) from None
        logger.warning("Invalid %s=%r (%s); falling back to default %r", name, raw, exc, fallback_raw)
        return fallback


apply_server_defaults()
