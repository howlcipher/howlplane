#!/usr/bin/env python3
"""Factory-native owner decisions: `howlplane factory approve|reject|retry|pending`.

The CLI handlers in ``cli.py`` stay thin; this module owns WorkItem lookup,
state validation, owner-decision persistence, content-bound mission
authorization, and the bridge to a parked orchestration task.

An approval never re-runs work inside the CLI. For an item parked on an
orchestration human boundary it records the task-level approval and points the
WorkItem at that same run, so the supervisor's next dispatch resumes it
instead of starting a duplicate.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from howlplane.control_plane.factory.mission_authorization import (
    AUTHORIZATION_ORIGIN_OWNER,
    CONTENT_CHANGED,
    UNTRUSTED_ROOT,
    MissionAuthorizationStore,
    compute_mission_digest,
    mission_number,
)
from howlplane.control_plane.factory.owner_decision import (
    OwnerDecisionStore,
    record_owner_decision,
)
from howlplane.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
    WorkItemStore,
)

IMPLEMENTATION_NO_CHANGES = "implementation_no_changes"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_RETRY = "retry"

# States an owner can see in `factory pending` / `factory status`.
PARKED_STATES = (
    WorkItemState.AWAITING_OWNER,
    WorkItemState.BLOCKED,
    WorkItemState.DEFERRED,
    WorkItemState.FAILED,
)

_SPECULATIVE_ORIGINS = {
    WorkItemOrigin.INFERRED_IMPROVEMENT.value,
    WorkItemOrigin.INFERRED_NEED.value,
    WorkItemOrigin.CREATIVE_EXPERIMENT.value,
    WorkItemOrigin.SPECULATIVE_IDEA.value,
}

_TRIGGER_LINE = re.compile(r"^-\s*(?:⚠️\s*)?\*\*([^*]+)\*\*:")


class OwnerDecisionError(ValueError):
    """An owner decision was refused; nothing was persisted."""


# ---------------------------------------------------------------------------
# Parked orchestration evidence
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def describe_parked_orchestration(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read-only summary of why an orchestration run parked awaiting a human.

    Built from the run's own durable artifacts (decision packet, implementation
    result and diff, reviewer attempt records), so the Factory can say exactly
    which boundary stopped the work instead of `awaiting_human`.
    """
    run_dir = Path(run_dir)
    packet_file = run_dir / "decision_packet.md"
    if not packet_file.is_file():
        return None
    try:
        packet = packet_file.read_text(encoding="utf-8")
    except OSError:
        return None

    triggers: List[str] = []
    recommended: Optional[str] = None
    lines = packet.splitlines()
    for index, line in enumerate(lines):
        match = _TRIGGER_LINE.match(line.strip())
        if match and match.group(1).strip() not in triggers:
            triggers.append(match.group(1).strip())
        if line.strip() == "## Recommended Action":
            for following in lines[index + 1:]:
                if following.strip():
                    recommended = following.strip()
                    break

    implementation_dir = run_dir / "implementation"
    diff_file = implementation_dir / "diff.patch"
    no_changes = diff_file.is_file() and not diff_file.read_text(
        encoding="utf-8", errors="replace"
    ).strip()
    implementer_result = _read_json(implementation_dir / "result.json")
    implementer_note = (implementer_result.get("stdout") or "").strip().splitlines()
    reviewer_failures: List[str] = []
    reviews_dir = run_dir / "reviews"
    if reviews_dir.is_dir():
        for role_dir in sorted(p for p in reviews_dir.iterdir() if p.is_dir()):
            attempts_dir = role_dir / "attempts"
            if not attempts_dir.is_dir():
                continue
            failures = []
            succeeded = False
            for attempt in sorted(p for p in attempts_dir.iterdir() if p.is_dir()):
                result = _read_json(attempt / "result.json")
                outcome = result.get("outcome") or "unknown"
                resource = attempt.name.split("-", 1)[-1]
                if outcome == "completed":
                    succeeded = True
                else:
                    failures.append(f"{resource} {result.get('failure_class') or outcome}")
            if failures and not succeeded:
                reviewer_failures.append(f"{role_dir.name}: {', '.join(failures)}")

    return {
        "orchestration_task_id": run_dir.name,
        "run_dir": str(run_dir),
        "target_repo": str(run_dir.parent.parent),
        "triggers": triggers,
        "recommended_action": recommended,
        "implementation_no_changes": bool(no_changes),
        "implementer": implementer_result.get("agent_id"),
        "implementer_note": implementer_note[0][:240] if implementer_note else None,
        "reviewer_failures": reviewer_failures,
    }


