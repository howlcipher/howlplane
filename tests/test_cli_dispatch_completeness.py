#!/usr/bin/env python3
"""
test_cli_dispatch_completeness.py

Every subcommand a parser registers must have somewhere to go.

argparse does not connect a subparser to an implementation; a separate table
does. Nothing enforces that the two agree, and when they disagree the failure
is quiet: argparse accepts the subcommand and parses its flags, the dispatch
lookup misses, and the operator gets top-level help and exit 1 -- which reads
like a usage mistake, not a missing implementation.

`howlplane marathon` shipped in exactly that state. The parser advertised the
subcommand, its `--help` rendered marathon-specific help, `cmd_marathon` existed
and was correct, and the canonical launcher had no entry for it, so the command
the unattended dogfood run depends on could not be invoked at all.
`howlplane acceptance` had the same gap in the cli entry point.

Both directions are checked: a registered subcommand with no handler is dead on
arrival, and a handler for an unregistered subcommand is unreachable code.
"""

import argparse
from typing import Dict, Set

import pytest

from src.control_plane import cli, launcher


def _registered_subcommands(parser: argparse.ArgumentParser) -> Set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError(f"{parser.prog} registers no subparsers")


ENTRY_POINTS = {
    "launcher": (launcher.build_parser, lambda: launcher.ACTIONS),
    "cli": (cli.build_parser, lambda: cli.HANDLERS),
}


@pytest.mark.unit
@pytest.mark.parametrize("entry_point", sorted(ENTRY_POINTS))
def test_every_registered_subcommand_has_a_dispatch_target(entry_point):
    build_parser, get_table = ENTRY_POINTS[entry_point]
    registered = _registered_subcommands(build_parser())
    table: Dict[str, object] = get_table()

    undispatched = sorted(registered - set(table))

    assert not undispatched, (
        f"{entry_point}.build_parser() registers {undispatched} with no dispatch "
        f"entry. The subcommand will parse, then print top-level help and exit 1."
    )


@pytest.mark.unit
@pytest.mark.parametrize("entry_point", sorted(ENTRY_POINTS))
def test_every_dispatch_target_is_reachable_and_callable(entry_point):
    build_parser, get_table = ENTRY_POINTS[entry_point]
    registered = _registered_subcommands(build_parser())
    table: Dict[str, object] = get_table()

    unreachable = sorted(set(table) - registered)
    assert not unreachable, (
        f"{entry_point} dispatches {unreachable}, which no subparser registers."
    )

    not_callable = sorted(name for name, fn in table.items() if not callable(fn))
    assert not not_callable, f"{entry_point} maps {not_callable} to non-callables."


@pytest.mark.unit
def test_marathon_specifically_dispatches_from_the_canonical_launcher():
    """The command the unattended dogfood run is invoked with."""
    assert "marathon" in _registered_subcommands(launcher.build_parser())
    assert launcher.ACTIONS["marathon"] is cli.cmd_marathon


@pytest.mark.unit
def test_the_scan_would_catch_a_missing_handler(monkeypatch):
    """A completeness check that cannot fail is not a completeness check."""
    monkeypatch.setattr(
        launcher,
        "ACTIONS",
        {k: v for k, v in launcher.ACTIONS.items() if k != "marathon"},
    )
    with pytest.raises(AssertionError, match="marathon"):
        test_every_registered_subcommand_has_a_dispatch_target("launcher")
