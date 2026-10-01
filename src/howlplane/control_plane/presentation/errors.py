"""Operator-facing error explanations: what happened, why, and what to do next.

`explain` maps exceptions HowlPlane already raises onto a stable code, a one
line reason and a recovery action. Only commands that exist are suggested. An
exception it does not recognize returns None so the CLI keeps its original
message; nothing is hidden or rewritten.
"""

import os
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from howlplane.control_plane.presentation.redact import redact_operator_text

ERROR_SCHEMA = "howlplane.error/v1"


@dataclass(frozen=True)
class OperatorError:
    code: str
    message: str
    why: str
    next_action: str
    command: Optional[str] = None
    schema: str = ERROR_SCHEMA

    diagnostic_id: Optional[str] = None
    expected_recovery: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("diagnostic_id", "expected_recovery"):
            if data[key] is None:
                del data[key]
        return data

    def render(self, style=None) -> str:
        """Plain text by default; ``style`` (presentation.style.Style) adds emphasis only."""
        def paint(kind, text):
            return style.paint(kind, text) if style else text
        lines = [paint("error", self.message), f"Why: {self.why}"]
        if self.expected_recovery:
            lines.append(f"Recovery: {self.expected_recovery}")
        lines.append(f"Next: {self.next_action}")
        if self.command:
            lines.append("  " + paint("command", self.command))
        if self.diagnostic_id:
            lines.append(f"Diagnostic ID: {self.diagnostic_id}")
        lines.append(f"Code: {self.code}")
        return "\n".join(lines)


class OperatorFailure(Exception):
    """An expected failure that already carries its canonical explanation."""

    def __init__(self, error: "OperatorError"):
        super().__init__(error.message)
        self.error = error


def redact_text(text: str) -> str:
    return redact_operator_text(text)


def diagnostics_dir() -> Path:
    base = os.environ.get("HOWLPLANE_DIAGNOSTICS_DIR")
    if base:
        return Path(base)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "howlplane" / "diagnostics"


def internal_error(exc: BaseException) -> "OperatorError":
    """Wrap an unexpected exception. The traceback is kept (redacted) in a diagnostics file."""
    diag = "HP-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    path = None
    try:
        directory = diagnostics_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{diag}.txt"
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        path.write_text(redact_text(trace), encoding="utf-8")
    except OSError:
        path = None
    where = f"Traceback saved to {path}." if path else "The traceback could not be saved."
    return OperatorError(
        "INTERNAL_ERROR", f"Something unexpected failed: {redact_text(str(exc)) or type(exc).__name__}",
        f"This is likely a HowlPlane bug or an unhandled condition ({type(exc).__name__}). {where}",
        "Re-run with --debug to print the traceback, and include the diagnostic ID in a bug report.",
        None, diagnostic_id=diag)


def _config_error(exc: BaseException) -> Optional["OperatorError"]:
    try:
        from pydantic import ValidationError
    except ImportError:  # pragma: no cover
        return None
    if not isinstance(exc, ValidationError):
        return None
    problems = []
    for item in exc.errors():
        name = ".".join(str(p) for p in item.get("loc", ())) or "setting"
        ctx = item.get("ctx") or {}
        expected = ctx.get("expected")
        got = item.get("input")
        text = f"{name}: {item.get('msg', 'invalid value')}"
        if got is not None and not isinstance(got, (dict, list)):
            text += f" (got {got!r})"
        if expected and expected not in text:
            text += f" Expected {expected}."
        problems.append(text)
    shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
    names = ", ".join(sorted({(i.get("loc") or ("setting",))[0] for i in exc.errors()}))
    return OperatorError(
        "INVALID_CONFIGURATION", f"Invalid configuration: {names}.", shown,
        "Fix the setting (environment variable, config/settings.yaml or ~/.config/howlplane/config.toml).",
        "howlplane config validate")


def _by_message(text: str) -> Optional[tuple]:
    upper = text.upper()
    if "AUTHENTICATION_REQUIRED" in upper:
        return ("AUTHENTICATION_REQUIRED", "A provider CLI is not signed in.",
                "Sign in to that provider's CLI, then re-check readiness.", "howlplane agents doctor")
    if "SESSION_LIMIT" in upper:
        return ("SESSION_LIMIT", "A provider hit its session limit.",
                "Nothing required if another worker is available; otherwise wait for the limit to reset.",
                "howlplane agents doctor")
    if "WORKSPACE TRUST REQUIRED" in upper or "WORKSPACE_TRUST_REQUIRED" in upper:
        return ("WORKSPACE_TRUST_REQUIRED", "A worker would meet a trust prompt in this workspace, so it was not used.",
                "Prepare the repository for unattended use.", "howlplane factory prepare")
    return None


def explain(exc: BaseException) -> Optional[OperatorError]:
    text = str(exc)
    if isinstance(exc, OperatorFailure):
        return exc.error
    config_err = _config_error(exc)
    if config_err:
        return config_err
    from howlplane.control_plane import cli, human_boundary, locking, workspace_trust
    from howlplane.control_plane.agent_execution import AgentUnavailableError
    from howlplane.control_plane.factory.campaign import CampaignError

    if isinstance(exc, cli.TargetRepositoryNotFoundError):
        return OperatorError("NOT_A_GIT_REPOSITORY", text, "HowlPlane operates on a Git repository.",
                             "Run it inside a Git repository, or pass --repo PATH.")
    if isinstance(exc, workspace_trust.PreparationRefused):
        return OperatorError("WORKSPACE_TRUST_REQUIRED", text,
                             "Workspace trust was not authorized for this repository.",
                             "Authorize and prepare the repository.", "howlplane factory prepare")
    if isinstance(exc, (locking.RepositoryLockedError, locking.TaskLockedError)):
        return OperatorError("LOCKED", text, "Another run currently holds the lock.",
                             "See who holds it. Use unlock only if its owner is gone.", "howlplane status")
    if isinstance(exc, human_boundary.ApprovalRequiredError):
        return OperatorError("OWNER_REQUIRED", text, "This step needs an owner decision.",
                             "List tasks awaiting approval, then approve or reject the task.", "howlplane status")
    if isinstance(exc, (human_boundary.StaleApprovalError, human_boundary.RepositoryDriftError)):
        return OperatorError("APPROVAL_STALE", text, "The repository changed after the approval was recorded.",
                             "Review the change, then approve the task again (howlplane approve TASK_ID).",
                             "howlplane status")
    if isinstance(exc, AgentUnavailableError):
        return OperatorError("PROVIDER_UNAVAILABLE", text, "The selected worker could not be used.",
                             "Check which workers are ready.", "howlplane agents doctor")
    by_text = _by_message(text)
    if by_text:
        code, why, nxt, cmd = by_text
        return OperatorError(code, text, why, nxt, cmd)
    if isinstance(exc, CampaignError):
        return OperatorError("CAMPAIGN_ERROR", text, "The Factory cannot safely use this repository as configured.",
                             "Run the Factory readiness check.", "howlplane factory doctor")
    if isinstance(exc, ValueError) and "workspace trust policy" in text:
        return OperatorError("INVALID_CONFIGURATION", text, "The workspace trust policy value is not recognized.",
                             "Use one of the listed policies in --workspace-trust or HOWLPLANE_WORKSPACE_TRUST.",
                             "howlplane config validate")
    if isinstance(exc, cli.ControlPlaneError):
        return OperatorError("CONTROL_PLANE_ERROR", text, "HowlPlane could not complete the request.",
                             "Check the message above and the readiness of this repository.", "howlplane doctor")
    return None
