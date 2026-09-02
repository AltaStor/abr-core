# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
from typing import Any

from .table_renderer import TableRenderer


class CliOutputRenderer:
    def __init__(self, table_renderer: TableRenderer | None = None) -> None:
        self.table_renderer = table_renderer or TableRenderer()

    def print_json(self, payload: Any) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True))

    def print_output(self, output: str, payload: Any, columns: list[tuple[str, str]] | None = None) -> None:
        if output == "table":
            rows, resolved_columns = self._table_shape(payload, columns)
            print(self.table_renderer.render(rows, resolved_columns))
            return
        self.print_json(payload)

    @staticmethod
    def _table_shape(
        payload: Any, columns: list[tuple[str, str]] | None
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        if columns is not None:
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                return payload["items"], columns
            if isinstance(payload, dict):
                return [payload], columns
            if isinstance(payload, list):
                return payload, columns
            return [{"value": payload}], columns
        if isinstance(payload, dict):
            return [{"field": key, "value": value} for key, value in payload.items()], [
                ("field", "FIELD"),
                ("value", "VALUE"),
            ]
        if isinstance(payload, list):
            keys: list[str] = []
            for item in payload:
                if isinstance(item, dict):
                    for key in item:
                        if key not in keys:
                            keys.append(key)
            if keys:
                return payload, [(key, key.upper()) for key in keys]
            return [{"value": item} for item in payload], [("value", "VALUE")]
        return [{"value": payload}], [("value", "VALUE")]
