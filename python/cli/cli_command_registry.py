# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse


class CliCommandRegistry:
    def __init__(self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        self.subparsers = subparsers

    def add_parser(self, *args, **kwargs) -> argparse.ArgumentParser:
        return self.subparsers.add_parser(*args, **kwargs)
