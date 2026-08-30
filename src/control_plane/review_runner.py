#!/usr/bin/env python3
"""
review_runner.py

Executes independent adversarial reviewers against actual implementation diffs,
validates structured output schemas, and determines targeted re-review strategies.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json, re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import yaml

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    LAUNCH_OUTCOME_KEY,
    TIMEOUT_SOURCE_KEY,
)
from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.reconciliation import ReviewFinding, ReconciliationResult, ReviewReconciler, VALID_SEVERITIES
from src.control_plane.reviewers import get_reviewer_role, ReviewerRole, build_skill_context
from src.control_plane.task_spec import TaskSpec

# src.control_plane.synthesis is imported lazily inside build_reviewer_candidates
# (below), not here at module scope: synthesis/__init__.py imports engine.py,
# which imports this module -- a module-level import here would be circular.

REVIEW_RUN_SCHEMA_VERSION = "howlplane.review_runner/v1"

# Shared review-attempt status vocabulary (#59.2 Phase 2). Both SingleReviewResult
# (this module) and engine.py's per-role invocation dict draw their `status`
# values from this set, so the two schemas stay legible against one vocabulary
# without being merged into a single abstraction -- they serve different callers
# with different persistence shapes.
#   clean              -- reviewer ran, produced valid output, zero findings
#                          (REVIEW_COMPLETED_WITH_NO_FINDINGS)
#   findings_detected  -- reviewer ran, produced valid output, findings present
#   reviewer_failure   -- backend execution itself failed (non-zero exit,
#                          timeout, crash)
#   malformed_output    -- backend succeeded but output could not be parsed
#                          under the expected review contract
#   output_invalid      -- backend succeeded (exit 0) but produced empty or
#                          otherwise non-committal output -- REVIEW_OUTPUT_INVALID,
#                          never silently treated as "clean"
REVIEW_ATTEMPT_STATUSES = (
    "clean",
    "findings_detected",
    "reviewer_failure",
    "malformed_output",
    "output_invalid",
)

# Default bounded timeout for a single reviewer invocation (#59.2 Phase 6).
#
# Was 180s, derived from a single observed 30.104s near-miss in campaign
# DOGFOOD-20260822-205616-5466ce on the theory that reading a diff needs less
# wall-clock than writing code. HOWLFRAM-BUG-50, the first real governed run,
# produced 20 attempts that say otherwise:
#
#   claude_code  0 completions / 4 attempts   180.155 180.157 180.163 180.14
#   codex        0 completions / 3 attempts   180.052 180.039 180.03
#   devin_cli    0 completions / 3 attempts   1.718 1.688 1.606  (quota, issues.md #15)
#   agy          4 completions / 10 attempts  124.159 136.037 169.434 178.316
#
# 13 of 20 attempts hit the deadline. Only agy ever completed a review, and its
# fastest success finished 1.7s under the ceiling: 180s sat at the *median*
# review duration, so every review was close to a coin flip. That is a
# governance problem, not a latency one -- when one provider fits the budget,
# bounded failover converges on it, and on that run it was the implementer,
# which PR #60 then correctly gated as a self-review.
#
# 600s is the value OrchestrationConfig already uses as its remediation
# ceiling. The 300s parameter on AgentBackend.execute is a default, not a cap,
# and no enclosing review-cycle or orchestration deadline exists, so this
# budget reaches the provider. Worst case is 3 roles x
# MAX_REVIEWER_FAILOVER_ATTEMPTS x (1 + max_remediation_cycles) cycles; that
# bound is documented rather than optimised, because truthful governed review
# is worth more than cutting off a reviewer that would have answered.
REVIEW_TIMEOUT_SECONDS = 600

# Bounded reviewer failover depth (#59.2 Phase 4): preferred assignment + one
# fallback candidate. Matches the observed real failure mode (one reviewer
# times out, not the whole pool) without risking a multi-minute stall chaining
# through every eligible candidate on every review cycle.
MAX_REVIEWER_FAILOVER_ATTEMPTS = 2


REVIEW_RESULT_SCHEMA_VERSION = "howlplane.review_result/v1"

# A reviewer that has not run yet is distinct from one that ran and found
# nothing. Resume must be able to tell them apart, so "never ran" is a durable
# state rather than the absence of a file.
REVIEW_STATUS_NOT_RUN = "not_run"


def review_role_dir(cycle_dir: Path, role_id: str) -> Path:
    """Durable evidence directory for one reviewer role in one cycle."""
    return cycle_dir / role_id


def write_review_result(
    cycle_dir: Optional[Path],
    role_id: str,
    result: "SingleReviewResult",
    *,
    implementer: Optional[str] = None,
    assigned_resource: Optional[str] = None,
) -> Optional[Path]:
    """Persists what actually happened when a reviewer ran.

    The markdown and findings files alone cannot answer whether a reviewer
    succeeded: a zero-byte transcript and an empty findings list are exactly
    what a clean review and a dead provider both leave behind. SLOPFIX-06
    resumed such a pair as "clean" and moved on. Everything the live path
    already computed -- status, process result, normalized failure, output
    validity -- is written here so resume can read the truth instead of
    guessing it.
    """
    if cycle_dir is None:
        return None

    agent_res = result.agent_result
    role_dir = review_role_dir(cycle_dir, role_id)
    attempts = result.attempts or []
    effective_resource = assigned_resource
    if attempts:
        effective_resource = attempts[-1].get("resource_id") or effective_resource
    if agent_res is not None:
        effective_resource = getattr(agent_res, "agent_id", None) or effective_resource

    # When every candidate fails, invoke_reviewer_with_failover returns no
    # AgentExecutionResult, so reading process evidence off `agent_res` wrote
    # null for all of it -- which is exactly the case where an operator most
    # needs to know why. Fall back to the last attempt, which now carries the
    # same structural fields (HOWLFRAM-BUG-50, issues.md #14).
    last_attempt = attempts[-1] if attempts else {}
    metadata = getattr(agent_res, "metadata", None) or {}

    def _evidence(field: str, *, from_metadata: bool = False) -> Any:
        if agent_res is not None:
            value = metadata.get(field) if from_metadata else getattr(agent_res, field, None)
            if value is not None:
                return value
        return last_attempt.get(field)

    # A role that consumed two full review budgets did not take 0.0 seconds.
    duration_seconds = result.duration_seconds
    if not duration_seconds and attempts:
        duration_seconds = round(
            sum(a.get("duration_seconds") or 0.0 for a in attempts), 3
        )

    payload: Dict[str, Any] = {
        "role": role_id,
        "reviewer_name": result.reviewer_name,
        "status": result.status,
        "attempt_count": max(len(attempts), 1),
        "resource_id": effective_resource,
        "assigned_resource_id": assigned_resource,
        "completed_at": result.timestamp,
        "duration_seconds": duration_seconds,
        "process": {
            "exit_code": _evidence("exit_code"),
            "success": getattr(agent_res, "success", None),
            "timed_out": _evidence("timed_out"),
        },
        "launch_outcome": _evidence(LAUNCH_OUTCOME_KEY, from_metadata=True),
        "timeout_source": _evidence(TIMEOUT_SOURCE_KEY, from_metadata=True),
        "raw_failure": result.error_message,
        "normalized_failure": metadata.get("normalized_failure")
        or last_attempt.get("failure_class"),
        "output_present": bool((result.raw_output or "").strip()),
        "output_valid": result.status in ("clean", "findings_detected"),
        "findings_count": len(result.findings),
        "disposition": result.status,
        "failover": {
            "engaged": len(attempts) > 1,
            "attempts": attempts,
        },
        "independence": {
            "implementer": implementer,
            "reviewer": effective_resource,
            "independent": (
                None
                if implementer is None or effective_resource is None
                else implementer != effective_resource
            ),
        },
        "schema": REVIEW_RESULT_SCHEMA_VERSION,
    }

    role_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(role_dir / "result.json", payload)

    # Per-attempt evidence, so a failover leaves a record of every provider
    # tried and not just the one that happened to answer last.
    for index, attempt in enumerate(attempts, start=1):
        resource = attempt.get("resource_id") or "unknown"
        attempt_dir = role_dir / "attempts" / f"{index:02d}-{resource}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(attempt_dir / "result.json", dict(attempt))

    return role_dir / "result.json"


def read_review_result(cycle_dir: Optional[Path], role_id: str) -> Optional[Dict[str, Any]]:
    """Loads a reviewer's persisted outcome, or None when there is none."""
    if cycle_dir is None:
        return None
    path = review_role_dir(cycle_dir, role_id) / "result.json"
    if not path.is_file():
        return None
    try:
        data = safe_load_json(path)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("status"):
        return None
    return data


