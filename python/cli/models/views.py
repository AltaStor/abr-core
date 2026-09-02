# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any


def parse_iso_datetime(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def human_size(size_bytes: int | float | None) -> str:
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return "N/A"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return "N/A"
