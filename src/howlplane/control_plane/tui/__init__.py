"""`howlplane tui`: an interactive terminal for one objective at a time (ADR 0010).

The whole TUI is the HowlFrame program `integrations/howlframe/tui/tui_app.howl`;
this package only launches it with an exact grant profile.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from howlplane.control_plane.config_loader import ConfigLoader


def command(args: argparse.Namespace) -> int:
    from howlplane.control_plane.cli import _resolve_repo
    from howlplane.control_plane.howlframe_runner import find_howlframe_binary
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    from howlplane.control_plane.tui import howlframe_engine

    target_repo = Path(_resolve_repo(args))
    settings = ConfigLoader().settings.tui
    howlframe = find_howlframe_binary()
    if not howlframe:
        raise OperatorFailure(OperatorError(
            "HOWLFRAME_UNAVAILABLE", "The HowlFrame runtime was not found.",
            "howlplane tui runs entirely on the HowlFrame VM.",
            "Install HowlFrame or set HOWLFRAME_BIN to a howlframe binary with terminal_app."))
    try:
        return howlframe_engine.run(howlframe, target_repo, settings, howlframe_engine.default_state_root())
    except howlframe_engine.LaunchError as exc:
        raise OperatorFailure(OperatorError(
            "TUI_LAUNCH_FAILED", str(exc), "The HowlFrame TUI could not be prepared.",
            "Fix the reported path and try again.")) from exc
