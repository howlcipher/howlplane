"""Canonical operator status model.

One pure projection from the Factory status payload to what an operator needs
to know: overall state, health, why, and the next action. The CLI text, the
``--json`` output and the redacted remote snapshot all derive from it so they
cannot disagree.

This module reads no files and runs no commands. It reuses the existing
classifications (``SupervisorState``, ``ProviderFailureClass``, the snapshot
``OWNER_REQUIRED`` blocker class) instead of defining a second taxonomy; the
only new codes name conditions that previously had no stable token.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from howlplane.control_plane.factory.supervisor_state import SupervisorState
from howlplane.control_plane.resource_models import ProviderFailureClass

OPERATOR_STATUS_SCHEMA = "howlplane.operator.status/v1"

from howlplane.control_plane.factory.status_publish import OWNER_REQUIRED


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    ATTENTION = "attention"
    ERROR = "error"


class ReasonCode(str, Enum):
    """Stable machine-readable reasons. Values are a v1 contract; do not rename."""

    NONE = "NONE"
    OWNER_REQUIRED = OWNER_REQUIRED
    AUTHORITY_NOT_CONFIGURED = "AUTHORITY_NOT_CONFIGURED"
    AUTHENTICATION_REQUIRED = ProviderFailureClass.AUTHENTICATION_REQUIRED.value
    SESSION_LIMIT = ProviderFailureClass.SESSION_LIMIT.value
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    VERIFICATION_FAILURE = ProviderFailureClass.VERIFICATION_FAILURE.value
    FACTORY_FAILURE = "FACTORY_FAILURE"
    WAITING_FOR_WORK = "WAITING_FOR_WORK"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    BACKOFF_AFTER_FAILURE = "BACKOFF_AFTER_FAILURE"
    STALE_STATE = "STALE_STATE"
    STOPPED_BY_OPERATOR = "STOPPED_BY_OPERATOR"
    STOPPED = "STOPPED"
    BOUNDED_RUN_COMPLETE = "BOUNDED_RUN_COMPLETE"
    NOT_STARTED = "NOT_STARTED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    SNAPSHOT_ABSENT = "SNAPSHOT_ABSENT"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"


@dataclass(frozen=True)
class OperatorAction:
    """What to do next. ``command`` is only ever a real ``howlplane`` command."""

    message: str
    command: Optional[str] = None


@dataclass(frozen=True)
class OperatorStatus:
    state: str
    label: str
    severity: Severity
    summary: str
    reason_code: ReasonCode = ReasonCode.NONE
    current_work: Optional[str] = None
    worker: Optional[str] = None
    blockers: List[str] = field(default_factory=list)
    owner_required: bool = False
    next_action: Optional[OperatorAction] = None
    schema: str = OPERATOR_STATUS_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["reason_code"] = self.reason_code.value
        return data


def _classified_failure(status: Mapping[str, Any]) -> Optional[ProviderFailureClass]:
    haystack = f"{status.get('last_error') or ''} {status.get('provider_wake_conditions') or ''}".upper()
    for failure in ProviderFailureClass:
        if failure.value in haystack:
            return failure
    return None


def _blockers(status: Mapping[str, Any]) -> List[str]:
    blockers = []
    for item in status.get("parked_items") or []:
        reason = item.get("blocker") or item.get("state")
        blockers.append(f"{item.get('work_item_id')}: {reason}")
    for proposal in status.get("proposals_awaiting_authority") or []:
        blockers.append(f"proposal {proposal.get('proposal_id')}: {proposal.get('disposition')}")
    return blockers


def _owner_items(status: Mapping[str, Any]) -> bool:
    if status.get("proposals_awaiting_authority"):
        return True
    return any(i.get("state") == "awaiting_owner" for i in status.get("parked_items") or [])


def derive_operator_status(status: Mapping[str, Any]) -> OperatorStatus:
    """Project the ``factory status`` payload into the operator model."""
    state = str(getattr(status.get("state"), "value", status.get("state")) or "unknown")
    process = status.get("process")
    work = status.get("current_work_item_id")
    blockers = _blockers(status)
    common = {"state": state, "current_work": work, "worker": status.get("worker"), "blockers": blockers}
    failure = _classified_failure(status)
    not_configured = status.get("authority") == "not configured"

    if _owner_items(status) or state == SupervisorState.WAITING_FOR_AUTHORITY.value:
        if not_configured and not _owner_items(status):
            return OperatorStatus(
                label="OWNER REQUIRED", severity=Severity.ATTENTION,
                summary="No authority level is configured, so authority-required work is parked.",
                reason_code=ReasonCode.AUTHORITY_NOT_CONFIGURED, owner_required=True,
                next_action=OperatorAction(
                    "Choose an authority level and restart the Factory.",
                    "howlplane factory start --authority safe"),
                **common)
        return OperatorStatus(
            label="OWNER REQUIRED", severity=Severity.ATTENTION,
            summary="Work is waiting for an owner decision.",
            reason_code=ReasonCode.OWNER_REQUIRED, owner_required=True,
            next_action=OperatorAction(
                "Review the items awaiting an owner decision.", "howlplane status"),
            **common)

    if process == "stale":
        return OperatorStatus(
            label="STALE", severity=Severity.ERROR,
            summary="The Factory process record says it is running but the process is gone.",
            reason_code=ReasonCode.STALE_STATE,
            next_action=OperatorAction("Restart the Factory.", "howlplane factory start"),
            **common)

    if state == SupervisorState.STOPPED.value:
        reason = status.get("stopped_reason") or ""
        if status.get("bounded_run_stop_reason") or status.get("bounded_run_completed_at"):
            return OperatorStatus(
                label="COMPLETE", severity=Severity.INFO,
                summary="The bounded run finished.", reason_code=ReasonCode.BOUNDED_RUN_COMPLETE,
                next_action=OperatorAction("Start another run when ready.", "howlplane factory start"),
                **common)
        if status.get("last_error") and reason != "operator_stop":
            return OperatorStatus(
                label="FAILED", severity=Severity.ERROR,
                summary=f"The Factory stopped after an error: {status['last_error']}",
                reason_code=ReasonCode.FACTORY_FAILURE,
                next_action=OperatorAction(
                    "Inspect the logs, then resume.", "howlplane factory logs"),
                **common)
        code = ReasonCode.STOPPED_BY_OPERATOR if reason == "operator_stop" else ReasonCode.STOPPED
        return OperatorStatus(
            label="STOPPED", severity=Severity.INFO,
            summary="The Factory is stopped." if not reason else f"The Factory is stopped ({reason}).",
            reason_code=code,
            next_action=OperatorAction("Resume the Factory.", "howlplane factory resume"),
            **common)

    if failure is ProviderFailureClass.AUTHENTICATION_REQUIRED:
        return OperatorStatus(
            label="ATTENTION", severity=Severity.ATTENTION,
            summary="A provider needs you to sign in again.",
            reason_code=ReasonCode.AUTHENTICATION_REQUIRED, owner_required=True,
            next_action=OperatorAction(
                "Sign in to the provider CLI, then check readiness.", "howlplane agents doctor"),
            **common)
    if failure is ProviderFailureClass.SESSION_LIMIT or (
        state == SupervisorState.WAITING_FOR_PROVIDER.value
    ):
        return OperatorStatus(
            label="WAITING", severity=Severity.ATTENTION,
            summary="Waiting for a provider to become available.",
            reason_code=(ReasonCode.SESSION_LIMIT if failure is ProviderFailureClass.SESSION_LIMIT
                         else ReasonCode.PROVIDER_UNAVAILABLE),
            next_action=OperatorAction(
                "Nothing required; the Factory resumes when the provider recovers. "
                "Check provider readiness for details.", "howlplane agents doctor"),
            **common)
    if failure is ProviderFailureClass.VERIFICATION_FAILURE:
        return OperatorStatus(
            label="ATTENTION", severity=Severity.ATTENTION,
            summary="Verification failed for the current work.",
            reason_code=ReasonCode.VERIFICATION_FAILURE,
            next_action=OperatorAction("Inspect the details.", "howlplane factory status --verbose"),
            **common)
    if state == SupervisorState.BACKOFF_AFTER_FAILURE.value:
        return OperatorStatus(
            label="BACKING OFF", severity=Severity.ATTENTION,
            summary="The last attempt failed; the Factory will retry automatically.",
            reason_code=ReasonCode.BACKOFF_AFTER_FAILURE,
            next_action=OperatorAction("Inspect the failure.", "howlplane factory logs"),
            **common)
    if state == SupervisorState.WAITING_FOR_DEPENDENCY.value:
        return OperatorStatus(
            label="WAITING", severity=Severity.INFO,
            summary="Waiting for a dependency before continuing.",
            reason_code=ReasonCode.WAITING_FOR_DEPENDENCY,
            next_action=OperatorAction("None. The Factory continues automatically."), **common)
    if state == SupervisorState.WAITING_FOR_WORK.value or state == SupervisorState.IDLE.value:
        return OperatorStatus(
            label="IDLE", severity=Severity.OK,
            summary="No work is ready. The Factory is waiting for new work.",
            reason_code=ReasonCode.WAITING_FOR_WORK,
            next_action=OperatorAction("None. Add Pending backlog items to give it work."), **common)
    if state == SupervisorState.DISPATCHING.value:
        return OperatorStatus(
            label="RUNNING", severity=Severity.OK, summary="The Factory is working normally.",
            next_action=OperatorAction("None. The Factory is working normally."), **common)
    return OperatorStatus(
        label=state.upper(), severity=Severity.INFO, summary=f"Factory state: {state}.",
        next_action=OperatorAction("Inspect the details.", "howlplane factory status --verbose"),
        **common)


def render_operator_text(op: OperatorStatus, status: Mapping[str, Any]) -> List[str]:
    """Concise human view. Internal IDs and timestamps live under ``--verbose``."""
    lines = [f"HowlPlane Factory — {op.label}", ""]
    if status.get("project"):
        lines.append(f"Project: {status['project']}")
    if status.get("authority"):
        lines.append(f"Authority: {status['authority']}")
    if status.get("objective"):
        lines.append(f"Objective: {status['objective']}")
    lines.append(f"Current work: {op.current_work or 'none'}")
    if op.worker:
        lines.append(f"Worker: {op.worker}")
    health = {Severity.OK: "Healthy", Severity.INFO: "Healthy", Severity.ATTENTION: "Needs attention",
              Severity.ERROR: "Unhealthy"}[op.severity]
    lines.append(f"Health: {health}")
    lines.append(f"Completed: {len(status.get('recent_completed') or [])}")
    lines.append(f"Failed: {len(status.get('recent_failed') or [])}")
    if op.severity is not Severity.OK or op.blockers:
        lines.append(f"Reason: {op.summary}")
    for blocker in op.blockers:
        lines.append(f"  - {blocker}")
    lines.append("")
    action = op.next_action
    if action is None or (action.command is None and action.message.startswith("None")):
        lines.append(f"Next action: {action.message if action else 'None'}")
    elif action.command:
        lines.append(f"Next action: {action.message}")
        lines.append(f"  {action.command}")
    else:
        lines.append(f"Next action: {action.message}")
    lines += ["", "Details:", "  howlplane factory status --verbose", "  howlplane factory logs --follow"]
    return lines
