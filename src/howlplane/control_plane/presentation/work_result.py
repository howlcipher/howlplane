"""The ``howlplane.work_result/v1`` document printed by ``work --json`` and ``resume --json``.

An agent driving HowlPlane needs one stable answer to "what happened and what
do I run next", not the internal OrchestrationResult, whose fields move with
the orchestrator. This module is that contract. The terminal summary reads the
same role verdicts, so text and JSON cannot disagree about a review.

Additive changes keep v1. Removing or renaming a key, or changing its meaning,
needs v2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

WORK_RESULT_SCHEMA = "howlplane.work_result/v1"

VERDICT_PASS = "pass"
VERDICT_FINDINGS = "findings"
VERDICT_FAILED = "failed"
VERDICT_NOT_RUN = "not_run"


def role_verdicts(review_cycles: List[Any], roles: Iterable[str]) -> List[Dict[str, Any]]:
    """Each reviewer role's latest verdict across the task's review cycles.

    Re-review cycles only re-run roles that still had findings, so a role that
    came back clean earlier has its verdict in an earlier cycle; it is reported
    from there with ``earlier_cycle`` set rather than as not run.
    """
    if not review_cycles:
        return [_verdict(role, None, False, False) for role in roles]
    last = review_cycles[-1]
    non_independent = set(getattr(last, "non_independent_roles", None) or [])
    verdicts = []
    for role in roles:
        result, earlier = last.reviewer_results.get(role), False
        if result is None:
            for cycle in reversed(review_cycles[:-1]):
                if role in cycle.reviewer_results:
                    result, earlier = cycle.reviewer_results[role], True
                    break
        verdicts.append(_verdict(role, result, earlier, role in non_independent))
    return verdicts


def _verdict(role: str, result: Any, earlier: bool, self_review: bool) -> Dict[str, Any]:
    if result is None:
        verdict, findings = VERDICT_NOT_RUN, []
    elif result.status == "reviewer_failure":
        verdict, findings = VERDICT_FAILED, []
    elif result.findings:
        verdict, findings = VERDICT_FINDINGS, list(result.findings)
    else:
        verdict, findings = VERDICT_PASS, []
    return {
        "role": role,
        "verdict": verdict,
        "finding_count": len(findings),
        "highest_severity": findings[0].severity if findings else None,
        "earlier_cycle": earlier,
        "self_review": self_review,
    }


def _verification(plan: Any) -> Dict[str, Any]:
    if plan is None:
        return {"status": "none", "steps": []}
    return {
        "status": plan.overall_status,
        "steps": [
            {"name": s.name, "category": s.category, "status": s.status,
             "exit_code": s.exit_code, "required": s.required}
            for s in plan.steps
        ],
    }


def _follow_ups(run_dir: Optional[Path]) -> List[Dict[str, Any]]:
    path = run_dir / "follow_ups.yaml" if run_dir else None
    if not path or not path.is_file():
        return []
    try:
        items = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("follow_ups") or []
    except Exception:
        return []
    keys = ("id", "severity", "title", "reviewer_role", "location", "suggested_fix")
    return [{k: item.get(k) for k in keys} for item in items if isinstance(item, dict)]


def next_action(task_id: str, final_state: str, run_dir: Optional[Path]) -> Dict[str, Any]:
    """The command an operator or agent should run next, or why there is none."""
    if final_state == "awaiting_human":
        return {
            "command": f"howlplane approve {task_id}",
            "then": f"howlplane resume {task_id}",
            "alternatives": [f"howlplane reject {task_id}"],
            "reason": "OWNER_REQUIRED",
            "detail": f"Read {run_dir / 'decision_packet.md'} first." if run_dir else None,
        }
    if final_state == "complete":
        return {"command": None, "then": None, "alternatives": [], "reason": "COMPLETE",
                "detail": "Review the change in the working tree and commit it."}
    if final_state == "prepared":
        return {"command": None, "then": None, "alternatives": [], "reason": "PREPARED",
                "detail": "Run the same command with --execute to carry out the task."}
    return {"command": None, "then": None, "alternatives": [], "reason": final_state.upper(),
            "detail": f"See {run_dir} for the evidence." if run_dir else None}


def work_result_document(res: Any, roles: Iterable[str]) -> Dict[str, Any]:
    """The v1 document for a finished, parked or failed governed run."""
    run_dir = Path(res.run_dir) if getattr(res, "run_dir", None) else None
    packet = run_dir / "decision_packet.md" if run_dir else None
    delta = getattr(res, "final_delta", None)
    return {
        "schema": WORK_RESULT_SCHEMA,
        "task_id": res.task_id,
        "final_state": res.final_state,
        "exit_code": res.exit_code,
        "run_dir": str(run_dir) if run_dir else None,
        "implementer": getattr(res, "executing_provider", None),
        "changes": {
            "files_added": list(delta.files_added) if delta else [],
            "files_modified": list(delta.files_modified) if delta else [],
            "files_deleted": list(delta.files_deleted) if delta else [],
            "insertions": delta.insertions if delta else 0,
            "deletions": delta.deletions if delta else 0,
        },
        "reviews": role_verdicts(getattr(res, "review_cycles", None) or [], roles),
        "remediation_cycles": getattr(res, "remediation_cycles_count", 0),
        "verification": _verification(getattr(res, "verification_plan", None)),
        "follow_ups": _follow_ups(run_dir),
        "decision_packet": str(packet) if packet and packet.is_file() else None,
        "error": getattr(res, "error_message", None),
        "next_action": next_action(res.task_id, res.final_state, run_dir),
    }


def prepared_document(spec: Any, decision: Any, plan: Any, boundary: Any, run_dir: Path,
                      launch_command: str, exit_code: int) -> Dict[str, Any]:
    """The v1 document for ``work`` without ``--execute``: routed and planned, nothing run.

    Nothing is parked by a dry run, so the state is always ``prepared``; a
    boundary the planned actions would hit is reported, not acted on.
    """
    packet = run_dir / "decision_packet.md"
    return {
        "schema": WORK_RESULT_SCHEMA,
        "task_id": spec.task_id,
        "final_state": "prepared",
        "exit_code": exit_code,
        "run_dir": str(run_dir),
        "implementer": decision.selected_agent_id,
        "reviewers": list(decision.recommended_reviewers),
        "rationale": decision.rationale,
        "verification": _verification(plan),
        "requires_human_approval": bool(boundary.requires_human_approval),
        "boundaries": list(boundary.triggered_boundaries),
        "decision_packet": str(packet) if packet.is_file() else None,
        "launch_command": launch_command,
        "next_action": next_action(spec.task_id, "prepared", run_dir),
    }
