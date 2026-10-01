"""Everyday product commands: bare `howlplane`, `start`, `status`, `logs`, `stop`.

A thin facade. It owns no state, no status model and no logging: every command
resolves the repository's Factory campaign through the existing discovery and
delegates to the canonical Factory handlers, so `howlplane factory ...` and the
everyday commands cannot drift. State, health, reason, owner-required and the next
action all come from `presentation.operator`.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _cli():
    from howlplane.control_plane import cli
    return cli


_FACTORY_DEFAULTS = {
    "state_dir": None, "target_repo": None, "json": False, "verbose": False,
    "publish": False, "publish_path": None, "arm_periodic": False,
}

# Stop reasons that mean a person or the host paused work, or a bounded run ended
# normally. Anything else (failure, malformed state, blocked work) needs a deliberate restart.
_CONTINUABLE_STOP_REASONS = frozenset({
    "operator_stop", "signal_sigterm", "signal_sigint",
    "bounded_work_item_completed", "max_work_items_reached",
    "bounded_no_eligible_provider", "bounded_work_item_deferred",
})


def _factory_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    """Factory handler arguments built from the everyday command's arguments."""
    values = {**_FACTORY_DEFAULTS, **{k: v for k, v in vars(args).items() if v is not None}}
    values.update(overrides)
    values["everyday"] = True
    return argparse.Namespace(**values)


def _repo_start(args: argparse.Namespace) -> Optional[str]:
    return getattr(args, "repo", None) or getattr(args, "repo_dir", None) or None


def _campaign(args: argparse.Namespace):
    """The campaign for this repository without creating anything, or None outside a repository."""
    from howlplane.control_plane.factory.campaign import CampaignError, campaign_from_state_dir, resolve_campaign
    explicit = getattr(args, "state_dir", None)
    if explicit:
        return campaign_from_state_dir(explicit)
    try:
        return resolve_campaign(_repo_start(args), target_repo=getattr(args, "target_repo", None), prefer_active=True)
    except CampaignError:
        return None


def _has_state(campaign: Any) -> bool:
    """True once a Factory has recorded supervisor state for this campaign."""
    if campaign is None:
        return False
    supervisor_dir = Path(campaign.state_dir) / "supervisor"
    return supervisor_dir.is_dir() and any(supervisor_dir.glob("*.json"))


def _style():
    from howlplane.control_plane.presentation.style import resolve_style
    return resolve_style(sys.stdout, _cli()._COLOR_MODE)


def _project_name(args: argparse.Namespace) -> Optional[str]:
    from howlplane.control_plane.factory.campaign import CampaignError, discover_repository
    try:
        repository = discover_repository(_repo_start(args))
    except CampaignError:
        return None
    return repository.remote or repository.root.name


def _readiness(args: argparse.Namespace) -> Dict[str, Any]:
    """Read-only setup readiness (repository, workers, workspace preparation)."""
    from howlplane.control_plane import setup_cli
    return setup_cli.gather(Path(_repo_start(args) or ".").expanduser().resolve())


def _not_started(args: argparse.Namespace) -> Tuple[Any, Dict[str, Any], Optional[int]]:
    """Operator view of a repository where nothing has run: (operator, status, workers available)."""
    from howlplane.control_plane.presentation.operator import not_started_status
    report = _readiness(args)
    status = {"project": _project_name(args), "state": "not_started"}
    checks = report["checks"]
    blocked = next((c for c in checks if c["status"] == "blocked"), None)
    workers = sum(1 for c in checks if c["id"].startswith("agent.") and c["status"] == "ready")
    if blocked is not None and blocked["id"] == "repository":
        return (not_started_status(False, None, "howlplane setup",
                                   "This directory is not a Git repository.",
                                   "Go into the repository you want HowlPlane to work on, then run:"),
                {"state": "not_started"}, None)
    if blocked is not None or report["needs_preparation"] or report["factory"] == "blocked":
        return not_started_status(False), status, workers
    return not_started_status(True), status, workers


def _collect(args: argparse.Namespace):
    """(operator, status, workers_available) from durable state, or the not-started view."""
    campaign = _campaign(args)
    if not _has_state(campaign):
        return _not_started(args)
    factory_args = _factory_args(args)
    status, operator, _campaign_, _record, _published = _cli().collect_factory_status(factory_args)
    return operator, status, None


def _emit_json(operator: Any, status: Dict[str, Any]) -> None:
    print(json.dumps({**status, "operator": operator.to_dict()}, indent=2, default=str))