@dataclass
class SingleReviewResult:
    """Result of an individual independent reviewer execution."""

    reviewer_role: str
    reviewer_name: str
    status: str  # one of REVIEW_ATTEMPT_STATUSES
    findings: List[ReviewFinding] = field(default_factory=list)
    raw_output: str = ""
    error_message: Optional[str] = None
    duration_seconds: float = 0.0
    agent_result: Optional[AgentExecutionResult] = None
    # Every candidate provider tried for this role, in order, when failover was
    # engaged (#59.2 Phase 4). Empty when only a single provider was assigned
    # (no provider_pool supplied) or a cached result was reused.
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reviewer_role": self.reviewer_role,
            "reviewer_name": self.reviewer_name,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "raw_output": self.raw_output,
            "error_message": self.error_message,
            "duration_seconds": self.duration_seconds,
            "agent_result": self.agent_result.to_dict() if self.agent_result else None,
            "attempts": self.attempts,
            "timestamp": self.timestamp,
        }


@dataclass
class ReviewCycleResult:
    """Consolidated result of a complete review cycle across multiple reviewers."""

    cycle_index: int
    reviewer_results: Dict[str, SingleReviewResult] = field(default_factory=dict)
    all_findings: List[ReviewFinding] = field(default_factory=list)
    reconciliation: Optional[ReconciliationResult] = None
    status: str = "clean"  # "clean", "has_findings", "review_failure"
    requires_remediation: bool = False
    # Roles this cycle served with the implementer itself, i.e. a self-review.
    # Recorded so the authority gate can name the real reason it triggered.
    non_independent_roles: List[str] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema: str = REVIEW_RUN_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_index": self.cycle_index,
            "status": self.status,
            "requires_remediation": self.requires_remediation,
            "non_independent_roles": self.non_independent_roles,
            "reviewer_results": {k: v.to_dict() for k, v in self.reviewer_results.items()},
            "all_findings": [f.to_dict() for f in self.all_findings],
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "timestamp": self.timestamp,
            "schema": self.schema,
        }


