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

from src.control_plane.agent_execution import AgentBackend, AgentBackendRegistry, AgentExecutionResult
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
# Live campaign DOGFOOD-20260822-205616-5466ce recorded claude_code timing out
# at 30.104s against the previous 30s ceiling in engine.py -- 180s is 6x that
# one observed near-miss, well under the 300s SubprocessAgentBackend default
# and the 600s OrchestrationConfig ceiling used for actual code remediation. A
# review only reads a diff and emits findings; it should need meaningfully
# less wall-clock than an implementation/remediation attempt. No per-provider
# or per-role override: one data point does not justify a config system.
REVIEW_TIMEOUT_SECONDS = 180

# Bounded reviewer failover depth (#59.2 Phase 4): preferred assignment + one
# fallback candidate. Matches the observed real failure mode (one reviewer
# times out, not the whole pool) without risking a multi-minute stall chaining
# through every eligible candidate on every review cycle.
MAX_REVIEWER_FAILOVER_ATTEMPTS = 2


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
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema: str = REVIEW_RUN_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_index": self.cycle_index,
            "status": self.status,
            "requires_remediation": self.requires_remediation,
            "reviewer_results": {k: v.to_dict() for k, v in self.reviewer_results.items()},
            "all_findings": [f.to_dict() for f in self.all_findings],
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "timestamp": self.timestamp,
            "schema": self.schema,
        }


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
    elif isinstance(parsed, str) and any(w in parsed.lower() for w in ["no defects", "zero findings", "looks good", "clean", "passed", "no issues"]):
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
) -> List[str]:
    """
    Builds the bounded reviewer candidate list for one role (#59.2 Phase 4):
    the preferred assignment first, then independent fallback candidates from
    the provider pool. Roles in LOCAL_INELIGIBLE_REVIEWER_ROLES never receive
    a local candidate, even as a fallback (#59.2 Phase 10). Shared by both
    review-invocation call sites (this module and engine.py) so the
    candidate-building logic exists in exactly one place.
    """
    from src.control_plane.synthesis.provider_pool import (
        LOCAL_INELIGIBLE_REVIEWER_ROLES,
        LOCAL_PROVIDER_IDS,
    )

    fallback_pool = provider_pool.select_candidates(
        task_category="code_heavy", avoid_provider=preferred, task=task,
    )
    if role_id in LOCAL_INELIGIBLE_REVIEWER_ROLES:
        fallback_pool = [c for c in fallback_pool if c not in LOCAL_PROVIDER_IDS]
    return [preferred] + [c for c in fallback_pool if c != preferred]


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
        try:
            backend = backend_lookup(candidate)
        except Exception:
            attempts_log.append({"provider": candidate, "outcome": "unavailable"})
            continue
        if not backend or not backend.is_available():
            attempts_log.append({"provider": candidate, "outcome": "unavailable"})
            continue

        # Tell the backend which candidate this attempt represents, matching
        # the pattern implementation/repair failover already uses (engine.py
        # sets TaskSpec.actual_agent=candidate per attempt) -- a dispatcher
        # backend keyed by actual_agent needs this to answer per-candidate.
        task.actual_agent = candidate
        agent_res = backend.execute(
            task=task,
            cwd=cwd,
            role=role_id,
            prompt_override=prompt_override,
            timeout_seconds=REVIEW_TIMEOUT_SECONDS,
        )
        attempt: Dict[str, Any] = {
            "provider": candidate,
            "duration_seconds": round(agent_res.duration_seconds, 3),
        }

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

            # Check if this reviewer already completed in a previous interrupted run
            cached_result: Optional[SingleReviewResult] = None
            if cycle_dir:
                cached_md = cycle_dir / f"{role_id}.md"
                cached_yaml = cycle_dir / f"{role_id}_findings.yaml"
                if cached_md.is_file() and cached_yaml.is_file():
                    try:
                        c_raw = cached_md.read_text(encoding="utf-8")
                        raw_data = yaml.safe_load(cached_yaml.read_text(encoding="utf-8")) or []
                        if isinstance(raw_data, dict):
                            c_findings_data = raw_data.get("findings", [])
                        elif isinstance(raw_data, list):
                            c_findings_data = raw_data
                        else:
                            c_findings_data = []
                        c_findings = [
                            ReviewFinding.from_dict(f) if isinstance(f, dict) else f
                            for f in c_findings_data
                        ]
                        cached_result = SingleReviewResult(
                            reviewer_role=role_id,
                            reviewer_name=role_name,
                            status="findings_detected" if c_findings else "clean",
                            findings=c_findings,
                            raw_output=c_raw,
                            error_message=None,
                            duration_seconds=0.0,
                        )
                    except Exception:
                        cached_result = None

            if cached_result is not None:
                reviewer_results[role_id] = cached_result
                all_findings.extend(cached_result.findings)
                continue

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
            if custom_reviewer_fn:
                try:
                    raw_output = custom_reviewer_fn(role_id, diff_content, task)
                except Exception as exc:
                    err_message = str(exc)
                    has_failure = True
            elif provider_pool is not None and not backend:
                agent_id = (reviewer_agent_mapping or {}).get(role_id) or "claude_code"
                candidates = build_reviewer_candidates(role_id, agent_id, provider_pool, task)
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
                if winner and agent_res:
                    raw_output = agent_res.stdout
                else:
                    err_message = "All candidate reviewers failed or were unavailable"
                    has_failure = True
            else:
                agent_id = (reviewer_agent_mapping or {}).get(role_id) or "claude_code"
                selected_backend = backend or AgentBackendRegistry.get_backend(agent_id)
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

            # Persist individual reviewer artifact incrementally
            if cycle_dir:
                try:
                    from src.control_plane.atomic_io import atomic_write_text, atomic_write_yaml
                    atomic_write_text(cycle_dir / f"{role_id}.md", raw_output)
                    atomic_write_yaml(
                        cycle_dir / f"{role_id}_findings.yaml",
                        [f.to_dict() for f in findings],
                        sort_keys=False,
                    )
                except Exception:
                    pass

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