def cmd_home(args: argparse.Namespace) -> int:
    """Bare ``howlplane``: a safe, read-only, contextual home screen."""
    from howlplane.control_plane.presentation.operator import render_home_text
    operator, status, workers = _collect(args)
    follow_ups = ["howlplane status"] if operator.state == "not_started" else ["howlplane status", "howlplane logs"]
    if operator.label == "NOT READY":
        follow_ups = []
    print("\n".join(render_home_text(operator, status, _style(), workers, follow_ups)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """`howlplane status`: the operator view; `--verbose` keeps the project diagnostics."""
    if getattr(args, "verbose", False):
        return _cli().cmd_status(args)
    from howlplane.control_plane.presentation.operator import render_home_text, render_operator_text
    operator, status, workers = _collect(args)
    if getattr(args, "json", False):
        _emit_json(operator, status)
        return 0
    if operator.state == "not_started":
        print("\n".join(render_home_text(operator, status, _style(), workers)))
        return 0
    details = ["howlplane status --verbose",
               "howlplane logs --errors" if operator.severity.value in ("attention", "error")
               else "howlplane logs --follow"]
    print("\n".join(render_operator_text(operator, status, _style(), title="HowlPlane", details=details)))
    return 0


def _refuse_start(operator: Any, status: Dict[str, Any]) -> None:
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    raise OperatorFailure(OperatorError(
        "START_NEEDS_REVIEW", "HowlPlane did not restart on its own.",
        _stop_reason_text(status),
        "Look at what went wrong. When you are ready to continue, restart on purpose.",
        "howlplane start --retry"))


def _stop_reason_text(status: Dict[str, Any]) -> str:
    detail = status.get("last_error") or status.get("stopped_reason") or status.get("bounded_run_stop_reason")
    base = "It stopped after a problem rather than being paused."
    return f"{base} ({detail})" if detail else base


def _needs_deliberate_restart(status: Dict[str, Any]) -> bool:
    """A stopped Factory whose stop was a failure, not a pause or a normal bounded finish."""
    if str(status.get("state")) != "stopped":
        return False
    reason = status.get("stopped_reason") or status.get("bounded_run_stop_reason")
    if reason in _CONTINUABLE_STOP_REASONS:
        return False
    return bool(status.get("last_error") or reason)


def cmd_start(args: argparse.Namespace) -> int:
    """`howlplane start [OBJECTIVE]`: start, or continue after a stop, via the Factory start path."""
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    positional = getattr(args, "objective_text", None)
    flag = getattr(args, "objective", None)
    if positional and flag and positional != flag:
        raise OperatorFailure(OperatorError(
            "START_OBJECTIVE_CONFLICT", "Two different objectives were given.",
            "Pass the objective once, either as text or with --objective.",
            "Run it again with one objective.", 'howlplane start "your objective"'))
    factory_args = _factory_args(args, objective=positional or flag, preflight="off")
    campaign = _campaign(args)
    if _has_state(campaign) and not getattr(args, "retry", False):
        status, operator, _c, _r, _p = _cli().collect_factory_status(_factory_args(args))
        if _needs_deliberate_restart(status):
            _refuse_start(operator, status)
    report = _cli()._factory_readiness(workspace=_cli()._factory_workspace(factory_args))
    if report["readiness"]["status"] == "BLOCKED":
        gated = report["readiness"].get("workspace_trust_required")
        raise OperatorFailure(OperatorError(
            "START_NO_WORKER", "HowlPlane could not start.",
            "This repository has not been prepared for autonomous work." if gated
            else "No authenticated autonomous worker is currently available.",
            "Prepare this repository." if gated else "See which workers need attention.",
            "howlplane setup" if gated else "howlplane doctor --agents"))
    return _cli().cmd_factory_start(factory_args)


def cmd_logs(args: argparse.Namespace) -> int:
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    campaign = _campaign(args)
    if campaign is None:
        raise OperatorFailure(OperatorError(
            "LOGS_NO_REPOSITORY", "There are no logs to show here.",
            "This directory is not a Git repository HowlPlane has worked on.",
            "Go into the repository and set it up.", "howlplane setup"))
    return _cli().cmd_factory_logs(_factory_args(args))


def cmd_stop(args: argparse.Namespace) -> int:
    """`howlplane stop`: pause safely, preserving state. `howlplane start` continues."""
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    campaign = _campaign(args)
    if campaign is None or not _has_state(campaign):
        if campaign is None:
            raise OperatorFailure(OperatorError(
                "STOP_NO_REPOSITORY", "There is nothing to stop here.",
                "This directory is not a Git repository HowlPlane has worked on.",
                "Run it inside the repository.", "howlplane"))
        _cli()._print_factory_result("NOT RUNNING", "info", "HowlPlane has not been started in this repository.",
                                     [], "Start it when ready:", ["howlplane start"], title="HowlPlane")
        return 0
    return _cli().cmd_factory_stop(_factory_args(args))