def format_human_boundary(boundary: Dict[str, Any]) -> str:
    """One-line operator explanation of a parked orchestration boundary."""
    triggers = boundary.get("triggers") or ["awaiting_human"]
    parts = [f"human_boundary:{','.join(triggers)}"]
    if boundary.get("implementation_no_changes"):
        implementer = boundary.get("implementer") or "implementer"
        parts.append(f"{IMPLEMENTATION_NO_CHANGES} ({implementer})")
    for failure in boundary.get("reviewer_failures") or []:
        parts.append(f"reviewer failed: {failure}")
    parts.append(f"task {boundary.get('orchestration_task_id')}")
    if boundary.get("run_dir"):
        parts.append(f"run_dir {boundary['run_dir']}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Reasons and actions shown to the owner
# ---------------------------------------------------------------------------

def parked_reason_and_action(
    item: WorkItem,
    boundary: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """(reason, exact CLI command) for a parked WorkItem, from recorded evidence only.

    ``boundary`` lets a caller supply a boundary read from a legacy run
    directory for items parked before the boundary was recorded on the item.
    Unknown reasons are reported as ``unclassified:<raw>``, never guessed.
    """
    wid = item.work_item_id
    approve = f"howlplane factory approve {wid}"
    retry = f"howlplane factory retry {wid}"
    raw = item.admission_blocked_reason
    boundary = item.human_boundary or boundary

    if item.state == WorkItemState.AWAITING_OWNER:
        if boundary:
            reason = format_human_boundary(boundary)
            if boundary.get("implementation_no_changes"):
                # Approving would authorize integration of nothing.
                return reason, retry
            return reason, approve
        if item.mission_path or item.kind == "mission":
            if raw and (raw.startswith("successor_") or "successor" in raw):
                return f"untrusted_successor_mission ({raw})", approve
            if raw in (None, "", UNTRUSTED_ROOT, "unauthorized_root_mission"):
                return UNTRUSTED_ROOT, approve
            return raw, approve
        if item.origin in _SPECULATIVE_ORIGINS:
            return raw or f"{item.origin}_requires_owner_review", approve
        if raw:
            return raw, approve
        last = item.reopening_history[-1].get("reason") if item.reopening_history else None
        return f"unclassified:{last or 'no recorded reason'}", approve

    if item.state == WorkItemState.FAILED:
        return raw or "unclassified:failed", retry
    if item.state == WorkItemState.BLOCKED:
        blockers = ", ".join(item.blocked_by) if item.blocked_by else None
        reason = raw or (f"blocked_by:{blockers}" if blockers else "unclassified:blocked")
        return reason, None
    if item.state == WorkItemState.DEFERRED:
        reason = raw or "deferred"
        if item.retry_after:
            reason = f"{reason} (retry after {item.retry_after})"
        return reason, None
    return raw or item.state, None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _mission_path_from_item(item: WorkItem) -> Optional[str]:
    """Recover a numbered mission path from an item admitted before mission_path existed."""
    if item.mission_path:
        return item.mission_path
    candidates = list(item.evidence_refs or [])
    for fingerprint in item.evidence_fingerprints or []:
        if not str(fingerprint).startswith("mission:"):
            continue
        rest = str(fingerprint)[len("mission:"):]
        path, sep, tail = rest.rpartition(":")
        if sep and len(tail) == 64 and all(c in "0123456789abcdef" for c in tail):
            candidates.append(path)
        else:
            candidates.append(rest)
    candidates.extend(str(key) for key in (getattr(item, "identity_keys", None) or []))
    for candidate in candidates:
        if mission_number(candidate) is not None:
            return candidate
    return None


def _load_item(store: WorkItemStore, work_item_id: str) -> WorkItem:
    try:
        return store.load(work_item_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise OwnerDecisionError(f"WorkItem {work_item_id} not found") from exc


def _mission_digest_for_approval(item: WorkItem, mission_roots: List[Path]) -> str:
    """The content identity this approval binds to; refuses anything unverifiable.

    ``mission_roots`` are the checkouts discovery may have read the mission
    from, in discovery order (managed execution worktree first). An item that
    already recorded a digest must still match it somewhere; a legacy item
    admitted before digests were recorded binds to the first readable copy.
    """
    if not mission_roots:
        raise OwnerDecisionError(
            f"Cannot verify mission content for {item.work_item_id}: repository "
            f"{item.repository} is not resolvable from this campaign"
        )
    seen = []
    for root in mission_roots:
        current = compute_mission_digest(Path(root) / item.mission_path)
        if not current:
            continue
        if not item.mission_digest or item.mission_digest == current:
            return current
        seen.append(str(root))
    if seen:
        raise OwnerDecisionError(
            f"{CONTENT_CHANGED}: {item.mission_path} in {', '.join(seen)} no longer matches "
            "the content this WorkItem was admitted with. The Factory will re-admit it "
            "with the new content; review and approve that version instead."
        )
    raise OwnerDecisionError(
        f"Mission file {item.mission_path} is missing from "
        f"{', '.join(str(r) for r in mission_roots)}"
    )


def apply_owner_decision(
    *,
    work_item_store: WorkItemStore,
    owner_decision_store: OwnerDecisionStore,
    mission_authorization_store: MissionAuthorizationStore,
    work_item_id: str,
    decision: str,
    reason: Optional[str] = None,
    mission_roots: Optional[List[Path]] = None,
    operator_source: str = "cli",
    lifecycle: Any = None,
) -> Dict[str, Any]:
    """Apply one owner decision to exactly one Factory WorkItem.

    * ``approved``: AWAITING_OWNER -> READY. A mission approval is bound to
      the mission's content digest; an orchestration-boundary approval records
      the task-level approval and marks the same run for resumption.
    * ``rejected``: AWAITING_OWNER -> REJECTED.
    * ``retry``: AWAITING_OWNER/FAILED/BLOCKED -> READY for a fresh governed
      attempt of the same WorkItem (used when the parked run has nothing worth
      resuming, e.g. an implementation that produced no changes).

    Every refusal raises :class:`OwnerDecisionError` before anything is written.
    """
    if decision not in (DECISION_APPROVED, DECISION_REJECTED, DECISION_RETRY):
        raise OwnerDecisionError(f"Unknown owner decision {decision!r}")
    item = _load_item(work_item_store, work_item_id)
    allowed = (
        (WorkItemState.AWAITING_OWNER, WorkItemState.FAILED, WorkItemState.BLOCKED)
        if decision == DECISION_RETRY
        else (WorkItemState.AWAITING_OWNER,)
    )
    if item.state not in allowed:
        raise OwnerDecisionError(
            f"WorkItem {work_item_id} is '{item.state}'; {decision} requires "
            f"{' or '.join(str(getattr(s, 'value', s)) for s in allowed)}"
        )

    boundary = item.human_boundary or None
    mission_digest: Optional[str] = None
    task_decision = None
    if decision == DECISION_APPROVED and not item.mission_path:
        recovered = _mission_path_from_item(item)
        if recovered:
            item.mission_path = recovered
    if decision == DECISION_APPROVED:
        if item.mission_path:
            mission_digest = _mission_digest_for_approval(item, list(mission_roots or []))
        if boundary and boundary.get("implementation_no_changes"):
            raise OwnerDecisionError(
                f"{work_item_id} parked after an implementation that produced no changes; "
                f"there is nothing to approve. Run: howlplane factory retry {work_item_id}"
            )
        if boundary and boundary.get("orchestration_task_id") and boundary.get("target_repo"):
            if lifecycle is None:
                from howlplane.control_plane.human_boundary import HumanLifecycleManager
                lifecycle = HumanLifecycleManager
            try:
                task_decision = lifecycle.approve(
                    target_repo=boundary["target_repo"],
                    task_id=boundary["orchestration_task_id"],
                    reason=reason,
                    operator_source=f"factory_owner_approval:{operator_source}",
                )
            except Exception as exc:  # surfaced, never swallowed
                raise OwnerDecisionError(
                    f"Orchestration task {boundary['orchestration_task_id']} could not be "
                    f"approved: {type(exc).__name__}: {exc}"
                ) from exc

    record = record_owner_decision(
        store=owner_decision_store,
        work_item_id=item.work_item_id,
        repository=item.repository,
        decision=decision,
        reason=reason,
        evidence_fingerprint=item.fingerprint,
        evidence_fingerprints=list(item.evidence_fingerprints),
        mission_digest=mission_digest or item.mission_digest,
        mission_path=item.mission_path,
        orchestration_task_id=(boundary or {}).get("orchestration_task_id"),
        prior_state=str(item.state),
        prior_blocked_reason=item.admission_blocked_reason,
        operator_source=operator_source,
    )
    item.owner_decision_id = record.decision_id
    note = reason or "no reason given"

    if decision == DECISION_APPROVED:
        if item.mission_path and mission_digest:
            mission_authorization_store.authorize(
                repository=item.repository,
                mission_path=item.mission_path,
                mission_digest=mission_digest,
                work_item_id=item.work_item_id,
                origin=AUTHORIZATION_ORIGIN_OWNER,
                owner_decision_id=record.decision_id,
            )
            item.mission_digest = mission_digest
            item.trusted_provenance = True
        if boundary and boundary.get("orchestration_task_id"):
            item.resume_orchestration_task_id = boundary["orchestration_task_id"]
        item.admission_blocked_reason = None
        item.transition_to(WorkItemState.READY, reason=f"owner_approved:{record.decision_id}:{note}")
    elif decision == DECISION_REJECTED:
        item.transition_to(WorkItemState.REJECTED, reason=f"owner_rejected:{record.decision_id}:{note}")
    else:
        item.human_boundary = None
        item.resume_orchestration_task_id = None
        item.blocked_by = []
        item.retry_after = None
        item.admission_blocked_reason = None
        item.transition_to(WorkItemState.READY, reason=f"owner_retry:{record.decision_id}:{note}")

    work_item_store.save_object(item)
    return {
        "work_item_id": item.work_item_id,
        "repository": item.repository,
        "decision": decision,
        "state": str(item.state),
        "owner_decision_id": record.decision_id,
        "mission_path": item.mission_path,
        "mission_digest": item.mission_digest,
        "resume_orchestration_task_id": item.resume_orchestration_task_id,
        "orchestration_decision": getattr(task_decision, "decision", None),
    }
