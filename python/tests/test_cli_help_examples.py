# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import unittest

from python.cli.parsing.parser_builder import build_parser


def _iter_leaf_parsers(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    subparsers_action = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if subparsers_action is None:
        yield path, parser
        return
    for name, subparser in subparsers_action.choices.items():
        yield from _iter_leaf_parsers(subparser, path + (name,))


class CliHelpExamplesTests(unittest.TestCase):
    def test_every_leaf_command_help_includes_an_examples_section(self) -> None:
        parser = build_parser()
        leaves = list(_iter_leaf_parsers(parser))

        missing = [" ".join(("abr", *path)) for path, leaf in leaves if "Examples:" not in leaf.format_help()]

        self.assertEqual(missing, [], f"Leaf commands missing an Examples: section: {missing}")

    def test_every_example_line_is_runnable_against_its_own_command_path(self) -> None:
        parser = build_parser()
        expected_prefix_by_path = {path: "abr " + " ".join(path) for path, _ in _iter_leaf_parsers(parser)}
        for path, leaf in _iter_leaf_parsers(parser):
            help_text = leaf.format_help()
            if "Examples:" not in help_text:
                continue
            examples_block = help_text.split("Examples:", 1)[1]
            for line in examples_block.splitlines():
                line = line.strip()
                if "abr " not in line:
                    continue
                command_part = line[line.index("abr "):]
                self.assertTrue(
                    command_part.startswith(expected_prefix_by_path[path]),
                    f"Example {line!r} does not invoke its own command {expected_prefix_by_path[path]!r}",
                )


if __name__ == "__main__":
    unittest.main()
