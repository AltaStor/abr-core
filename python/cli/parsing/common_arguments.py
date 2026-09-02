# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse


def add_name_or_id_selector(parser: argparse.ArgumentParser, *, name_help: str, id_help: str) -> None:
    parser.add_argument("--name", help=name_help)
    parser.add_argument("--id", help=id_help)
