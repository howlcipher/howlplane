"""The ``howlplane.work_result/v1`` document printed by ``work --json`` and ``resume --json``.

An agent driving HowlPlane needs one stable answer to "what happened and what
do I run next", not the internal OrchestrationResult, whose fields move with
the orchestrator. This module is that contract. The terminal summary reads the
same role verdicts, so text and JSON cannot disagree about a review.

Additive changes keep v1. Removing or renaming a key, or changing its meaning,
needs v2.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

import yaml

WORK_RESULT_SCHEMA = "howlplane.work_result/v1"

VERDICT_PASS = "pass"
VERDICT_FINDINGS = "findings"
VERDICT_FAILED = "failed"
VERDICT_NOT_RUN = "not_run"
# The reviewer said it finished, but its findings file cannot say what it found.
VERDICT_UNRECORDED = "findings_unrecorded"

_SEVERITY_ORDER = ("blocker", "high", "medium", "low", "informational")


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
    # `findings is None` means the findings were never recorded (resume only); it is not "no findings".
    unrecorded = result is not None and result.findings is None
    findings = [] if result is None or result.status == "reviewer_failure" else list(result.findings or [])
    if result is None:
        verdict = VERDICT_NOT_RUN
    elif result.status == "reviewer_failure":
        verdict = VERDICT_FAILED
    elif findings or (unrecorded and result.status == "findings_detected"):
        verdict = VERDICT_FINDINGS
    elif unrecorded:
        verdict = VERDICT_UNRECORDED
    else:
        verdict = VERDICT_PASS
    return {
        "role": role,
        "verdict": verdict,
        "finding_count": None if unrecorded else len(findings),
        "highest_severity": min((f.severity for f in findings), key=_SEVERITY_ORDER.index, default=None),
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
             "exit_code": s.exit_code, "required": s.required,
             "failure_class": (s.metadata or {}).get("failure_class"),
             "timeout_seconds": s.effective_timeout_seconds}
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


def _seconds(value: Any) -> Optional[float]:
    """A recorded duration, or None. A boolean is never a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _total(values: List[Optional[float]]) -> Optional[float]:
    """The sum of the durations actually recorded, or None when none were."""
    recorded = [v for v in values if v is not None]
    return round(sum(recorded), 3) if recorded else None


def _read_json(path: Optional[Path]) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path else None


def _read_findings(path: Optional[Path]) -> Optional[List[Any]]:
    """The findings a reviewer recorded, or None when the file is missing, empty or corrupt."""
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entries = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return None
    out = []
    for entry in entries:
        severity = str(entry.get("severity") or "medium").lower().strip()
        out.append(SimpleNamespace(severity=severity if severity in _SEVERITY_ORDER else "medium"))
    return out


def _read_verification(path: Optional[Path]) -> Any:
    """(plan, document): a missing file is `none`, one that cannot be read is `unreadable`."""
    if path is None:
        return None, {"status": "none", "steps": []}
    try:
        from howlplane.control_plane.verification import VerificationPlan
        plan = VerificationPlan.from_dict(_read_json(path))
    except Exception as exc:
        return None, {"status": "unreadable", "steps": [], "reason": f"{type(exc).__name__}: {exc}"}
    return plan, _verification(plan)


