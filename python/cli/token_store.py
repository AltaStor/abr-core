# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CONFIG_DIR_ENV_VAR = "ABR_CLI_CONFIG_DIR"

_DEFAULT_PORTS = {"https": 443, "http": 80}


def normalise_base_url(base_url: str) -> str:
    raw = base_url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw.rstrip("/")
    if not parts.scheme or not parts.hostname:
        return raw.rstrip("/")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def _config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".altastor-backup"


def _credentials_path() -> Path:
    return _config_dir() / "credentials.json"


def _load_all() -> dict[str, Any]:
    path = _credentials_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _matching_keys(data: dict[str, Any], base_url: str) -> set[str]:
    key = normalise_base_url(base_url)
    return {stored for stored in data if normalise_base_url(stored) == key} | ({key} & set(data))


def save_token(base_url: str, token: str, expires_at: str) -> None:
    data = _load_all()
    for stale in _matching_keys(data, base_url):
        data.pop(stale, None)
    data[normalise_base_url(base_url)] = {"token": token, "expires_at": expires_at}
    _write_all(data)


def load_token(base_url: str) -> str | None:
    data = _load_all()
    for key in sorted(_matching_keys(data, base_url)):
        entry = data.get(key)
        if isinstance(entry, dict):
            token = entry.get("token")
            if isinstance(token, str) and token:
                return token
    return None


def clear_token(base_url: str) -> None:
    data = _load_all()
    matching = _matching_keys(data, base_url)
    if not matching:
        return
    for key in matching:
        data.pop(key, None)
    if data:
        _write_all(data)
    else:
        _credentials_path().unlink(missing_ok=True)