# Natural-language phrasings real reviewers use to report a clean result
# without wrapping it in a findings: YAML block (#59.2 live evidence: campaign
# DOGFOOD-20260823-203128-ed0e9e's simplicity-reviewer wrote "Zero defects
# found..." prose). Recognizing these directly -- before attempting to parse
# unfenced prose as YAML -- avoids misreading narrative bullet points/colons
# as structured (and thus malformed) data. All strongly indicate "nothing to
# report"; a reviewer describing an actual problem uses different framing, so
# this does not risk masking a real finding.
CLEAN_REVIEW_PHRASES = (
    "no defects", "zero defects", "zero findings", "zero regressions",
    "no regressions", "looks good", "no issues", "no problems",
    "nothing found", "no bugs",
)


def _is_recognized_clean_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CLEAN_REVIEW_PHRASES)


def parse_and_validate_findings(
    raw_output: str,
    reviewer_role: str,
) -> Tuple[List[ReviewFinding], Optional[str], bool]:
    """
    Parses YAML/JSON findings from raw reviewer output.
    Returns (findings, error_message, is_valid_output).
    If output is malformed, returns a synthetic finding signaling REVIEWER_FAILURE.

    `is_valid_output` is False whenever zero findings resulted from output that
    was empty, blank, or otherwise not a deliberate "no findings" signal -- such
    output must never be indistinguishable from a reviewer that actually ran and
    reported a clean result (REVIEW_COMPLETED_WITH_NO_FINDINGS, is_valid_output=True).
    """
    if not raw_output or not raw_output.strip():
        # Empty output is never evidence of a completed clean review.
        return [], None, False

    clean_text = raw_output.strip()

    # Extract yaml / json code block if wrapped in markdown
    code_block_match = re.search(r"```(?:yaml|json)?\s*\n([\s\S]*?)\n```", clean_text)
    if code_block_match:
        payload_text = code_block_match.group(1).strip()
    elif _is_recognized_clean_phrase(clean_text):
        # Unfenced natural-language prose reporting a clean result: accept it
        # directly rather than risk yaml.safe_load misreading its bullet
        # points/colons as an unrelated structured (and thus malformed) shape.
        return [], None, True
    else:
        payload_text = clean_text

    try:
        parsed = yaml.safe_load(payload_text)
    except Exception as exc:
        err_msg = f"Malformed reviewer output YAML: {exc}"
        failure_finding = ReviewFinding(
            id=f"ERR-{reviewer_role[:4].upper()}-001",
            reviewer_role=reviewer_role,
            title=f"Malformed reviewer output from {reviewer_role}",
            severity="high",
            category="other",
            description=f"Reviewer output failed schema validation: {err_msg}",
            status="open",
            claim="Reviewer produced unparseable output format",
            evidence=raw_output[:500],
        )
        return [failure_finding], err_msg, False

    if parsed is None:
        # Output existed but carried no structured content (e.g. an empty
        # code block) -- ambiguous, not a deliberate clean signal.
        return [], None, False

    # Normalization of parsed content
    findings_list: List[Dict[str, Any]] = []
    if isinstance(parsed, dict):
        if "findings" in parsed:
            raw_f = parsed.get("findings")
            if isinstance(raw_f, list):
                findings_list = raw_f
            elif raw_f is None:
                findings_list = []
            else:
                err_msg = f"'findings' field in output must be a list, got {type(raw_f)}"
                return [_make_err_finding(reviewer_role, err_msg, raw_output)], err_msg, False
        else:
            # Maybe single finding object
            if "title" in parsed or "severity" in parsed:
                findings_list = [parsed]
            elif not parsed:
                # Empty dict -- ambiguous, not a deliberate clean signal.
                return [], None, False
            else:
                err_msg = "Reviewer dictionary output missing 'findings' list"
                return [_make_err_finding(reviewer_role, err_msg, raw_output)], err_msg, False
    elif isinstance(parsed, list):
        findings_list = parsed
    elif isinstance(parsed, str) and _is_recognized_clean_phrase(parsed):
        # An explicit prose "clean" signal is a deliberate, valid result.
        return [], None, True
    else:
        err_msg = f"Unexpected reviewer output type: {type(parsed)}"
        return [_make_err_finding(reviewer_role, err_msg, raw_output)], err_msg, False

    validated_findings: List[ReviewFinding] = []
    for idx, item in enumerate(findings_list, 1):
        if not isinstance(item, dict):
            err_msg = f"Finding at index {idx} is not a valid dictionary"
            return [_make_err_finding(reviewer_role, err_msg, raw_output)], err_msg, False

        f_id = str(item.get("id") or f"F{idx:03d}")
        title = str(item.get("title") or f"Issue found by {reviewer_role}")
        severity = str(item.get("severity") or "medium").lower().strip()
        if severity not in VALID_SEVERITIES:
            severity = "medium"

        category = str(item.get("category") or "correctness").lower().strip()
        description = str(item.get("description") or item.get("claim") or title)
        location = item.get("location")
        claim = item.get("claim")
        evidence = item.get("evidence")
        suggested_fix = item.get("suggested_fix")
        component = item.get("component")

        finding = ReviewFinding(
            id=f_id,
            reviewer_role=reviewer_role,
            title=title,
            severity=severity,
            category=category,
            description=description,
            component=component,
            claim=claim,
            location=location,
            evidence=evidence,
            suggested_fix=suggested_fix,
            status="open",
        )
        validated_findings.append(finding)

    # Reaching here means the output was a structurally valid findings
    # container (an explicit list, a `findings:` key, or `findings: null`),
    # even when validated_findings ends up empty -- a deliberate clean signal.
    return validated_findings, None, True


