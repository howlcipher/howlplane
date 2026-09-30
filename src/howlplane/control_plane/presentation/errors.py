"""Operator-facing error explanations: what happened, why, and what to do next.

`explain` maps exceptions HowlPlane already raises onto a stable code, a one
line reason and a recovery action. Only commands that exist are suggested. An
exception it does not recognize returns None so the CLI keeps its original
message; nothing is hidden or rewritten.
"""

from dataclasses import asdict, dataclass
from typing import Optional

ERROR_SCHEMA = "howlplane.error/v1"


@dataclass(frozen=True)
class OperatorError:
    code: str
    message: str
    why: str
    next_action: str
    command: Optional[str] = None
    schema: str = ERROR_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        lines = [self.message, f"Why: {self.why}", f"Next: {self.next_action}"]
        if self.command:
            lines.append(f"  {self.command}")
        lines.append(f"Code: {self.code}")
        return "\n".join(lines)


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
    return None
