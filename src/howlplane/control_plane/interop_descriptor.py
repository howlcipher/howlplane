"""The ``howl.interop/v1`` descriptor printed by ``howlplane interop --json``.

``howl doctor`` reads it to check that the commands other components run here
(``orchestrate``, ``agents doctor``, ``factory prepare``, ``reconcile``) still
exist, and that every command HowlPlane runs of another component is still
offered by the installed producer. Commands come from the live argparse tree;
``consumes`` lists the exact argv HowlPlane builds. Contract:
https://github.com/howlcipher/howl/blob/main/docs/INTEROP.md
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

from howlplane import __version__

SCHEMA = "howl.interop/v1"

EXIT_CODES = {
    "0": "ok",
    "1": "error, including a usage or input error (a howlplane.error/v1 object on stderr with --json)",
    "2": "awaiting a human decision (approval, handoff, or confirmation); nothing else uses 2",
    "3": "blocked by a boundary, readiness or verification gate (MISMATCH, REJECTED, refused preflight)",
    "4": "write: an owner-only decision HowlPlane will not relay (HowlWriter's code, passed through)",
    "141": "the reader of stdout went away",
}

ENV = [
    "HOWLPLANE_HOME",
    "HOWLPLANE_DIR",
    "HOWLCHANGEOPS_BIN",
    "CHANGEOPS_BIN",
    "HOWLFRAME_BIN",
    "HOWLDREAM_BIN",
    "HOWLCREATE_BIN",
    "HOWLWRITER_BIN",
    "HOWLINSTINCT_BIN",
    "HOWL_FORBID_LOCAL_INFERENCE",
]

#: Every command HowlPlane runs of another component, with the flags it passes.
CONSUMES: List[Dict[str, Any]] = [
    {"component": "howlchangeops", "command": ["evaluate"], "flags": ["--json"], "min_version": "0.2.0"},
    {"component": "howlchangeops", "command": ["approve"], "flags": ["--json", "--owner-confirmed", "--approver"],
     "min_version": "0.2.0"},
    {"component": "howlchangeops", "command": ["execute"], "flags": ["--json"], "min_version": "0.2.0"},
    {"component": "howlframe", "command": ["run"], "flags": ["--max-instructions"]},
    {"component": "howlframe", "command": ["build"], "flags": ["-o"]},
    {"component": "howlframe", "command": [], "flags": ["--version", "-run-bc", "-allow-caps"]},
    {"component": "howlinstinct", "command": ["decide"], "flags": ["--input", "--json"],
     "schemas": ["howlinstinct.decide_output/v1", "howlinstinct.decision_receipt/v1"]},
    {"component": "howldream", "command": ["explore"], "flags": ["--output", "--command-config", "--allow-remote"],
     "schemas": ["howl.exploration_result/v1"]},
    {"component": "howldream", "command": ["export"], "flags": ["--candidate-id"], "schemas": ["howl.candidate/v1"]},
    {"component": "howldream", "command": ["cluster"], "flags": ["--external", "--rank", "--out"]},
    {"component": "howlcreate", "command": ["develop"],
     "flags": ["--from-writer", "--from-dream", "--output", "--provider", "--command-config"],
     "schemas": ["howl.development_result/v1"]},
    {"component": "howlcreate", "command": ["scaffold"], "flags": ["--from-writer", "--from-dream", "--output"],
     "schemas": ["howl.development_result/v1"]},
    {"component": "howlcreate", "command": ["materialize"], "flags": ["--input", "--output-dir"]},
    {"component": "howlwriter", "command": [],
     "flags": ["--version", "--provider", "--review-provider", "--local-model"]},
    {"component": "howlwriter", "command": ["writer"], "flags": ["--fail-if-blocked", "--out", "--target-words"]},
    {"component": "howlwriter", "command": ["project", "draft"],
     "flags": ["--instructions", "--json", "--fail-if-blocked", "--out", "--mode", "--voice"]},
    {"component": "howlwriter", "command": ["humanize"], "flags": ["--out", "--voice"]},
    {"component": "howlwriter", "command": ["lint"], "flags": ["--json"]},
    {"component": "howlwriter", "command": ["native", "request"], "flags": ["--from-dream", "--spec", "--out"],
     "schemas": ["howlwriter.request/v1"]},
    {"component": "howlwriter", "command": ["native", "write"],
     "flags": ["--request", "--command-config", "--out", "--failure-out"], "schemas": ["howlwriter.copy_package/v1"]},
]

EMITS = {
    ("reconcile",): [],
    ("interop",): [SCHEMA],
}

# Commands answered by the Go launcher itself, not the Python engine.
GO_COMMANDS: List[Dict[str, Any]] = [
    {"path": ["project"], "flags": [], "json": False},
    {"path": ["project", "validate"], "flags": [], "json": False},
]


def _flags(parser: argparse.ArgumentParser) -> List[str]:
    return sorted(
        option
        for action in parser._actions
        for option in action.option_strings
        if option not in ("-h", "--help")
    )


def describe(parser: argparse.ArgumentParser) -> Dict[str, Any]:
    """The descriptor for ``parser`` (every nested subcommand) plus the Go launcher's own commands."""
    commands: List[Dict[str, Any]] = []
    seen = set()

    def walk(current: argparse.ArgumentParser, path: List[str]) -> None:
        key = tuple(path)
        if key in seen:  # an alias reaches the same parser under another name
            return
        seen.add(key)
        flags = _flags(current)
        entry: Dict[str, Any] = {"path": path, "flags": flags, "json": "--json" in flags or key == ("interop",)}
        if EMITS.get(key):
            entry["emits"] = EMITS[key]
        commands.append(entry)
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, [*path, name])

    walk(parser, [])
    # --color and --debug are read before parsing so they work anywhere on the line.
    commands[0]["flags"] = sorted({*commands[0]["flags"], "--color", "--debug"})
    for go_command in GO_COMMANDS:
        if tuple(go_command["path"]) not in seen:
            commands.append(dict(go_command))
    return {
        "schema": SCHEMA,
        "component": "howlplane",
        "version": __version__,
        "commands": commands,
        "exit_codes": EXIT_CODES,
        "env": ENV,
        "consumes": CONSUMES,
    }
