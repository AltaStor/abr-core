# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BROKERS = ROOT / "python" / "srv" / "brokers"
BROKER_PACKAGE = "python.srv.brokers"
RUN_AS_ROOT_PACKAGES = (ROOT / "python" / "__init__.py", ROOT / "python" / "srv" / "__init__.py", BROKERS / "__init__.py")


def _package_of(path: Path) -> list[str]:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    return parts[:-1]


def _imported(path: Path) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level:
            package = _package_of(path)
            base = package[: len(package) - node.level + 1]
            found.append((".".join([*base, *([node.module] if node.module else [])]), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.lineno))
    return found


def _project_sources(*folders: Path) -> list[Path]:
    return sorted(path for folder in folders for path in folder.rglob("*.py") if "__pycache__" not in path.parts)


class BrokerIsolationTests(unittest.TestCase):
    def test_brokers_import_only_the_standard_library_and_each_other(self) -> None:
        foreign = [
            f"{path.relative_to(ROOT)}:{line} imports {name}"
            for path in _project_sources(BROKERS)
            for name, line in _imported(path)
            if name != "__future__" and name.split(".")[0] not in sys.stdlib_module_names and not name.startswith(BROKER_PACKAGE)
        ]
        self.assertEqual(foreign, [], "a broker runs as root and must not load project code:\n" + "\n".join(foreign))

    def test_nothing_outside_the_brokers_imports_them(self) -> None:
        sources = [path for path in _project_sources(ROOT / "python" / "srv", ROOT / "python" / "cli") if BROKERS not in path.parents]
        reaching = [
            f"{path.relative_to(ROOT)}:{line} imports {name}"
            for path in sources
            for name, line in _imported(path)
            if name.startswith(BROKER_PACKAGE)
        ]
        self.assertEqual(reaching, [], "the server reaches a broker only through its socket:\n" + "\n".join(reaching))

    def test_the_packages_root_runs_through_to_a_broker_hold_no_code(self) -> None:
        statements = [
            f"{path.relative_to(ROOT)}:{node.lineno} {type(node).__name__}"
            for path in RUN_AS_ROOT_PACKAGES
            if path.is_file()
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        self.assertEqual(statements, [], "python -m python.srv.brokers.<broker> runs these as root:\n" + "\n".join(statements))


if __name__ == "__main__":
    unittest.main()
