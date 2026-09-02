# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import textwrap
from typing import Any


class TableRenderer:
    MAX_CELL_WIDTH = 80

    @staticmethod
    def resolve_cell_value(row: dict[str, Any], key: str) -> Any:
        if key in row:
            return row.get(key)
        current: Any = row
        for segment in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(segment)
            if current is None:
                return None
        return current

    @staticmethod
    def stringify_cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return str(value)

    @classmethod
    def wrap_cell(cls, text: str) -> list[str]:
        if len(text) <= cls.MAX_CELL_WIDTH:
            return [text]
        return textwrap.wrap(text, width=cls.MAX_CELL_WIDTH, break_long_words=False, break_on_hyphens=False) or [text]

    def render(self, rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
        headers = [label for _, label in columns]
        table_rows = [
            [self.wrap_cell(self.stringify_cell(self.resolve_cell_value(row, key))) for key, _ in columns]
            for row in rows
        ]
        widths = [
            max([len(header), *(len(line) for row in table_rows for line in row[index])])
            for index, header in enumerate(headers)
        ]
        rendered = [
            "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
            "  ".join("-" * width for width in widths),
        ]
        for row in table_rows:
            for line_index in range(max(len(cell) for cell in row)):
                rendered.append(
                    "  ".join(
                        (cell[line_index] if line_index < len(cell) else "").ljust(widths[index])
                        for index, cell in enumerate(row)
                    )
                )
        return "\n".join(rendered)
