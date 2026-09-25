#!/usr/bin/env python3
"""
decision_queue.py

Parked-task bookkeeping for delegated overnight campaigns (#59 Phases 12-14).

When a proposed action falls outside a campaign's AuthorityEnvelope, the task
is PARKED rather than the whole campaign halting: its state is preserved, a
ParkedTaskRecord is appended to durable campaign state, and the marathon loop
moves on to another independent, evidence-backed task. The campaign only
stops with AWAITING_HUMAN when a parked task turns out to block every
remaining meaningful objective.

An optional AI-generated recommendation may be attached to a parked task for
the human's morning review. That recommendation carries ZERO authority: it is
stored purely as data on ParkedTaskRecord and is never read by
evaluate_action_against_envelope() or GitIntegrationExecutor.evaluate() --
enforced by construction, since neither of those functions accepts a
recommendation argument at all.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from howlplane.control_plane.task_spec import DataClassSerializationMixin

PARKED_TASK_SCHEMA_VERSION = "howlplane.parked_task/v1"


@dataclass
class ParkedTaskRecord(DataClassSerializationMixin):
    """A task parked awaiting human authority, with enough context for morning review."""

    task_id: str
    objective: str
    boundary_type: str
    requested_action: str
    repository: str
    evidence: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    verification_state: str = "unknown"
    recommended_action: str = "REVIEW"
    recommendation_providers: List[str] = field(default_factory=list)
    why_delegated_authority_did_not_apply: str = ""
    blocks_other_work: bool = False
    parked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decision_packet: Dict[str, Any] = field(default_factory=dict)
    schema: str = PARKED_TASK_SCHEMA_VERSION


def request_ai_recommendation(
    task_objective: str,
    boundary_type: str,
    backend_pool: Optional[List[Any]] = None,
    recommend_fn: Optional[Callable[[str, str], Tuple[str, List[str]]]] = None,
) -> Tuple[str, List[str]]:
    """
    Optionally asks available AI reviewer(s) to recommend APPROVE / REJECT /
    REVIEW for a parked task, purely for the human's morning context. The
    result is data-only: it is only ever threaded into
    ParkedTaskRecord.recommended_action / recommendation_providers, never
    into any authority decision.

    `recommend_fn` is an injection point for tests / callers that already
    have a concrete AgentBackend-driven recommendation strategy; when absent,
    this returns a deterministic REVIEW recommendation with no provider
    attribution rather than guessing.
    """
    if recommend_fn is not None:
        return recommend_fn(task_objective, boundary_type)
    return "REVIEW", []


def already_parked(parked_tasks: List[Dict[str, Any]], task_id: str) -> bool:
    """Prevents repeatedly re-selecting a task that is already parked (#59 Phase 13)."""
    return any(p.get("task_id") == task_id for p in parked_tasks)


def compute_blocks_other_work(
    parked_task_id: str,
    framework_gaps: List[Dict[str, Any]],
    requested_benchmarks: List[str],
    already_succeeded_benchmarks: List[str],
    resolved_gap_codes: List[str],
    parked_tasks: List[Dict[str, Any]],
    current_gap_code: Optional[str] = None,
) -> bool:
    """
    Determines whether this park blocks every remaining meaningful objective,
    as opposed to leaving independent useful work available (#59 Phase 13).

    v1 heuristic (deliberately conservative -- prefer "keep going" unless
    truly nothing else is left): the park blocks everything only if there is
    no other unresolved, non-parked framework gap AND no benchmark remains
    both requested and not-yet-succeeded.

    `current_gap_code` identifies the specific framework gap this parked
    task was attempting to resolve (its task_id, e.g. "ENG-NOTES-01",
    generally differs from the gap's own code, e.g. "HF_GAP_NOTES" --
    conflating the two would leave the gap being parked right now counted
    as "other pending work" and never report blocks_other_work=True even
    when it's genuinely the only thing left).
    """
    parked_gap_codes = {p.get("gap_type_resolved") or p.get("task_id") for p in parked_tasks}
    parked_gap_codes.add(parked_task_id)
    if current_gap_code:
        parked_gap_codes.add(current_gap_code)

    other_pending_gaps = [
        g for g in framework_gaps
        if g.get("code") not in resolved_gap_codes and g.get("code") not in parked_gap_codes
    ]
    if other_pending_gaps:
        return False

    remaining_benchmarks = [b for b in requested_benchmarks if b not in already_succeeded_benchmarks]
    if remaining_benchmarks:
        return False

    return True