def build_reviewer_candidates(
    role_id: str,
    preferred: str,
    provider_pool: Any,
    task: TaskSpec,
    implementer: Optional[str] = None,
) -> List[str]:
    """
    Builds the bounded reviewer candidate list for one role (#59.2 Phase 4):
    the preferred assignment first, then independent fallback candidates from
    the provider pool. Roles in LOCAL_INELIGIBLE_REVIEWER_ROLES never receive
    a local candidate, even as a fallback (#59.2 Phase 10). Shared by both
    review-invocation call sites (this module and engine.py) so the
    candidate-building logic exists in exactly one place.

    The implementer is ordered *last*. It stays reachable, because a degraded
    provider pool should still yield some signal rather than none, but it is
    only ever reached once every independent candidate has been tried. On
    HOWLFRAM-BUG-50 the implementer was not considered at all here, so ordinary
    failover handed it three of its own reviews -- including the final
    correctness verdict on its own diff. A review it does serve is recorded as
    non-independent and forces the human authority gate.
    """
    from src.control_plane.synthesis.provider_pool import (
        LOCAL_INELIGIBLE_REVIEWER_ROLES,
        LOCAL_PROVIDER_IDS,
    )

    fallback_pool = provider_pool.select_candidates(
        task_category="code_heavy",
        avoid_provider=preferred,
        task=task,
        role=role_id,
    )
    if role_id in LOCAL_INELIGIBLE_REVIEWER_ROLES:
        fallback_pool = [c for c in fallback_pool if c not in LOCAL_PROVIDER_IDS]
    ordered = [preferred] + [c for c in fallback_pool if c != preferred]
    if implementer:
        independent = [c for c in ordered if c != implementer]
        if len(independent) != len(ordered):
            ordered = independent + [implementer]
    return ordered


def _current_capacity_block(provider_pool: Optional[Any], candidate: str) -> Optional[str]:
    """Names the reason a provider cannot serve a review right now, if any.

    Consults the pool's live availability rather than only asking whether the
    executable exists, so a provider already known to be unreachable or
    exhausted is skipped before an attempt is spent on it.
    """
    if provider_pool is None:
        return None
    try:
        status = provider_pool.get_status(candidate)
    except Exception:
        return None
    blocked = getattr(provider_pool, "_capacity_exclusion", None)
    if callable(blocked):
        try:
            return blocked(status)
        except Exception:
            return None
    return None


