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
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from howlplane.control_plane.factory.status_publish import OWNER_REQUIRED
from howlplane.control_plane.factory.supervisor_state import SupervisorState
from howlplane.control_plane.presentation.style import Style, format_duration
from howlplane.control_plane.resource_models import ProviderFailureClass

OPERATOR_STATUS_SCHEMA = "howlplane.operator.status/v1"


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
    # Other real commands that are equally valid here (for example reject next to approve).
    alternatives: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class OperatorStatus:
    state: str
    label: str
    severity: Severity
    summary: str
    reason_code: ReasonCode = ReasonCode.NONE
    current_work: Optional[str] = None
    current_work_title: Optional[str] = None
    worker: Optional[str] = None
    blockers: List[str] = field(default_factory=list)
    owner_required: bool = False
    next_action: Optional[OperatorAction] = None
    schema: str = OPERATOR_STATUS_SCHEMA
    # Additive in v1. Only populated from durable state; never estimated.
    worker_resource_id: Optional[str] = None
    attempt: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    retry_in_seconds: Optional[int] = None
    recovery: Optional[str] = None  # "automatic" or "manual" when the state is a wait or failure

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["reason_code"] = self.reason_code.value
        return data


_WORKER_NAMES = {"claude_code": "Claude", "claude": "Claude", "codex": "Codex", "cursor": "Cursor",
                 "agy": "AGY", "devin_cli": "Devin", "devin": "Devin", "gemini": "Gemini"}


