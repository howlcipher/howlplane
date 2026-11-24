"""`howlplane tui`: an interactive terminal for one objective at a time (ADR 0009)."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from howlplane.control_plane.config_loader import ConfigLoader


def command(args: argparse.Namespace) -> int:
    from howlplane.control_plane.cli import _resolve_repo
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    from howlplane.control_plane.tui.app import TuiApp, default_state_root, session_log
    from howlplane.control_plane.tui.backend import HowlPlaneBackend
    from howlplane.control_plane.tui.reducer import ReducerError, ReducerRunner
    from howlplane.control_plane.tui.terminal import open_terminal

    target_repo = Path(_resolve_repo(args))
    settings = ConfigLoader().settings.tui
    if getattr(args, "engine", "python") == "howlframe":
        return _howlframe_engine(target_repo, settings)
    reducer = ReducerRunner(settings)
    try:
        reducer.check_ready()
    except ReducerError as exc:
        raise OperatorFailure(OperatorError(exc.code, str(exc), exc.why, exc.next_action)) from exc
    session_id = secrets.token_hex(8)
    state_root = default_state_root()
    log = session_log(state_root, session_id)
    backend = HowlPlaneBackend(target_repo, state_root / "runs")
    with open_terminal(sys.stdin, sys.stdout) as terminal:
        return TuiApp(backend, reducer, terminal, settings, log).run_loop()


def _howlframe_engine(target_repo: Path, settings) -> int:
    from howlplane.control_plane.howlframe_runner import find_howlframe_binary
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    from howlplane.control_plane.tui import howlframe_engine
    from howlplane.control_plane.tui.app import default_state_root

    howlframe = find_howlframe_binary()
    if not howlframe:
        raise OperatorFailure(OperatorError(
            "HOWLFRAME_UNAVAILABLE", "The HowlFrame runtime was not found.",
            "--engine howlframe runs the TUI on the HowlFrame VM.",
            "Install HowlFrame or set HOWLFRAME_BIN, or use the default engine."))
    try:
        return howlframe_engine.run(howlframe, target_repo, settings, default_state_root())
    except howlframe_engine.LaunchError as exc:
        raise OperatorFailure(OperatorError(
            "TUI_LAUNCH_FAILED", str(exc), "The HowlFrame TUI could not be prepared.",
            "Fix the reported path, or use the default engine.")) from exc
