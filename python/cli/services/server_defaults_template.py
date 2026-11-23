# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.cli.core.logger_conf import REDACTED_VALUE
from python.cli.core.logger_conf import redact_text as _redact_text

DEFAULTS_ENV_PATH = Path(__file__).resolve().parents[2] / "srv" / "defaults.env"

EXTRA_SETTINGS_HEADER = (
    "# --- Additional recovered settings (not in the packaged .default template) ---"
)


def _load_defaults_env_template(path: Path = DEFAULTS_ENV_PATH) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Server defaults file not found or unreadable: {path}") from exc


def render_recovered_server_defaults(configuration: dict[str, Any]) -> str:
    remaining = dict(configuration)
    lines: list[str] = []

    for line in _load_defaults_env_template().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0]
            if key in remaining:
                lines.append(f"{key}={remaining.pop(key)}")
                continue
        lines.append(line)

    if remaining:
        lines.append("")
        lines.append(EXTRA_SETTINGS_HEADER)
        for key, value in sorted(remaining.items()):
            lines.append(f"{key}={value}")

    return _without_masked_values(_redact_text("\n".join(lines) + "\n"))


def _without_masked_values(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        key, _, value = stripped.partition("=")
        if stripped and not stripped.startswith("#") and REDACTED_VALUE in value:
            lines.append(f"# {key} was not exported: its value is secret. Set it here by hand if this server needs it.")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"
