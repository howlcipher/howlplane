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
