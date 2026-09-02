# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from .abr_cli import main as _main_impl


def main(argv: list[str] | None = None) -> int:
    return _main_impl(argv)


if __name__ == "__main__":
    raise SystemExit(main())
