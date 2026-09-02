# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from .common_arguments import add_name_or_id_selector
from .validators import parse_csv_values, parse_json_object, parse_key_value_pairs, parse_strict_hhmm, parse_weekday_csv

__all__ = [
    "AltaStorParserBuilder",
    "add_name_or_id_selector",
    "build_parser",
    "parse_csv_values",
    "parse_json_object",
    "parse_key_value_pairs",
    "parse_strict_hhmm",
    "parse_weekday_csv",
]


def __getattr__(name: str):
    if name in {"AltaStorParserBuilder", "build_parser"}:
        from .parser_builder import AltaStorParserBuilder, build_parser

        return {"AltaStorParserBuilder": AltaStorParserBuilder, "build_parser": build_parser}[name]
    raise AttributeError(name)
