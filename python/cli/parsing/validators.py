# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import json
import re
from typing import Any

WEEKDAY_NAME_MAP = {
    "mon": "monday",
    "tue": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}
STRICT_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DURATION_PATTERN = re.compile(r"^(?P<value>[1-9]\d*)(?P<unit>[mhdMHD])$")


def parse_key_value_pairs(values: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected KEY=VALUE pair, got: {value}")
        key, raw_value = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Metadata key cannot be empty: {value}")
        metadata[key] = raw_value.strip()
    return metadata


def parse_json_object(raw: str, *, field_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must decode to a JSON object")
    return value


def parse_csv_values(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_strict_hhmm(value: str) -> str:
    if not STRICT_TIME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("`--at` must use strict HH:MM format")
    return value


def parse_weekday_csv(raw: str) -> list[str]:
    values = parse_csv_values(raw)
    if not values:
        raise argparse.ArgumentTypeError("`--weekdays` must include at least one weekday")

    normalized: list[str] = []
    invalid: list[str] = []
    for value in values:
        mapped = WEEKDAY_NAME_MAP.get(value.strip().lower())
        if mapped is None:
            invalid.append(value)
            continue
        normalized.append(mapped)

    if invalid:
        valid = ", ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
        bad_values = ", ".join(invalid)
        raise argparse.ArgumentTypeError(f"Unknown weekday value(s): {bad_values}. Valid values: {valid}")

    deduped: list[str] = []
    for day in normalized:
        if day not in deduped:
            deduped.append(day)
    return deduped


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_day_of_month(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("`--day-of-month` must be an integer from 1 to 31") from exc
    if parsed < 1 or parsed > 31:
        raise argparse.ArgumentTypeError("`--day-of-month` must be an integer from 1 to 31")
    return parsed


def parse_compression_level(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("`--compression-level` must be an integer from 0 to 9") from exc
    if parsed < 0 or parsed > 9:
        raise argparse.ArgumentTypeError("`--compression-level` must be an integer from 0 to 9")
    return parsed


def parse_duration(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not DURATION_PATTERN.fullmatch(normalized):
        raise argparse.ArgumentTypeError("`--every` must use a duration like 15m, 1h, 6h, or 1d")
    return normalized
