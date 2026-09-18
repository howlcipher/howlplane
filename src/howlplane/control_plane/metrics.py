#!/usr/bin/env python3
"""
metrics.py

Transparent calculation of agent, reviewer, and control-plane engineering metrics
from actual engineering history recorded in the evidence ledger.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from howlplane.control_plane.evidence_ledger import EvidenceEntry
from howlplane.control_plane.task_spec import DataClassSerializationMixin

# Minimum observations required before historical performance can inform automated routing
MIN_ROUTING_SAMPLE_SIZE = 10


@dataclass
class ReviewerMetricSummary(DataClassSerializationMixin):
    """Observable statistics for a specialized reviewer role."""

    role_id: str
    reviewer_runs: int = 0
    findings_total: int = 0
    confirmed_findings: int = 0
    likely_findings: int = 0
    false_positives: int = 0
    disputed_findings: int = 0
    blockers_found: int = 0
    high_findings_found: int = 0
    unique_findings: int = 0
    findings_that_triggered_remediation: int = 0
    findings_that_prevented_bad_completion: int = 0
    marginal_value: int = 0

    @property
    def signal_rate(self) -> float:
        """Rate of actionable (confirmed + likely) findings vs total."""
        actionable = self.confirmed_findings + self.likely_findings
        return round(actionable / self.findings_total, 2) if self.findings_total > 0 else 0.0

    @property
    def false_positive_rate(self) -> float:
        return round(self.false_positives / self.findings_total, 2) if self.findings_total > 0 else 0.0


@dataclass
class AgentMetricSummary(DataClassSerializationMixin):
    """Observable statistics for an individual agent type."""

    agent_id: str
    tasks_worked: int = 0
    tasks_completed: int = 0
    tasks_abandoned: int = 0
    first_pass_successes: int = 0
    verification_failures: int = 0
    review_findings_received: int = 0
    confirmed_findings_received: int = 0
    blocker_findings_received: int = 0
    human_interventions: int = 0
    remediation_cycles: int = 0
    control_plane_defects_caught: int = 0
    task_classes_worked: Dict[str, int] = field(default_factory=dict)
    total_elapsed_seconds: Optional[float] = None
    total_tokens: Optional[int] = None
    total_cost: Optional[float] = None
    session_count: Optional[int] = None

    @property
    def completion_rate(self) -> float:
        return round(self.tasks_completed / self.tasks_worked, 2) if self.tasks_worked > 0 else 0.0

    @property
    def first_pass_success_rate(self) -> float:
        return round(self.first_pass_successes / self.tasks_worked, 2) if self.tasks_worked > 0 else 0.0


@dataclass
class PerformanceMetricsSummary:
    """Aggregate statistics across all engineering tasks and reviewer roles."""

    total_tasks: int = 0
    completed_tasks: int = 0
    abandoned_tasks: int = 0
    first_pass_successes: int = 0
    tasks_requiring_remediation: int = 0
    control_plane_caught_defects: int = 0
    review_caught_defects: int = 0
    verification_caught_defects: int = 0
    boundary_caught_risks: int = 0
    routing_recommendations_followed: int = 0
    routing_overrides: int = 0
    verification_failures: int = 0
    total_review_findings: int = 0
    confirmed_review_findings: int = 0
    blocker_findings: int = 0
    high_findings: int = 0
    human_interventions: int = 0
    rework_cycles_total: int = 0
    slop_checks_run: int = 0
    slop_checks_failed: int = 0
    duplication_regressions_caught: int = 0
    orphans_caught: int = 0
    stale_tombstones_caught: int = 0
    claim_gaps_caught: int = 0
    slop_findings_remediated: int = 0
    debt_acceptance_requests: int = 0
    debt_acceptance_approved: int = 0
    debt_acceptance_rejected: int = 0
    hygiene_policies_tightened: int = 0
    hygiene_policies_weakened: int = 0
    hygiene_ceilings_lowered: int = 0
    hygiene_ceiling_raise_attempts: int = 0
    hygiene_tombstones_added: int = 0
    hygiene_tombstones_removed: int = 0
    hygiene_provider_mismatches: int = 0
    reviewer_summaries: Dict[str, ReviewerMetricSummary] = field(default_factory=dict)
    agent_summaries: Dict[str, AgentMetricSummary] = field(default_factory=dict)
    repositories_exercised: Dict[str, int] = field(default_factory=dict)
    orchestration_counts: Dict[str, int] = field(default_factory=dict)
    common_failure_modes: Dict[str, int] = field(default_factory=dict)

    @property
    def first_pass_success_rate(self) -> float:
        return (
            round(self.first_pass_successes / self.total_tasks, 2)
            if self.total_tasks > 0
            else 0.0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "abandoned_tasks": self.abandoned_tasks,
            "first_pass_success_rate": self.first_pass_success_rate,
            "first_pass_successes": self.first_pass_successes,
            "tasks_requiring_remediation": self.tasks_requiring_remediation,
            "control_plane_caught_defects": self.control_plane_caught_defects,
            "review_caught_defects": self.review_caught_defects,
            "verification_caught_defects": self.verification_caught_defects,
            "boundary_caught_risks": self.boundary_caught_risks,
            "routing_recommendations_followed": self.routing_recommendations_followed,
            "routing_overrides": self.routing_overrides,
            "verification_failures": self.verification_failures,
            "total_review_findings": self.total_review_findings,
            "confirmed_review_findings": self.confirmed_review_findings,
            "blocker_findings": self.blocker_findings,
            "high_findings": self.high_findings,
            "human_interventions": self.human_interventions,
            "rework_cycles_total": self.rework_cycles_total,
            "slop_checks_run": self.slop_checks_run,
            "slop_checks_failed": self.slop_checks_failed,
            "duplication_regressions_caught": self.duplication_regressions_caught,
            "orphans_caught": self.orphans_caught,
            "stale_tombstones_caught": self.stale_tombstones_caught,
            "claim_gaps_caught": self.claim_gaps_caught,
            "slop_findings_remediated": self.slop_findings_remediated,
            "debt_acceptance_requests": self.debt_acceptance_requests,
            "debt_acceptance_approved": self.debt_acceptance_approved,
            "debt_acceptance_rejected": self.debt_acceptance_rejected,
            "hygiene_policies_tightened": self.hygiene_policies_tightened,
            "hygiene_policies_weakened": self.hygiene_policies_weakened,
            "hygiene_ceilings_lowered": self.hygiene_ceilings_lowered,
            "hygiene_ceiling_raise_attempts": self.hygiene_ceiling_raise_attempts,
            "hygiene_tombstones_added": self.hygiene_tombstones_added,
            "hygiene_tombstones_removed": self.hygiene_tombstones_removed,
            "hygiene_provider_mismatches": self.hygiene_provider_mismatches,
            "repositories_exercised": self.repositories_exercised,
            "orchestration_counts": self.orchestration_counts,
            "common_failure_modes": self.common_failure_modes,
            "agent_summaries": {k: v.to_dict() for k, v in self.agent_summaries.items()},
            "reviewer_summaries": {k: v.to_dict() for k, v in self.reviewer_summaries.items()},
        }

    def render_markdown(self) -> str:
        """Renders the metrics summary in standard operational format."""
        fps_pct = (self.first_pass_successes / self.total_tasks * 100) if self.total_tasks > 0 else 0.0
        lines = [
            "# Multi-Agent Engineering Control Plane Operational Report",
            "",
            "## Aggregate Overview",
            f"- **Repositories exercised:** {len(self.repositories_exercised)}",
            f"- **Tasks completed:** {self.completed_tasks}/{self.total_tasks}",
            f"- **Tasks abandoned / failed:** {self.abandoned_tasks}",
            f"- **First-pass success rate:** {fps_pct:.1f}% ({self.first_pass_successes}/{self.total_tasks} tasks)",
            f"- **Tasks requiring remediation:** {self.tasks_requiring_remediation}",
            f"- **Control-plane caught defects:** {self.control_plane_caught_defects} (Review: {self.review_caught_defects}, Verification: {self.verification_caught_defects}, Boundary: {self.boundary_caught_risks})",
            f"- **Routing decisions:** {self.routing_recommendations_followed} recommended followed, {self.routing_overrides} operator overrides",
            f"- **Human interventions required:** {self.human_interventions}",
            "",
            "## Implementation Agent Performance",
            "| Agent | Worked | Completed | First-Pass Success | Remediation | Verif Failures | Findings Recv | Blockers | Human Gates |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for aid, summary in sorted(self.agent_summaries.items()):
            fps_str = f"{summary.first_pass_success_rate * 100:.0f}%" if summary.tasks_worked >= MIN_ROUTING_SAMPLE_SIZE else f"{summary.first_pass_success_rate * 100:.0f}%*"
            lines.append(
                f"| `{aid}` | {summary.tasks_worked} | {summary.tasks_completed} | "
                f"{fps_str} | {summary.remediation_cycles} | {summary.verification_failures} | "
                f"{summary.review_findings_received} | {summary.blocker_findings_received} | {summary.human_interventions} |"
            )
        lines.append("")
        lines.append(f"*\\*Note: Sample size < {MIN_ROUTING_SAMPLE_SIZE} indicates insufficient sample size for automated routing.*")
        lines.append("")

        lines.extend([
            "## Reviewer Effectiveness & Marginal Value Breakdown",
            "| Reviewer Role | Runs | Findings | Confirmed | Likely | False Pos | Disputed | Blockers | Unique | Triggered Fix | Prevented Bad Completion | Marginal Value |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for rid, rsum in sorted(self.reviewer_summaries.items()):
            lines.append(
                f"| `{rid}` | {rsum.reviewer_runs} | {rsum.findings_total} | {rsum.confirmed_findings} | "
                f"{rsum.likely_findings} | {rsum.false_positives} | {rsum.disputed_findings} | {rsum.blockers_found} | "
                f"{rsum.unique_findings} | {rsum.findings_that_triggered_remediation} | {rsum.findings_that_prevented_bad_completion} | {rsum.marginal_value} |"
            )
        lines.append("")

        if (
            self.slop_checks_run > 0
            or self.duplication_regressions_caught > 0
            or self.debt_acceptance_requests > 0
            or self.slop_findings_remediated > 0
            or self.hygiene_ceilings_lowered > 0
            or self.hygiene_ceiling_raise_attempts > 0
        ):
            lines.extend([
                "## Repository Hygiene & Slop Gate Tracking",
                f"- **Hygiene checks executed:** {self.slop_checks_run} ({self.slop_checks_failed} failed)",
                f"- **Duplication regressions caught:** {self.duplication_regressions_caught}",
                f"- **Ceilings lowered (debt reduced):** {self.hygiene_ceilings_lowered}",
                f"- **Ceiling raise attempts blocked:** {self.hygiene_ceiling_raise_attempts}",
                f"- **Policy tightenings:** {self.hygiene_policies_tightened}, **Weakenings requested:** {self.hygiene_policies_weakened}",
                f"- **Tombstones added (debt accepted):** {self.hygiene_tombstones_added}, **Removed (cleaned):** {self.hygiene_tombstones_removed}",
                f"- **Orphan surfaces caught:** {self.orphans_caught}",
                f"- **Stale tombstones caught:** {self.stale_tombstones_caught}",
                f"- **Contract / claims gaps caught:** {self.claim_gaps_caught}",
                f"- **Provider integrity mismatches:** {self.hygiene_provider_mismatches}",
                f"- **Slop findings remediated:** {self.slop_findings_remediated}",
                f"- **Debt acceptance requests:** {self.debt_acceptance_requests} ({self.debt_acceptance_approved} approved, {self.debt_acceptance_rejected} rejected)",
                "",
            ])

        if self.repositories_exercised:
            lines.extend([
                "## Repositories Exercised Breakdown",
                "| Repository | Tasks Recorded |",
                "| --- | --- |",
            ])
            for repo, count in sorted(self.repositories_exercised.items()):
                lines.append(f"| `{repo}` | {count} |")
            lines.append("")

        if self.orchestration_counts:
            lines.extend([
                "## Human Orchestration & Operator Friction Tracking",
                "| Orchestration Action | Count |",
                "| --- | --- |",
            ])
            for act, count in sorted(self.orchestration_counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"| `{act}` | {count} |")
            lines.append("")

        if self.common_failure_modes:
            lines.append("## Most Common Failure Modes Caught")
            for fail_mode, count in sorted(self.common_failure_modes.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"- **{fail_mode}**: {count} occurrence(s)")
            lines.append("")

        return "\n".join(lines)


class MetricsCalculator:
    """Calculates verifiable performance statistics from evidence ledger entries."""

    @staticmethod
    def calculate(entries: List[EvidenceEntry]) -> PerformanceMetricsSummary:
        """
        Parses evidence ledger entries and constructs a PerformanceMetricsSummary
        with strict first_pass_success semantics.
        Independent properties are processed independently without mutually exclusive suppression.
        """
        summary = PerformanceMetricsSummary()
        if not entries:
            return summary

        # Group entries by task
        tasks: Dict[str, List[EvidenceEntry]] = {}
        for entry in entries:
            tasks.setdefault(entry.task_id, []).append(entry)

        summary.total_tasks = len(tasks)

        for task_id, task_entries in tasks.items():
            task_agents = set()
            repo_name = None
            is_task_override = False

            for e in task_entries:
                if e.agent_id:
                    task_agents.add(e.agent_id)
                if e.implementing_agent:
                    task_agents.add(e.implementing_agent)
                if e.actual_agent:
                    task_agents.add(e.actual_agent)
                if e.repository:
                    repo_name = e.repository
                elif isinstance(e.metadata, dict) and "repository" in e.metadata:
                    repo_name = e.metadata["repository"]
                if e.is_override is True or (isinstance(e.metadata, dict) and e.metadata.get("is_override") is True):
                    is_task_override = True

            if repo_name:
                summary.repositories_exercised[repo_name] = summary.repositories_exercised.get(repo_name, 0) + 1

            if is_task_override:
                summary.routing_overrides += 1
            else:
                summary.routing_recommendations_followed += 1

            for aid in task_agents:
                if aid not in summary.agent_summaries:
                    summary.agent_summaries[aid] = AgentMetricSummary(agent_id=aid)
                summary.agent_summaries[aid].tasks_worked += 1

            # Task level tracking flags
            has_remediation = False
            has_verif_failure = False
            has_confirmed_blocker_or_high = False
            has_human_intervention = False
            is_completed = False
            is_abandoned = False
            task_defects_caught = 0
            task_remediation_cycles = 0

            for e in task_entries:
                aid = e.actual_agent or e.implementing_agent or e.agent_id
                agent_sum = summary.agent_summaries.get(aid)

                # 1. Track task class
                if e.task_class and agent_sum:
                    agent_sum.task_classes_worked[e.task_class] = (
                        agent_sum.task_classes_worked.get(e.task_class, 0) + 1
                    )

                # 2. Track orchestration action
                orch_act = e.orchestration_action or (e.metadata.get("orchestration_action") if isinstance(e.metadata, dict) else None)
                if orch_act:
                    summary.orchestration_counts[orch_act] = summary.orchestration_counts.get(orch_act, 0) + 1

                # 3. Track defect types
                if e.defect_type == "review_caught_defect" or (e.action == "control_plane_defect_caught" and isinstance(e.metadata, dict) and (e.metadata.get("reviewer_role") or "review" in e.metadata.get("failure_mode", ""))):
                    summary.review_caught_defects += 1
                elif e.defect_type == "verification_caught_defect" or (e.action == "verification_executed" and e.result == "failed"):
                    summary.verification_caught_defects += 1
                elif e.defect_type == "boundary_caught_risk" or (e.action in ("boundary_checked", "boundary_triggered") and isinstance(e.metadata, dict) and e.metadata.get("boundary_triggered")):
                    summary.boundary_caught_risks += 1

                # 4. Track repository hygiene / slop metrics
                is_slop_check = e.action == "slop_check_executed" or (
                    e.action == "verification_executed"
                    and (
                        (e.command and "slopslint" in e.command)
                        or (isinstance(e.metadata, dict) and e.metadata.get("category") == "repository_hygiene")
                    )
                )
                if is_slop_check:
                    summary.slop_checks_run += 1
                    if e.result == "failed":
                        summary.slop_checks_failed += 1

                if e.action == "debt_acceptance_requested" or (
                    e.action in ("boundary_triggered", "boundary_checked")
                    and isinstance(e.metadata, dict)
                    and "slop_debt_acceptance" in (e.metadata.get("boundary_triggered") or [])
                ):
                    summary.debt_acceptance_requests += 1

                if e.action == "debt_acceptance_approved" or (
                    e.action == "human_decision"
                    and e.result in ("approved", "accepted")
                    and isinstance(e.metadata, dict)
                    and "slop_debt_acceptance" in (e.metadata.get("boundary_triggers") or [])
                ):
                    summary.debt_acceptance_approved += 1

                if e.action == "debt_acceptance_rejected" or (
                    e.action == "human_decision"
                    and e.result in ("rejected", "denied")
                    and isinstance(e.metadata, dict)
                    and "slop_debt_acceptance" in (e.metadata.get("boundary_triggers") or [])
                ):
                    summary.debt_acceptance_rejected += 1

                if e.action == "hygiene_policy_tightened":
                    summary.hygiene_policies_tightened += 1

                if e.action in ("hygiene_policy_weakening_requested", "hygiene_policy_weakened"):
                    summary.hygiene_policies_weakened += 1

                if e.action == "hygiene_ceiling_lowered":
                    summary.hygiene_ceilings_lowered += 1

                if e.action == "hygiene_ceiling_raise_attempted":
                    summary.hygiene_ceiling_raise_attempts += 1

                if e.action == "hygiene_tombstone_added":
                    summary.hygiene_tombstones_added += 1

                if e.action == "hygiene_tombstone_removed":
                    summary.hygiene_tombstones_removed += 1

                if e.action == "hygiene_provider_mismatch" or (
                    isinstance(e.metadata, dict) and e.metadata.get("failure_mode") == "hygiene_provider_mismatch"
                ):
                    summary.hygiene_provider_mismatches += 1

                if e.action == "slop_finding_remediated":
                    summary.slop_findings_remediated += 1

                # 5. Track control_plane_caught_defect & failure modes
                if e.control_plane_caught_defect is True or e.action == "control_plane_defect_caught" or (e.action == "verification_executed" and e.result == "failed"):
                    summary.control_plane_caught_defects += 1
                    task_defects_caught += 1
                    if agent_sum:
                        agent_sum.control_plane_defects_caught += 1
                    if isinstance(e.metadata, dict) and "failure_mode" in e.metadata:
                        fmode = e.metadata["failure_mode"]
                        summary.common_failure_modes[fmode] = summary.common_failure_modes.get(fmode, 0) + 1
                        if fmode in ("duplication_regression", "ceiling_regression"):
                            summary.duplication_regressions_caught += 1
                        elif fmode == "orphan_surface":
                            summary.orphans_caught += 1
                        elif fmode == "stale_tombstone":
                            summary.stale_tombstones_caught += 1
                        elif fmode == "contract_claim_gap":
                            summary.claim_gaps_caught += 1

                # 5. Track remediation cycles
                if e.remediation_cycles and e.remediation_cycles > 0:
                    has_remediation = True
                    task_remediation_cycles = max(task_remediation_cycles, e.remediation_cycles)

                if e.action == "remediation_started":
                    has_remediation = True
                elif e.action == "remediation_completed":
                    has_remediation = True
                    task_remediation_cycles += 1
                    summary.rework_cycles_total += 1
                    if agent_sum:
                        agent_sum.remediation_cycles += 1
                elif e.action == "reconciliation_completed":
                    if e.result and "remediated" in e.result.lower():
                        has_remediation = True
                        task_remediation_cycles += 1

                # 6. Track reviewer stats & findings
                if e.action == "review_submitted" or e.reviewing_agents:
                    reviewers_list = e.reviewing_agents or (
                        e.metadata.get("reviewers") if isinstance(e.metadata, dict) else None
                    )
                    if reviewers_list:
                        for r_role in reviewers_list:
                            if r_role not in summary.reviewer_summaries:
                                summary.reviewer_summaries[r_role] = ReviewerMetricSummary(role_id=r_role)
                            summary.reviewer_summaries[r_role].reviewer_runs += 1

                if e.findings_summary:
                    total_f = e.findings_summary.get("total", 0)
                    blockers = e.findings_summary.get("blocker", 0)
                    highs = e.findings_summary.get("high", 0)
                    confirmed = e.findings_summary.get("confirmed", 0)

                    summary.total_review_findings += total_f
                    summary.blocker_findings += blockers
                    summary.high_findings += highs
                    summary.confirmed_review_findings += confirmed

                    if agent_sum:
                        agent_sum.review_findings_received += total_f
                        agent_sum.confirmed_findings_received += confirmed
                        agent_sum.blocker_findings_received += blockers

                    if blockers > 0 or highs > 0:
                        has_confirmed_blocker_or_high = True

                if isinstance(e.metadata, dict) and "reviewers_breakdown" in e.metadata:
                    rb = e.metadata["reviewers_breakdown"]
                    for r_role, r_stats in rb.items():
                        if r_role not in summary.reviewer_summaries:
                            summary.reviewer_summaries[r_role] = ReviewerMetricSummary(role_id=r_role)
                        r_sum = summary.reviewer_summaries[r_role]
                        r_sum.findings_total += r_stats.get("findings_total", 0)
                        r_sum.confirmed_findings += r_stats.get("confirmed", 0)
                        r_sum.likely_findings += r_stats.get("likely", 0)
                        r_sum.false_positives += r_stats.get("false_positives", 0)
                        r_sum.disputed_findings += r_stats.get("disputed", 0)
                        r_sum.blockers_found += r_stats.get("blockers", 0)
                        r_sum.high_findings_found += r_stats.get("highs", 0)
                        r_sum.unique_findings += r_stats.get("unique", 0)
                        r_sum.findings_that_triggered_remediation += r_stats.get("triggered_remediation", 0)
                        r_sum.findings_that_prevented_bad_completion += r_stats.get("prevented_bad_completion", 0)

                # 7. Track verification
                if e.action == "verification_executed":
                    if e.result == "failed":
                        has_verif_failure = True
                        summary.verification_failures += 1
                        if agent_sum:
                            agent_sum.verification_failures += 1
                        fmode = "verification_failure"
                        summary.common_failure_modes[fmode] = summary.common_failure_modes.get(fmode, 0) + 1

                # 8. Track human decision
                if e.action == "human_decision" or e.human_decision is not None:
                    has_human_intervention = True
                    summary.human_interventions += 1
                    if agent_sum:
                        agent_sum.human_interventions += 1

                # 9. Track task closed
                if e.action == "task_closed":
                    if e.result == "complete":
                        is_completed = True
                        if agent_sum:
                            agent_sum.tasks_completed += 1
                    elif e.result in ("failed", "abandoned"):
                        is_abandoned = True
                        if agent_sum:
                            agent_sum.tasks_abandoned += 1

            if has_remediation:
                summary.tasks_requiring_remediation += 1

            if is_completed:
                summary.completed_tasks += 1
                is_first_pass = (
                    not has_remediation
                    and not has_verif_failure
                    and not has_confirmed_blocker_or_high
                    and not has_human_intervention
                    and task_remediation_cycles == 0
                )
                if is_first_pass:
                    summary.first_pass_successes += 1
                    for aid in task_agents:
                        if aid in summary.agent_summaries:
                            summary.agent_summaries[aid].first_pass_successes += 1
            elif is_abandoned:
                summary.abandoned_tasks += 1

        # Compute marginal value for each reviewer
        for r_role, r_sum in summary.reviewer_summaries.items():
            r_sum.marginal_value = (
                r_sum.unique_findings
                + r_sum.findings_that_triggered_remediation
                + r_sum.findings_that_prevented_bad_completion
                + r_sum.blockers_found
            )

        return summary