def invoke_reviewer_with_failover(
    role_id: str,
    candidates: List[str],
    task: TaskSpec,
    cwd: Union[str, Path],
    prompt_override: str,
    backend_lookup: Callable[[str], Optional[AgentBackend]],
    provider_pool: Optional[Any] = None,
    max_attempts: int = MAX_REVIEWER_FAILOVER_ATTEMPTS,
) -> Tuple[Optional[str], Optional["AgentExecutionResult"], List[Dict[str, Any]]]:
    """
    Tries independent reviewer candidates for one role, in order, bounded by
    `max_attempts` (#59.2 Phase 4). Stops at the first candidate whose backend
    executes successfully AND whose output is valid per
    `parse_and_validate_findings` (a completed clean review is a valid stop
    condition, not just a completed review with findings). Skips a candidate
    -- without counting it as a provider quota failure unless it actually is
    one -- on unavailability, exhaustion (detected via `provider_pool` if
    supplied), execution failure, or invalid/malformed output.

    Returns (winning_provider_id, winning_agent_result, attempt_log); the
    first element is None if every candidate in `candidates[:max_attempts]`
    failed. `attempt_log` records every attempt made (provider, duration,
    outcome) for durable evidence regardless of outcome.
    """
    attempts_log: List[Dict[str, Any]] = []
    for candidate in candidates[:max_attempts]:
        # Reviewer assignments are planned when implementation settles, which
        # can be many minutes before the review actually launches. Honour the
        # provider's state now rather than the state it had at planning time:
        # SLOPFIX-06 routed the correctness review to a provider whose
        # transport had already failed, spent the attempt, and got nothing back.
        unavailable_reason = _current_capacity_block(provider_pool, candidate)
        if unavailable_reason:
            attempts_log.append(
                {
                    "provider": candidate,
                    "resource_id": candidate,
                    "outcome": "unavailable",
                    "reason": unavailable_reason,
                    "checked_at": "launch",
                }
            )
            continue
        try:
            backend = backend_lookup(candidate)
        except Exception:
            attempts_log.append({"provider": candidate, "resource_id": candidate, "outcome": "unavailable"})
            continue
        if not backend or not backend.is_available():
            attempts_log.append({"provider": candidate, "resource_id": candidate, "outcome": "unavailable"})
            continue

        # Tell the backend which candidate this attempt represents. This is a
        # dispatch slot, not an audit field: writing it to `actual_agent` is
        # what let a reviewer's provider id become the task's durable
        # "implementing agent" on HOWLFRAM-BUG-52, where task.yaml ended
        # `actual_agent: codex` naming the test-falsifier reviewer while
        # effective_route.json named claude_code (issues.md #16). Backends read
        # `task.dispatch_target`, which falls back to `actual_agent` for the
        # implementation paths that never set a dispatch target.
        task.dispatch_resource_id = candidate
        agent_res = backend.execute(
            task=task,
            cwd=cwd,
            role=role_id,
            prompt_override=prompt_override,
            timeout_seconds=REVIEW_TIMEOUT_SECONDS,
        )
        attempt: Dict[str, Any] = {
            "provider": candidate,
            "resource_id": candidate,
            "duration_seconds": round(agent_res.duration_seconds, 3),
        }
        attempt.update(_attempt_execution_evidence(agent_res, provider_pool, candidate))

        exhaustion = provider_pool.detect_exhaustion(candidate, agent_res) if provider_pool else None
        if exhaustion:
            attempt["outcome"] = "exhausted"
            attempts_log.append(attempt)
            continue
        if not agent_res.success:
            attempt["outcome"] = "reviewer_failure"
            attempts_log.append(attempt)
            continue

        _findings, parse_err, is_valid_output = parse_and_validate_findings(agent_res.stdout, role_id)
        if parse_err:
            attempt["outcome"] = "malformed_output"
            attempts_log.append(attempt)
            continue
        if not is_valid_output:
            attempt["outcome"] = "output_invalid"
            attempts_log.append(attempt)
            continue

        attempt["outcome"] = "completed"
        attempts_log.append(attempt)
        return candidate, agent_res, attempts_log

    return None, None, attempts_log


def _attempt_execution_evidence(
    agent_res: Any,
    provider_pool: Optional[Any],
    candidate: str,
) -> Dict[str, Any]:
    """Structural evidence for one reviewer attempt.

    A reviewer that timed out, one whose executable never spawned, and one that
    launched and then exited non-zero are three different events. Recording only
    provider/duration/outcome made them indistinguishable, so on HOWLFRAM-BUG-50
    a 180s deadline and a 1.6s provider quota failure left byte-identical
    evidence and the cause had to be reproduced live against the provider.

    Everything here is read off the process result the harness already observed.
    Nothing is inferred from elapsed time.
    """
    metadata = getattr(agent_res, "metadata", None) or {}
    evidence: Dict[str, Any] = {
        "exit_code": getattr(agent_res, "exit_code", None),
        "timed_out": getattr(agent_res, "timed_out", None),
        "timeout_source": metadata.get(TIMEOUT_SOURCE_KEY),
        "launch_outcome": metadata.get(LAUNCH_OUTCOME_KEY),
    }
    if provider_pool is not None and not getattr(agent_res, "success", False):
        try:
            failure_class = provider_pool.classify_failure(candidate, agent_res)
        except Exception:
            failure_class = None
        evidence["failure_class"] = getattr(failure_class, "value", failure_class)
    return evidence