def worker_display(resource_id: Optional[str]) -> Optional[str]:
    """Friendly worker name; an unknown id is shown as-is rather than guessed."""
    if not resource_id:
        return None
    return _WORKER_NAMES.get(str(resource_id), str(resource_id))


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _runtime(status: Mapping[str, Any], state: str, now: Optional[datetime]) -> Dict[str, Any]:
    """Elapsed/retry/recovery facts that durable state truthfully supports."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out: Dict[str, Any] = {}
    resource = status.get("worker_resource_id")
    if resource:
        out["worker_resource_id"] = resource
        out["worker"] = status.get("worker") or worker_display(resource)
    if status.get("current_attempt") is not None:
        out["attempt"] = status["current_attempt"]
    started = _parse(status.get("dispatch_started_at"))
    if started and state == SupervisorState.DISPATCHING.value:
        out["elapsed_seconds"] = max(0, int((now - started).total_seconds()))
    wake = _parse(status.get("next_wake_at"))
    waiting = state in (SupervisorState.BACKOFF_AFTER_FAILURE.value, SupervisorState.WAITING_FOR_PROVIDER.value)
    if waiting and wake and wake > now:
        out["retry_in_seconds"] = int((wake - now).total_seconds())
    return out


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


def derive_operator_status(status: Mapping[str, Any], now: Optional[datetime] = None) -> OperatorStatus:
    """Project the ``factory status`` payload into the operator model."""
    state = str(getattr(status.get("state"), "value", status.get("state")) or "unknown")
    process = status.get("process")
    work = status.get("current_work_item_id")
    blockers = _blockers(status)
    runtime = _runtime(status, state, now)
    work_title = status.get("current_work_title")
    common = {"state": state, "current_work": work, "current_work_title": work_title,
              "worker": runtime.pop("worker", status.get("worker")), "blockers": blockers, **runtime}
    automatic = process in (None, "running")
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
        decisions = status.get("decisions") or []
        if decisions:
            first = decisions[0]
            action = OperatorAction(
                f"Review {first['work_item_id']} and decide:", first["approve"],
                alternatives=[first["reject"]] if first.get("reject") else [])
        else:
            action = OperatorAction("Review the items awaiting an owner decision.", "howlplane status")
        return OperatorStatus(
            label="OWNER REQUIRED", severity=Severity.ATTENTION,
            summary="Work is waiting for an owner decision.",
            reason_code=ReasonCode.OWNER_REQUIRED, owner_required=True, recovery="manual",
            next_action=action, **common)

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
            reason_code=ReasonCode.AUTHENTICATION_REQUIRED, owner_required=True, recovery="manual",
            next_action=OperatorAction(
                "Sign in to the provider CLI, then check readiness.", "howlplane agents doctor"),
            **common)
    if failure is ProviderFailureClass.SESSION_LIMIT or (
        state == SupervisorState.WAITING_FOR_PROVIDER.value
    ):
        return OperatorStatus(
            label="WAITING", severity=Severity.ATTENTION,
            summary="Waiting for a provider to become available.",
            recovery="automatic" if automatic else "manual",
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
            recovery="automatic" if automatic else "manual",
            reason_code=ReasonCode.BACKOFF_AFTER_FAILURE,
            next_action=OperatorAction("Inspect the failure.", "howlplane factory logs"),
            **common)
    if state == SupervisorState.WAITING_FOR_DEPENDENCY.value:
        return OperatorStatus(
            label="WAITING", severity=Severity.INFO,
            summary="Waiting for a dependency before continuing.",
            recovery="automatic" if automatic else "manual",
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


_HEALTH = {Severity.OK: "Healthy", Severity.INFO: "Healthy", Severity.ATTENTION: "Needs attention",
           Severity.ERROR: "Unhealthy"}


def _next_text(message: str) -> str:
    """Spell "None. ..." as the clearer "No action required. ..."."""
    if message.startswith("None"):
        rest = message[4:].lstrip(". ").strip()
        return "No action required." + (f" {rest}" if rest else "")
    return message


def render_operator_text(op: OperatorStatus, status: Mapping[str, Any], style: Optional[Style] = None) -> List[str]:
    """Human view: state -> meaning -> next action. Internals live under ``--verbose``.

    Every fact is spelled in words; ``style`` only adds emphasis, so the plain
    output (pipes, CI, NO_COLOR, screen readers) carries the same information.
    """
    style = style or Style()
    lines = style.header("HowlPlane Factory", op.label, op.severity.value)
    health_key = "Health"
    rows = [
        ("Project", status.get("project")),
        ("Authority", status.get("authority")),
        ("Objective", status.get("objective")),
        (health_key, _HEALTH[op.severity]),
        ("Worker", op.worker),
        ("Elapsed", format_duration(op.elapsed_seconds)),
        ("Attempt", op.attempt),
        ("Recovery", {"automatic": "Automatic", "manual": "Needs you"}.get(op.recovery or "")),
        ("Retry", f"in {format_duration(op.retry_in_seconds)}" if op.retry_in_seconds is not None else None),
        ("Completed", len(status.get("recent_completed") or [])),
        ("Failed", len(status.get("recent_failed") or [])),
    ]
    if op.severity is not Severity.OK or op.blockers:
        rows.insert(4, ("Reason", op.summary))
        if op.reason_code is not ReasonCode.NONE:
            rows.insert(5, ("Code", op.reason_code.value))
    lines += style.kv(rows, severities={health_key: op.severity.value})

    lines += ["", style.section("Current work"), op.current_work or "none"]
    if op.current_work_title:
        lines[-1] = f"{op.current_work}: {op.current_work_title}"
    for blocker in op.blockers:
        lines.append(f"  - {blocker}")

    lines += ["", style.section("Next")]
    action = op.next_action
    if action is None:
        lines.append("No action required.")
    else:
        lines.append(_next_text(action.message))
        if action.command:
            lines += ["", style.command_block(action.command)]
        for alt in action.alternatives:
            lines.append(style.command_block(alt))

    details = ["howlplane factory status --verbose"]
    details.append("howlplane factory logs --errors" if op.severity in (Severity.ATTENTION, Severity.ERROR)
                   else "howlplane factory logs --follow")
    lines += ["", style.muted("Details")] + [style.command_block(d) for d in details]
    return lines
