"""`howlplane --help` leads with the everyday workflow and drops no command."""

import argparse

import pytest

from howlplane.control_plane.cli import build_parser


def _help(parser):
    return parser.format_help()


def _subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []


@pytest.mark.unit
def test_top_level_help_leads_with_get_started_and_quickstart():
    text = _help(build_parser())
    assert text.index("howlplane setup") < text.index("Everyday:") < text.index("Advanced engineering:")
    first_group = text[text.index("Everyday:"):text.index("Governance and recovery:")]
    for name in ("setup", "start", "status", "logs", "stop", "approve", "reject", "doctor"):
        assert f"\n  {name} " in first_group
    assert "\n  factory " not in first_group


@pytest.mark.unit
def test_every_registered_command_is_still_listed_in_help():
    parser = build_parser()
    text = _help(parser)
    for name in _subcommands(parser):
        assert f"\n  {name} " in text, name


@pytest.mark.unit
def test_factory_help_shows_everyday_flow_before_advanced():
    parser = build_parser()
    factory = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["factory"]
    text = _help(factory)
    assert "start -> status -> logs -> stop" in text
    assert text.index("Everyday") < text.index("Advanced and debugging")
    for name in ("run-once", "canary", "queue"):
        assert f"\n  {name} " in text
