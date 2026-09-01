#!/usr/bin/env python3
"""
campaign_state.py

Durable campaign persistence for HowlPlane marathon dogfooding.
Tracks campaign identity, provider states, benchmark history, engineering tasks,
git commits / PRs / CI / merges, framework gaps, and resume points.
Guarantees atomic persistence across process boundaries and crashes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Union

from src.control_plane.atomic_io import atomic_write_json, atomic_write_text, safe_load_json
from src.control_plane.task_spec import DataClassSerializationMixin

CAMPAIGN_STATE_SCHEMA_VERSION = "howlplane.marathon_dogfood_state/v2"

# Longest provider error text retained per attempt. Enough to identify the
# failure in a morning report without copying a whole agent transcript into
# durable campaign state.
ATTEMPT_ERROR_DIGEST_LIMIT = 400

# Default local-only continuation budget: after all cloud providers are
# exhausted, at most this many consecutive local-eligible iterations run
# before the campaign stops cleanly (#58 Phase 13).
DEFAULT_LOCAL_ONLY_ITERATION_LIMIT = 10


def default_local_model_state() -> Dict[str, Any]:
    return {
        "provider": "local_ollama",
        "model": "qwen2.5-coder:7b-instruct",
        "context_length": 8192,
        "local_only_iterations": 0,
        "local_only_iteration_limit": DEFAULT_LOCAL_ONLY_ITERATION_LIMIT,
        "resource_guard_min_ram_gib": 8.0,
        "last_available_ram_gib": None,
        "success_count": 0,
        "failure_count": 0,
        "escalations": 0,
        "current_availability": "UNKNOWN",
    }


@dataclass
class GitIntegrationRecord(DataClassSerializationMixin):
    """
    Tracks a git commit, branch, PR, CI evaluation, and merge for an
    engineering task. Fail-closed by design (#59 Phase 1): every field that
    represents an observed real-world fact defaults to "nothing has been
    observed yet" rather than "assume success". `merged` in particular
    defaults to False and must never be set True by any code path except
    immediately after independently verifying the merge SHA is reachable
    from the remote's main branch -- see is_fully_integrated().
    """

    task_id: str
    target_repo: str
    idempotency_key: Optional[str] = None
    # "real": actual git/GitHub operations performed and independently
    #   verified. "simulated": no real integration was attempted (legacy /
    #   non-campaign paths). "test_fake": test-boundary fakes exercised the
    #   real production call sequence. Consumers must never treat
    #   "simulated" as evidence of a landed change.
    integration_mode: str = "simulated"
    branch: Optional[str] = None
    branch_observed: bool = False
    commit_sha: Optional[str] = None
    commit_observed: bool = False
    commit_message: str = ""
    push_observed: bool = False
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    pr_observed: bool = False
    required_checks: List[Dict[str, str]] = field(default_factory=list)
    required_checks_observed: bool = False
    required_checks_green: bool = False
    ci_status: str = "pending"  # "pending", "passed", "failed", "cancelled", "timed_out", "unavailable"
    merge_observed: bool = False
    merged: bool = False
    merge_sha: Optional[str] = None  # GitHub-produced squash-merge SHA, distinct from commit_sha
    merged_at: Optional[str] = None
    ci_observed_head_sha: Optional[str] = None
    current_pr_head_sha: Optional[str] = None
    remote_main_contains_merge: bool = False
    local_main_synced: bool = False
    provider: Optional[str] = None
    failure_reason: Optional[str] = None
    schema: str = "howlplane.git_record/v2"

    def is_fully_integrated(self) -> bool:
        """
        The single source of truth for "this task actually landed". No other
        code path should independently decide a task is integrated.
        """
        return (
            self.branch_observed
            and self.commit_observed
            and self.push_observed
            and self.pr_observed
            and self.required_checks_green
            and self.merge_observed
            and self.remote_main_contains_merge
        )


@dataclass
class EngineeringAttempt(DataClassSerializationMixin):
    """
    One provider's attempt at a single governed engineering task (#59.1
    Phase 2). A task may be attempted by several providers in turn when
    earlier ones report quota/session exhaustion or unavailability; recording
    every attempt is what lets the morning report explain *why* the provider
    changed rather than only naming whichever provider happened to finish.
    """

    task_id: str
    provider: str
    attempt_index: int
    # COMPLETE | SESSION_EXHAUSTED | RATE_LIMITED | UNAVAILABLE
    # | RESOURCE_CONSTRAINED | ENGINEERING_FAILURE | AUTHORITY_BLOCKED
    result: str
    failure_class: Optional[str] = None
    exit_code: Optional[int] = None
    error_digest: str = ""
    # True when this attempt left task-owned changes behind that were
    # checkpointed and reverted before the next provider was handed the repo.
    delta_reconciled: bool = False
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_seconds: float = 0.0

    @staticmethod
    def digest(text: Optional[str]) -> str:
        """Truncates provider error output to a durable, report-sized digest."""
        collapsed = " ".join((text or "").split())
        if len(collapsed) <= ATTEMPT_ERROR_DIGEST_LIMIT:
            return collapsed
        return collapsed[:ATTEMPT_ERROR_DIGEST_LIMIT] + "..."


@dataclass
class DurableCampaignState(DataClassSerializationMixin):
    """
    Durable, serializable state of an autonomous dogfooding campaign.
    Persisted outside ephemeral task journals to allow reliable resume and audit.
    """

    campaign_id: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    north_star_objective: str = "Prompt -> Create -> Verify/Repair -> Usable Product"
    active_benchmark: Optional[str] = None
    active_engineering_task: Optional[str] = None
    # Persisted benchmark/task scope this campaign was actually asked to run.
    # `ai dogfood --resume <id>` restores exactly this scope rather than
    # silently expanding to the full default benchmark suite (#58 Phase 1).
    requested_benchmarks: List[str] = field(default_factory=list)
    scope_extensions: List[Dict[str, Any]] = field(default_factory=list)
    # Local (quota-free) model participation state (#58 Phase 15).
    local_model: Dict[str, Any] = field(default_factory=dict)
    benchmark_history: List[Dict[str, Any]] = field(default_factory=list)
    provider_states: Dict[str, str] = field(default_factory=dict)
    provider_invocations: Dict[str, int] = field(default_factory=dict)
    # Per-role invocation counts recorded only where an AgentExecutionResult
    # actually exists (#59.1 Phase 8): {provider: {implementation|remediation|
    # review: n}}. A provider merely *mapped* to a reviewer role is never
    # counted here -- assignment is not execution.
    provider_role_invocations: Dict[str, Dict[str, int]] = field(default_factory=dict)
    completed_tasks: List[Dict[str, Any]] = field(default_factory=list)
    failed_tasks: List[Dict[str, Any]] = field(default_factory=list)
    # Ordered per-provider attempt history for governed engineering tasks.
    engineering_attempts: List[Dict[str, Any]] = field(default_factory=list)
    framework_gaps: List[Dict[str, Any]] = field(default_factory=list)
    # Cross-provider falsification rounds and their verdicts, plus failures
    # attributed to a provider rather than to the framework (#59.1 Phase 5).
    gap_falsifications: List[Dict[str, Any]] = field(default_factory=list)
    provider_specific_failures: List[Dict[str, Any]] = field(default_factory=list)
    git_records: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_diversity_records: List[Dict[str, Any]] = field(default_factory=list)
    # Reasoning strategy dogfooding artifacts (#60A): durable references to
    # trajectories and experiments, not the full payloads.
    trajectory_ids: List[str] = field(default_factory=list)
    reasoning_experiments: List[Dict[str, Any]] = field(default_factory=list)
    trajectory_observations: List[Dict[str, Any]] = field(default_factory=list)
    repair_cycles_total: int = 0
    # Delegated overnight authority (#59). `authority_envelope` is the
    # serialized AuthorityEnvelope this campaign was bound to at creation or
    # explicit resume-reauthorization; the envelope file itself remains the
    # tamper-evident source of truth (see authority_envelope.py) -- this
    # copy is for display/audit only and is never re-verified from here.
    authority_envelope: Optional[Dict[str, Any]] = None
    parked_tasks: List[Dict[str, Any]] = field(default_factory=list)
    pending_human_decisions: List[Dict[str, Any]] = field(default_factory=list)
    merges_this_campaign: int = 0
    stop_reason: str = "in_progress"
    next_action: str = "continue_benchmarks"
    total_duration_seconds: float = 0.0
    iterations_attempted: int = 0
    iterations_succeeded: int = 0
    iterations_failed: int = 0
    schema: str = CAMPAIGN_STATE_SCHEMA_VERSION

    def update_timestamp(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def ensure_local_model_defaults(self) -> Dict[str, Any]:
        """Lazily populates `local_model` for campaigns created before #58."""
        if not self.local_model:
            self.local_model = default_local_model_state()
        return self.local_model

    def extend_benchmark_scope(self, additional_benchmarks: List[str]) -> List[str]:
        """
        Explicitly adds benchmarks to the persisted campaign scope. Returns the
        list of benchmarks actually added (already-present ones are no-ops).
        Recorded as a distinct scope-extension event so it's clear this was an
        explicit operator widening rather than an implicit resume expansion.
        """
        added = [b for b in additional_benchmarks if b not in self.requested_benchmarks]
        if added:
            self.requested_benchmarks = self.requested_benchmarks + added
            self.scope_extensions.append({
                "added": added,
                "at": datetime.now(timezone.utc).isoformat(),
            })
            self.update_timestamp()
        return added

    def record_trajectory(self, trajectory_id: str) -> None:
        if trajectory_id and trajectory_id not in self.trajectory_ids:
            self.trajectory_ids.append(trajectory_id)
        self.update_timestamp()

    def record_reasoning_experiment(self, experiment_dict: Dict[str, Any]) -> None:
        experiment_id = experiment_dict.get("experiment_id")
        for index, existing in enumerate(self.reasoning_experiments):
            if experiment_id and existing.get("experiment_id") == experiment_id:
                if existing.get("prediction_digest") != experiment_dict.get("prediction_digest"):
                    raise ValueError(
                        "Reasoning experiment ID cannot be reassigned to a new definition."
                    )
                if existing.get("completed_at") and existing != experiment_dict:
                    raise ValueError("Completed reasoning experiment accounting is immutable.")
                self.reasoning_experiments[index] = dict(experiment_dict)
                self.update_timestamp()
                return
        self.reasoning_experiments.append(dict(experiment_dict))
        self.update_timestamp()

    def record_trajectory_observation(self, observation_dict: Dict[str, Any]) -> None:
        # Deduplicate by fingerprint
        fingerprint = observation_dict.get("fingerprint")
        if fingerprint and any(o.get("fingerprint") == fingerprint for o in self.trajectory_observations):
            return
        self.trajectory_observations.append(observation_dict)
        self.update_timestamp()

    def record_local_success(self, ram_gib: Optional[float] = None) -> None:
        lm = self.ensure_local_model_defaults()
        lm["success_count"] = lm.get("success_count", 0) + 1
        if ram_gib is not None:
            lm["last_available_ram_gib"] = ram_gib
        self.update_timestamp()

    def record_local_failure(self, ram_gib: Optional[float] = None) -> None:
        lm = self.ensure_local_model_defaults()
        lm["failure_count"] = lm.get("failure_count", 0) + 1
        if ram_gib is not None:
            lm["last_available_ram_gib"] = ram_gib
        self.update_timestamp()

    def record_local_escalation(self) -> None:
        lm = self.ensure_local_model_defaults()
        lm["escalations"] = lm.get("escalations", 0) + 1
        self.update_timestamp()

    def increment_local_only_iteration(self) -> int:
        """Increments and returns the consecutive local-only continuation counter."""
        lm = self.ensure_local_model_defaults()
        lm["local_only_iterations"] = lm.get("local_only_iterations", 0) + 1
        self.update_timestamp()
        return lm["local_only_iterations"]

    def reset_local_only_iterations(self) -> None:
        """Resets the counter once cloud capacity is available again (not on local success)."""
        lm = self.ensure_local_model_defaults()
        lm["local_only_iterations"] = 0
        self.update_timestamp()

    def local_only_budget_reached(self) -> bool:
        lm = self.ensure_local_model_defaults()
        return lm.get("local_only_iterations", 0) >= lm.get("local_only_iteration_limit", DEFAULT_LOCAL_ONLY_ITERATION_LIMIT)

    def record_provider_invocation(self, agent_id: str, role: str = "implementation") -> None:
        """
        Records one *actual* provider execution. `role` is one of
        "implementation", "remediation", or "review" (#59.1 Phase 8). Callers
        must only reach here when a real AgentExecutionResult was produced --
        never merely because a role mapping named this provider.
        """
        self.provider_invocations[agent_id] = self.provider_invocations.get(agent_id, 0) + 1
        by_role = self.provider_role_invocations.setdefault(agent_id, {})
        by_role[role] = by_role.get(role, 0) + 1
        self.update_timestamp()

    def record_engineering_attempt(self, attempt: Dict[str, Any]) -> None:
        """
        Appends one provider attempt at a governed engineering task. Attempts
        are append-only and persisted immediately by the caller so the history
        survives a crash or restart mid-failover (#59.1 Phase 2/4).
        """
        self.engineering_attempts.append(attempt)
        self.update_timestamp()

    def attempts_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        return [a for a in self.engineering_attempts if a.get("task_id") == task_id]

    def record_benchmark_result(self, result_dict: Dict[str, Any]) -> None:
        self.benchmark_history.append(result_dict)
        self.iterations_attempted = len(self.benchmark_history)
        self.iterations_succeeded = sum(1 for r in self.benchmark_history if r.get("success", False))
        self.iterations_failed = self.iterations_attempted - self.iterations_succeeded
        self.repair_cycles_total += result_dict.get("repair_cycles", 0)
        self.update_timestamp()

    def record_task_completed(self, task_dict: Dict[str, Any]) -> None:
        self.completed_tasks.append(task_dict)
        self.update_timestamp()

    def record_task_failed(self, task_dict: Dict[str, Any]) -> None:
        self.failed_tasks.append(task_dict)
        self.update_timestamp()

    def record_gap_falsification(self, record: Dict[str, Any]) -> None:
        """
        Records one cross-provider falsification round for an ambiguous
        synthesis failure (#59.1 Phase 5), whatever its verdict. Kept even
        when the verdict was "not a framework gap" -- that negative result is
        the evidence that HowlPlane correctly declined to modify itself.
        """
        self.gap_falsifications.append(record)
        self.update_timestamp()

    def record_provider_specific_failure(self, record: Dict[str, Any]) -> None:
        """
        Records a synthesis failure attributed to a provider rather than to
        the framework. Deliberately NOT a framework gap: nothing here opens a
        governed self-improvement task.
        """
        self.provider_specific_failures.append(record)
        self.update_timestamp()

    def record_framework_gap(self, gap_dict: Dict[str, Any]) -> None:
        # Avoid duplicate gaps by code
        code = gap_dict.get("code")
        if not any(g.get("code") == code for g in self.framework_gaps):
            self.framework_gaps.append(gap_dict)
            self.update_timestamp()

    def record_git_integration(self, git_rec: Dict[str, Any]) -> None:
        """
        Records/updates a task's GitIntegrationRecord. Deduplicates by
        task_id: a task's git integration record is progressively updated
        as real steps complete (#59 Phase 2/22), so a later call for the
        same task_id replaces the prior entry rather than appending a
        duplicate.
        """
        task_id = git_rec.get("task_id")
        self.git_records = [g for g in self.git_records if g.get("task_id") != task_id]
        self.git_records.append(git_rec)
        self.update_timestamp()

    def record_park(self, parked_record: Dict[str, Any]) -> None:
        """Records a task parked awaiting human authority (#59 Phase 12)."""
        task_id = parked_record.get("task_id")
        if not any(p.get("task_id") == task_id for p in self.parked_tasks):
            self.parked_tasks.append(parked_record)
        self.pending_human_decisions = list(self.parked_tasks)
        self.update_timestamp()

    def is_task_parked(self, task_id: str) -> bool:
        return any(p.get("task_id") == task_id for p in self.parked_tasks)

    def increment_merge_count(self) -> int:
        self.merges_this_campaign += 1
        self.update_timestamp()
        return self.merges_this_campaign

    def merge_budget_reached(self, envelope: Optional[Dict[str, Any]] = None) -> bool:
        env = envelope or self.authority_envelope
        max_merges = env.get("max_merges", 0) if env else 0
        return self.merges_this_campaign >= max_merges

    def save(self, run_dir: Union[str, Path]) -> Path:
        """Atomically saves campaign state and markdown report to directory."""
        target_dir = Path(run_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        self.update_timestamp()
        state_file = target_dir / "campaign_state.json"
        atomic_write_json(state_file, self.to_dict())
        summary_file = target_dir / "campaign_summary.md"
        atomic_write_text(summary_file, self.render_markdown())
        return state_file

    @classmethod
    def load(cls, run_dir: Union[str, Path]) -> "DurableCampaignState":
        target_dir = Path(run_dir).resolve()
        state_file = target_dir / "campaign_state.json"
        if not state_file.is_file():
            raise FileNotFoundError(f"Campaign state file not found at {state_file}")
        data = safe_load_json(state_file)
        return cls.from_dict(data)

    def render_markdown(self) -> str:
        lines = [
            f"# HowlPlane Marathon Dogfooding Report: `{self.campaign_id}`",
            "",
            f"- **Schema:** `{self.schema}`",
            f"- **Started At:** `{self.started_at}`",
            f"- **Updated At:** `{self.updated_at}`",
            f"- **Stop Reason:** `{self.stop_reason}`",
            f"- **Total Duration:** `{self.total_duration_seconds}s`",
            f"- **Iterations:** {self.iterations_succeeded}/{self.iterations_attempted} succeeded ({self.iterations_failed} failed)",
            f"- **Total Repair Cycles:** {self.repair_cycles_total}",
            f"- **Next Action:** `{self.next_action}`",
            f"- **Requested Benchmark Scope:** `{', '.join(self.requested_benchmarks) or 'N/A'}`",
            "",
            "## Provider Status & Invocations",
            "",
            "| Provider | Live State | Invocations |",
            "| --- | --- | --- |",
        ]
        all_providers = sorted(set(list(self.provider_states.keys()) + list(self.provider_invocations.keys())))
        for p in all_providers:
            st = self.provider_states.get(p, "UNKNOWN")
            inv = self.provider_invocations.get(p, 0)
            lines.append(f"| `{p}` | `{st}` | {inv} |")

        lines.extend([
            "",
            "## Benchmarks & Products Created",
            "",
            "| Benchmark | Product | Success | Checks | Repairs | Duration | Provider | Bundle Path |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for b in self.benchmark_history:
            mark = "✓ PASS" if b.get("success") else "✗ FAIL"
            checks = f"{b.get('passed_checks', 0)}/{b.get('total_checks', 0)}"
            bpath = b.get("bundle_path") or "N/A"
            lines.append(
                f"| {b.get('benchmark_id')} | {b.get('product_name')} | {mark} | {checks} | "
                f"{b.get('repair_cycles', 0)} | {b.get('duration_seconds', 0)}s | "
                f"`{b.get('implementing_provider', 'unknown')}` | `{bpath}` |"
            )

        if self.local_model:
            lm = self.local_model
            lines.extend([
                "",
                "## Local Model Participation",
                "",
                f"- **Provider/Model:** `{lm.get('provider')}` / `{lm.get('model')}`",
                f"- **Context Length:** {lm.get('context_length')}",
                f"- **Local Success/Failure:** {lm.get('success_count', 0)}/{lm.get('failure_count', 0)}",
                f"- **Escalations to Cloud:** {lm.get('escalations', 0)}",
                f"- **Local-Only Continuation:** {lm.get('local_only_iterations', 0)}/{lm.get('local_only_iteration_limit')}",
                f"- **Last Observed Available RAM:** {lm.get('last_available_ram_gib')} GiB",
                f"- **Current Availability:** `{lm.get('current_availability')}`",
            ])

        if self.framework_gaps:
            lines.extend([
                "",
                "## Framework & Runtime Gaps Discovered",
                "",
                "| Gap Code | Target Component | Description | Impact |",
                "| --- | --- | --- | --- |",
            ])
            for g in self.framework_gaps:
                lines.append(
                    f"| `{g.get('code')}` | `{g.get('target_component')}` | {g.get('required_behavior')} | `{g.get('impact')}` |"
                )

        if self.completed_tasks or self.failed_tasks:
            lines.extend([
                "",
                "## Governed Engineering Tasks",
                "",
                "| Task ID | Status | Objective | Provider | Remediations |",
                "| --- | --- | --- | --- | --- |",
            ])
            for t in self.completed_tasks:
                lines.append(
                    f"| `{t.get('task_id')}` | ✓ COMPLETED | {t.get('objective', '')[:50]} | `{t.get('provider')}` | {t.get('remediations', 0)} |"
                )
            for t in self.failed_tasks:
                lines.append(
                    f"| `{t.get('task_id')}` | ✗ FAILED | {t.get('objective', '')[:50]} | `{t.get('provider')}` | {t.get('error', '')[:30]} |"
                )

        if self.engineering_attempts:
            lines.extend([
                "",
                "## Engineering Provider Attempts",
                "",
                "Why the provider changed, in the order it was tried.",
                "",
                "| Task ID | # | Provider | Result | Failure Class | Delta Reconciled | Evidence |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ])
            for a in self.engineering_attempts:
                reconciled = "yes" if a.get("delta_reconciled") else "no"
                lines.append(
                    f"| `{a.get('task_id')}` | {a.get('attempt_index')} | `{a.get('provider')}` | "
                    f"`{a.get('result')}` | `{a.get('failure_class') or '-'}` | {reconciled} | "
                    f"{a.get('error_digest', '')[:80]} |"
                )

        if self.gap_falsifications:
            lines.extend([
                "",
                "## Framework-Gap Evidence",
                "",
                "Ambiguous synthesis failures are falsified against an independent",
                "provider before HowlPlane may modify itself.",
                "",
                "| Benchmark | Failure | Failing Provider | Verdict | Confirmations |",
                "| --- | --- | --- | --- | --- |",
            ])
            for f in self.gap_falsifications:
                tried = ", ".join(
                    f"{a.get('provider')}:{a.get('outcome')}" for a in f.get("attempts", [])
                ) or "none"
                lines.append(
                    f"| {f.get('benchmark')} | `{f.get('gap_type')}` | `{f.get('failing_provider')}` | "
                    f"`{f.get('evidence')}` | {tried} |"
                )

        if self.provider_role_invocations:
            lines.extend([
                "",
                "## Provider Invocations by Role",
                "",
                "Counted only where a provider actually executed -- a reviewer",
                "mapping alone never appears here.",
                "",
                "| Provider | Implementation | Remediation | Review | Total |",
                "| --- | --- | --- | --- | --- |",
            ])
            for provider in sorted(self.provider_role_invocations):
                roles = self.provider_role_invocations[provider]
                lines.append(
                    f"| `{provider}` | {roles.get('implementation', 0)} | {roles.get('remediation', 0)} | "
                    f"{roles.get('review', 0)} | {self.provider_invocations.get(provider, 0)} |"
                )

        if self.git_records:
            lines.extend([
                "",
                "## Git Commits, Pull Requests & Merges",
                "",
                "| Task ID | Repo | Mode | Branch | Commit SHA | PR | CI Status | Merged |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ])
            for gr in self.git_records:
                pr_str = f"#{gr.get('pr_number')}" if gr.get("pr_number") else "N/A"
                merged_str = "✓ MERGED" if gr.get("merged") else "PENDING"
                commit_sha = gr.get("commit_sha") or ""
                lines.append(
                    f"| `{gr.get('task_id')}` | `{gr.get('target_repo')}` | `{gr.get('integration_mode', 'simulated')}` | "
                    f"`{gr.get('branch') or 'N/A'}` | `{commit_sha[:8]}` | {pr_str} | `{gr.get('ci_status')}` | {merged_str} |"
                )

        if self.pending_human_decisions:
            lines.extend([
                "",
                "## Pending Human Decisions",
                "",
                "| Task ID | Objective | Boundary | Repo | Recommended | Blocks Other Work |",
                "| --- | --- | --- | --- | --- | --- |",
            ])
            for pd in self.pending_human_decisions:
                blocks_str = "⚠ YES" if pd.get("blocks_other_work") else "no"
                lines.append(
                    f"| `{pd.get('task_id')}` | {pd.get('objective', '')[:60]} | `{pd.get('boundary_type')}` | "
                    f"`{pd.get('repository')}` | `{pd.get('recommended_action')}` | {blocks_str} |"
                )

        if self.reviewer_diversity_records:
            lines.extend([
                "",
                "## Reviewer Diversity Verification",
                "",
                "| Task/Product | Implementer | Reviewers | Independent Diversity Achieved |",
                "| --- | --- | --- | --- |",
            ])
            for rd in self.reviewer_diversity_records:
                div_str = "✓ YES" if rd.get("diversity_achieved") else "⚠ REDUCED (Single Provider)"
                revs = ", ".join(f"{k}:{v}" for k, v in rd.get("reviewers", {}).items())
                lines.append(
                    f"| `{rd.get('target')}` | `{rd.get('implementer')}` | `{revs}` | {div_str} |"
                )

        lines.extend([
            "",
            "## Resume Command",
            "",
            "To resume or continue this dogfood campaign with updated quotas:",
            "```bash",
            f"ai dogfood --resume {self.campaign_id}",
            "```",
            "",
        ])
        return "\n".join(lines)
