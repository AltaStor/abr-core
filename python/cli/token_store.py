# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

CONFIG_DIR_ENV_VAR = "ABR_CLI_CONFIG_DIR"


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


def save_token(base_url: str, token: str, expires_at: str) -> None:
    data = _load_all()
    data[base_url] = {"token": token, "expires_at": expires_at}
    _write_all(data)


def load_token(base_url: str) -> str | None:
    entry = _load_all().get(base_url)
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    return token if isinstance(token, str) and token else None


def clear_token(base_url: str) -> None:
    data = _load_all()
    if base_url not in data:
        return
    data.pop(base_url, None)
    if data:
        _write_all(data)
    else:
        _credentials_path().unlink(missing_ok=True)