def _make_err_finding(reviewer_role: str, err_msg: str, raw_output: str) -> ReviewFinding:
    return ReviewFinding(
        id=f"ERR-{reviewer_role[:4].upper()}-001",
        reviewer_role=reviewer_role,
        title=f"Malformed reviewer output from {reviewer_role}",
        severity="high",
        category="other",
        description=f"Reviewer output failed validation: {err_msg}",
        status="open",
        claim="Reviewer produced unparseable output format",
        evidence=raw_output[:500],
    )


class ReviewRunner:
    """Orchestrates independent review runs, output parsing, and reconciliation."""

    @classmethod
    def _reconstruct_cached_review(
        cls,
        cycle_dir: Optional[Path],
        role_id: str,
        role_name: str,
    ) -> Optional["SingleReviewResult"]:
        """Rebuilds a reviewer's durable state after an interruption.

        Prefers the persisted `result.json`, which records exactly what the
        live path concluded. Runs predating that file fall back to judging the
        transcript itself: an empty one reconstructs as `output_invalid`, never
        as clean. Returns None when the reviewer genuinely never ran.
        """
        if cycle_dir is None:
            return None

        cached_md = cycle_dir / f"{role_id}.md"
        cached_yaml = cycle_dir / f"{role_id}_findings.yaml"

        persisted = read_review_result(cycle_dir, role_id)
        raw_text = ""
        if cached_md.is_file():
            try:
                raw_text = cached_md.read_text(encoding="utf-8")
            except Exception:
                raw_text = ""

        if persisted is not None:
            findings: List[ReviewFinding] = []
            if persisted.get("status") in ("clean", "findings_detected"):
                findings = cls._load_cached_findings(cached_yaml)
            return SingleReviewResult(
                reviewer_role=role_id,
                reviewer_name=role_name,
                status=persisted.get("status", REVIEW_STATUS_NOT_RUN),
                findings=findings,
                raw_output=raw_text,
                error_message=persisted.get("raw_failure"),
                duration_seconds=persisted.get("duration_seconds", 0.0) or 0.0,
                attempts=(persisted.get("failover") or {}).get("attempts", []),
            )

        if not (cached_md.is_file() and cached_yaml.is_file()):
            return None

        # Legacy evidence, written before reviewer results were persisted. The
        # findings file cannot settle this on its own -- an empty list is what a
        # deliberate clean review and a dead provider both leave behind. The
        # transcript can: a reviewer that emitted nothing at all did not review
        # anything, whatever its findings file says. That is exactly the pair
        # SLOPFIX-06 reconstructed as clean (0-byte transcript, `[]` findings).
        if not raw_text.strip():
            return SingleReviewResult(
                reviewer_role=role_id,
                reviewer_name=role_name,
                status="output_invalid",
                findings=[],
                raw_output="",
                error_message=(
                    "Reviewer transcript is empty; no durable result was "
                    "recorded, so the review cannot be treated as completed."
                ),
                duration_seconds=0.0,
            )

        findings = cls._load_cached_findings(cached_yaml)
        return SingleReviewResult(
            reviewer_role=role_id,
            reviewer_name=role_name,
            status="findings_detected" if findings else "clean",
            findings=findings,
            raw_output=raw_text,
            error_message=None,
            duration_seconds=0.0,
        )

    @staticmethod
    def _load_cached_findings(cached_yaml: Path) -> List[ReviewFinding]:
        """Reads a persisted findings file, tolerating both list and mapping forms."""
        if not cached_yaml.is_file():
            return []
        try:
            raw_data = yaml.safe_load(cached_yaml.read_text(encoding="utf-8")) or []
        except Exception:
            return []
        if isinstance(raw_data, dict):
            entries = raw_data.get("findings", [])
        elif isinstance(raw_data, list):
            entries = raw_data
        else:
            entries = []
        out: List[ReviewFinding] = []
        for item in entries:
            try:
                out.append(
                    ReviewFinding.from_dict(item) if isinstance(item, dict) else item
                )
            except Exception:
                continue
        return out

    @classmethod
    def execute_review_cycle(
        cls,
        task: TaskSpec,
        diff_content: str,
        reviewer_roles: List[str],
        cwd: Union[str, Path],
        backend: Optional[AgentBackend] = None,
        cycle_index: int = 1,
        reviewer_agent_mapping: Optional[Dict[str, str]] = None,
        custom_reviewer_fn: Optional[Callable[[str, str, TaskSpec], str]] = None,
        run_dir: Optional[Union[str, Path]] = None,
        provider_pool: Optional[Any] = None,
        progress_tracker: Optional[Any] = None,
        implementer_resource_id: Optional[str] = None,
    ) -> ReviewCycleResult:
        """
        Executes each specified reviewer independently against the actual implementation diff.
        Preserves already-completed reviewer artifacts from previous interrupted runs.

        `provider_pool` is optional (#59.2 Phase 4): when supplied, a reviewer
        whose primary assignment fails, times out, or produces invalid output
        gets one bounded fallback attempt against an independent candidate via
        `invoke_reviewer_with_failover`, mirroring implementation failover.
        Omitting it (the default, and the only behavior any caller exercised
        before #59.2) preserves the prior single-attempt-per-role behavior
        exactly.
        """
        target_cwd = Path(cwd).resolve()
        reviewer_results: Dict[str, SingleReviewResult] = {}
        all_findings: List[ReviewFinding] = []
        has_failure = False
        non_independent_roles: List[str] = []

        skill_context = build_skill_context(task)

        # Establish cycle directory for incremental checkpointing
        cycle_dir: Optional[Path] = None
        if run_dir:
            r_path = Path(run_dir).resolve()
            if cycle_index == 1:
                cycle_dir = r_path / "reviews"
            else:
                cycle_dir = r_path / "remediation" / f"cycle-{cycle_index - 1:02d}" / "re_review"
            cycle_dir.mkdir(parents=True, exist_ok=True)

        for role_id in reviewer_roles:
            role = get_reviewer_role(role_id)
            role_name = role.name if role else role_id

            # Reconstruct a reviewer that already ran in an interrupted run.
            # Status comes from the persisted result, never from the shape of
            # the findings file: inferring "clean" from an empty list turned a
            # dead reviewer's zero-byte output into a passing review and
            # skipped the retry it was owed (HOWLFRAM-SLOPFIX-06).
            cached_result = cls._reconstruct_cached_review(
                cycle_dir, role_id, role_name
            )

            if cached_result is not None:
                if cached_result.status in ("clean", "findings_detected"):
                    reviewer_results[role_id] = cached_result
                    all_findings.extend(cached_result.findings)
                    continue
                # Persisted as invalid/failed: it stays invalid, and the role is
                # re-run under the normal policy below rather than being
                # silently accepted.

            # Render brief with the REAL implementation diff and skill context
            brief = (
                role.render_brief(task=task, diff_content=diff_content, context=skill_context)
                if role
                else f"# Review Brief for {role_id}\n{skill_context or ''}\n```diff\n{diff_content}\n```"
            )

            raw_output = ""
            err_message = None
            agent_res: Optional[AgentExecutionResult] = None
            duration = 0.0

            attempts_log: List[Dict[str, Any]] = []
            assigned_agent = (reviewer_agent_mapping or {}).get(role_id) or "claude_code"
            if backend:
                assigned_agent = getattr(backend, "agent_id", assigned_agent)

            from src.control_plane.progress import track_operation
            with track_operation(
                progress_tracker,
                phase="REVIEWING",
                resource_id=assigned_agent,
                role="review",
                cycle=cycle_index,
                details=f"cycle {cycle_index}",
            ):
                if custom_reviewer_fn:
                    try:
                        raw_output = custom_reviewer_fn(role_id, diff_content, task)
                    except Exception as exc:
                        err_message = str(exc)
                        has_failure = True
                elif provider_pool is not None and not backend:
                    candidates = build_reviewer_candidates(
                        role_id, assigned_agent, provider_pool, task, implementer_resource_id
                    )
                    winner, agent_res, attempts_log = invoke_reviewer_with_failover(
                        role_id=role_id,
                        candidates=candidates,
                        task=task,
                        cwd=target_cwd,
                        prompt_override=brief,
                        backend_lookup=lambda aid: AgentBackendRegistry.get_backend(aid),
                        provider_pool=provider_pool,
                    )
                    duration = agent_res.duration_seconds if agent_res else 0.0
                    if winner and implementer_resource_id and winner == implementer_resource_id:
                        # Failover exhausted every independent candidate and fell
                        # through to the implementer. The review still runs, but a
                        # change reviewed by its own author is not independently
                        # reviewed, and the gate has to say so.
                        non_independent_roles.append(role_id)
                    if winner and agent_res:
                        raw_output = agent_res.stdout
                    else:
                        err_message = "All candidate reviewers failed or were unavailable"
                        has_failure = True
                else:
                    selected_backend = backend or AgentBackendRegistry.get_backend(assigned_agent)
                    agent_res = selected_backend.execute(
                        task=task,
                        cwd=target_cwd,
                        role=role_id,
                        prompt_override=brief,
                        timeout_seconds=REVIEW_TIMEOUT_SECONDS,
                    )
                    duration = agent_res.duration_seconds
                    if agent_res.success:
                        raw_output = agent_res.stdout
                    else:
                        err_message = agent_res.stderr or agent_res.error_message
                        has_failure = True

            findings, parse_err, is_valid_output = parse_and_validate_findings(raw_output, role_id)
            if parse_err:
                has_failure = True
                status = "malformed_output"
            elif err_message:
                status = "reviewer_failure"
            elif findings:
                status = "findings_detected"
            elif not is_valid_output:
                has_failure = True
                status = "output_invalid"
            else:
                status = "clean"

            single_res = SingleReviewResult(
                reviewer_role=role_id,
                reviewer_name=role_name,
                status=status,
                findings=findings,
                raw_output=raw_output,
                error_message=err_message or parse_err,
                duration_seconds=duration,
                agent_result=agent_res,
                attempts=attempts_log,
            )
            reviewer_results[role_id] = single_res
            all_findings.extend(findings)

            # Persist individual reviewer artifact incrementally. The legacy
            # markdown/findings pair stays for backward compatibility; the
            # result record is what resume actually reads.
            if cycle_dir:
                from src.control_plane.atomic_io import atomic_write_text, atomic_write_yaml
                try:
                    atomic_write_text(cycle_dir / f"{role_id}.md", raw_output)
                    atomic_write_yaml(
                        cycle_dir / f"{role_id}_findings.yaml",
                        [f.to_dict() for f in findings],
                        sort_keys=False,
                    )
                    write_review_result(
                        cycle_dir,
                        role_id,
                        single_res,
                        implementer=implementer_resource_id,
                        assigned_resource=assigned_agent,
                    )
                except Exception as persist_err:
                    # A review whose evidence did not survive is not a review we
                    # may later reconstruct as clean. Mark it failed loudly
                    # rather than leaving a half-written pair on disk.
                    single_res.status = "reviewer_failure"
                    single_res.error_message = (
                        f"Reviewer evidence could not be persisted: {persist_err}"
                    )
                    has_failure = True

        # Run reconciliation across all gathered findings
        reconciliation = ReviewReconciler.reconcile(all_findings) if all_findings else None

        # Check if remediation is needed: unresolved blockers > 0, unresolved highs
        # > 0, or any reviewer failed to complete (#59.2 Phase 3). A reviewer that
        # never ran/parsed successfully must never be silently indistinguishable
        # from "ran cleanly, found nothing" -- has_failure alone must gate here,
        # independent of whether any findings exist to reconcile.
        requires_remediation = False
        if has_failure:
            requires_remediation = True
        if reconciliation:
            if reconciliation.unresolved_blockers > 0 or reconciliation.unresolved_highs > 0:
                requires_remediation = True
            elif reconciliation.confirmed or reconciliation.likely:
                requires_remediation = True

        overall_status = "review_failure" if has_failure else ("has_findings" if all_findings else "clean")

        return ReviewCycleResult(
            cycle_index=cycle_index,
            reviewer_results=reviewer_results,
            all_findings=all_findings,
            reconciliation=reconciliation,
            status=overall_status,
            requires_remediation=requires_remediation,
            non_independent_roles=non_independent_roles,
        )

    @classmethod
    def determine_re_review_roles(
        cls,
        findings: List[ReviewFinding],
        original_roles: List[str],
    ) -> List[str]:
        """
        Deterministically selects targeted reviewers for re-review after remediation.
        """
        if not findings:
            return list(original_roles)

        selected: Set[str] = set()
        for f in findings:
            cat = (f.category or "").lower()
            role = f.reviewer_role

            if role:
                selected.add(role)

            if cat in ("security", "vuln", "auth"):
                selected.update(["security-reviewer", "correctness-reviewer", "test-falsifier"])
            elif cat in ("architecture", "coupling", "boundary"):
                selected.update(["architecture-reviewer", "regression-reviewer", "correctness-reviewer"])
            elif cat in ("regression", "breaking_change"):
                selected.update(["regression-reviewer", "correctness-reviewer"])
            elif cat in ("test_gap", "missing_test", "vacuous_test"):
                selected.update(["test-falsifier", "correctness-reviewer"])
            elif cat in ("simplicity", "complexity", "dead_code"):
                selected.update(["simplicity-reviewer", "correctness-reviewer"])
            else:
                selected.update(["correctness-reviewer", "test-falsifier"])

        # Filter against available original roles or known reviewer roles
        target_roles = [r for r in original_roles if r in selected]
        if not target_roles:
            target_roles = list(selected)
        return sorted(list(set(target_roles)))