def run_records(run_dir: Optional[Path]) -> Dict[str, Any]:
    """What a run directory records, read in one traversal, for ``resume --json``.

    Anything a record does not hold stays None (a duration) or is marked
    unrecorded (a role's findings); nothing is filled in with 0.0 or [].
    """
    found: Dict[tuple, Path] = {}
    if run_dir is not None and run_dir.is_dir():
        for dirpath, dirnames, filenames in os.walk(run_dir):
            parts = Path(dirpath).relative_to(run_dir).parts
            if not parts:
                dirnames[:] = [d for d in dirnames if d in ("reviews", "remediation", "implementation")]
            found.update({parts + (name,): Path(dirpath, name) for name in filenames})

    plan, verification = _read_verification(found.get(("verification_result.json",)))

    cycles: Dict[int, Dict[str, Any]] = {}  # cycle index -> role -> (status, findings, self_review, duration)
    for parts, path in found.items():
        if parts[-1] != "result.json":
            continue
        if len(parts) == 3 and parts[0] == "reviews":
            index, base, role = 1, parts[:1], parts[1]
        elif len(parts) == 5 and parts[0] == "remediation" and parts[2] == "re_review" and re.fullmatch(r"cycle-\d+", parts[1]):
            index, base, role = int(parts[1][6:]) + 1, parts[:3], parts[3]
        else:
            continue
        try:
            record = _read_json(path)
        except Exception:
            record = None
        record = record if isinstance(record, dict) else {}
        status = record.get("status")
        findings = _read_findings(found.get(base + (f"{role}_findings.yaml",)))
        if status == "findings_detected" and findings == []:
            findings = None  # the verdict says findings; the file says none, so what was found is unknown
        attempts = (record.get("failover") or {}).get("attempts") if isinstance(record.get("failover"), dict) else None
        duration = _seconds(record.get("duration_seconds"))
        if duration == 0.0 and not attempts:
            duration = None
        independence = record.get("independence")
        self_review = isinstance(independence, dict) and independence.get("independent") is False
        cycles.setdefault(index, {})[role] = (status, findings, self_review, duration)

    review_cycles, latest_self = [], {}
    for index in sorted(cycles):
        results = {}
        for role, (status, findings, self_review, _) in cycles[index].items():
            latest_self[role] = self_review
            if status != "not_run":
                results[role] = SimpleNamespace(
                    status=status if status in ("clean", "findings_detected") else "reviewer_failure",
                    findings=findings)
        review_cycles.append(SimpleNamespace(cycle_index=index, reviewer_results=results, non_independent_roles=[]))
    if review_cycles:
        review_cycles[-1].non_independent_roles = [r for r, flagged in latest_self.items() if flagged]

    attempt_records = []
    for parts in sorted(p for p in found if len(p) == 4 and p[:2] == ("implementation", "attempts") and p[3] == "attempt_record.json"):
        try:
            record = _read_json(found[parts])
        except Exception:
            continue
        if isinstance(record, dict):
            attempt_records.append(record)
    impl_durations = [None if (d := _seconds(r.get("duration_seconds"))) == 0.0 and r.get("exit_code") is None else d
                      for r in attempt_records]
    for cycle in sorted({p[1] for p in found if p[0] == "remediation"}):
        try:
            record = _read_json(found.get(("remediation", cycle, "result.json")))
        except Exception:
            record = None
        impl_durations.append(_seconds(record.get("duration_seconds")) if isinstance(record, dict) else None)

    review_durations = {i: [v[3] for v in roles.values()] for i, roles in cycles.items()}
    step_durations = [_seconds(s.duration_seconds) for s in plan.steps] if plan else []
    parts_of_run = impl_durations + [d for ds in review_durations.values() for d in ds] + step_durations
    run_duration = None
    if attempt_records and review_durations and step_durations and None not in parts_of_run:
        run_duration = round(sum(parts_of_run), 3)

    last = attempt_records[-1] if attempt_records else None
    return {
        "roles": sorted({role for roles in cycles.values() for role in roles}),
        "cycles": review_cycles,
        "verification": verification,
        "duration_seconds": run_duration,
        "review_cycles": [
            {"cycle_index": i, "roles": sorted(cycles[i]), "duration_seconds": _total(review_durations[i])}
            for i in sorted(cycles)
        ] or None,
        "provider_execution": None if last is None else {
            "agent_id": last.get("agent_id"),
            "resource_id": last.get("resource_id"),
            "attempt": last.get("attempt"),
            "attempts": len(attempt_records),
            "success": last.get("success") if isinstance(last.get("success"), bool) else None,
            "exit_code": last.get("exit_code"),
            "duration_seconds": impl_durations[len(attempt_records) - 1],
            "failure_class": last.get("failure_class"),
        },
    }


def work_result_document(res: Any, roles: Iterable[str], records: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The v1 document for a finished, parked or failed governed run.

    ``records`` (from ``run_records``, resume only) replaces what the live
    result would say about reviews, verification and timing with what the run
    directory recorded, and adds ``duration_seconds``, ``review_cycles`` and
    ``provider_execution`` (null when the records hold no value).
    """
    run_dir = Path(res.run_dir) if getattr(res, "run_dir", None) else None
    packet = run_dir / "decision_packet.md" if run_dir else None
    delta = getattr(res, "final_delta", None)
    document = _work_result_fields(res, roles, run_dir, packet, delta)
    if records is not None:
        document["reviews"] = role_verdicts(records["cycles"], sorted(set(roles) | set(records["roles"])))
        document["verification"] = records["verification"]
        for key in ("duration_seconds", "review_cycles", "provider_execution"):
            document[key] = records[key]
    return document


def _work_result_fields(res: Any, roles: Iterable[str], run_dir: Optional[Path], packet: Optional[Path],
                        delta: Any) -> Dict[str, Any]:
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
