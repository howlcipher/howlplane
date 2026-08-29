#!/usr/bin/env python3
"""
human_boundary.py

Human authority boundary enforcement and structured decision packet generation.
Guarantees that high-risk, destructive, financial, or security-sensitive actions
pause at AWAITING_HUMAN with complete evidence before proceeding.
Enforces that human approval is NOT execution, and completion requires verified receipts.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib, json, os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.git_env import run_git_in_repo
from src.control_plane.hygiene_policy import (
    PolicyChangeType,
    PolicyEvaluationResult,
)
from src.control_plane.proposed_action import (
    ProposedAction,
    infer_proposed_actions,
)
from src.control_plane.reconciliation import ReconciliationResult
from src.control_plane.task_spec import DataClassSerializationMixin, TaskSpec
from src.control_plane.verification import VerificationPlan


class HumanLifecycleError(Exception):
    """Base exception for human decision and lifecycle errors."""
    pass


class TaskRunNotFoundError(HumanLifecycleError):
    """Raised when the specified task run directory is not found."""
    pass


class InvalidTaskStateError(HumanLifecycleError):
    """Raised when an operation is invalid for the current task state."""
    pass


class ContradictoryDecisionError(HumanLifecycleError):
    """Raised when a decision contradicts a previous immutable decision."""
    pass


class RepositoryDriftError(HumanLifecycleError):
    """Raised when repository state drifts between review and approval/resume."""
    pass


class StaleApprovalError(HumanLifecycleError):
    """Raised when repository drift invalidates an existing approval."""
    pass


class ApprovalRequiredError(HumanLifecycleError):
    """Raised when resuming a task that has not been approved."""
    pass


class UnsupportedActionError(HumanLifecycleError):
    """Raised when an authorized action has no supporting bounded executor."""
    pass


class ExecutionFailedError(HumanLifecycleError):
    """Raised when bounded execution fails or returns verification failure."""
    pass


class InvalidReceiptError(HumanLifecycleError):
    """Raised when an execution receipt is invalid, forged, or mismatched."""
    pass


HUMAN_BOUNDARY_TRIGGERS = {
    "production_deployment": "Production deployment or live environment modification",
    "infrastructure_apply": "Infrastructure-as-code apply (e.g. terraform apply, k8s apply)",
    "destructive_database_change": "Destructive database migration (DROP TABLE/COLUMN, truncate)",
    "credential_provisioning": "Credential, token, or secret creation/rotation",
    "paid_service_usage": "Paid API or service consumption requiring user billing approval",
    "external_dependency_addition": "Adding new external dependencies or modifying license scope",
    "external_messaging": "Sending outbound emails, messages, webhooks, or notifications",
    "package_publishing": "Publishing packages or binaries to public registries or releases",
    "job_submission": "Submitting job applications, resumes, or external forms",
    "security_policy_exception": "Overriding or relaxing established security or anti-manipulation rules",
    "non_independent_review": (
        "The implementer reviewed its own change for one or more roles, so the change is not independently reviewed"
    ),
    "review_incomplete": "At least one reviewer never completed, so the change is under-reviewed",
    "slop_debt_acceptance": (
        "Accepting new repository debt tombstone or repointing existing debt without prior policy authorization"
    ),
    "hygiene_policy_weakening": (
        "Weakening repository hygiene scan scope, ignore patterns, detector parameters, or provider settings"
    ),
    "hygiene_policy_violation": (
        "Prohibited repository hygiene policy violation (ceiling increase or configuration deletion)"
    ),
    "force_push": "Force push to a shared branch, overwriting remote history",
    "history_rewrite": "Rewriting committed git history (filter-branch, rebase over pushed commits, hard reset of shared refs)",
    "bypass_required_checks": "Merging or landing a change while bypassing required GitHub status checks",
    "branch_protection_weakening": "Weakening or removing GitHub branch protection / required status checks",
    "authority_profile_modification": "Modifying an AuthorityProfile definition (allowed/denied actions, TTL, limits)",
    "authority_enforcement_modification": (
        "Modifying the code that enforces delegated authority itself "
        "(human_boundary.py, authority_profile.py, authority_envelope.py, executor.py)"
    ),
}

EXECUTABLE_BOUNDARIES = {
    "infrastructure_apply",
    "destructive_database_change",
    "package_publishing",
    "production_deployment",
    "credential_provisioning",
    "external_messaging",
    "create_release_candidate",
}

# Boundaries that can NEVER be satisfied by delegated authority, no matter
# what an AuthorityEnvelope's allowed_action_classes contains (#59 Phase 10).
# Checked before any envelope lookup in evaluate_with_delegated_authority();
# an envelope cannot override this set by construction.
NEVER_DELEGATABLE_BOUNDARIES = {
    "force_push",
    "history_rewrite",
    "bypass_required_checks",
    "branch_protection_weakening",
    "production_deployment",
    "infrastructure_apply",
    "destructive_database_change",
    "credential_provisioning",
    "package_publishing",
    "external_messaging",
    "job_submission",
    "paid_service_usage",
    "external_dependency_addition",
    "security_policy_exception",
    "hygiene_policy_weakening",
    "slop_debt_acceptance",
    "authority_profile_modification",
    "authority_enforcement_modification",
}


@dataclass
class HumanDecisionPacket(DataClassSerializationMixin):
    """Concise decision packet presented to a human operator for sign-off."""

    task_id: str
    objective: str
    change_summary: str
    boundary_triggers: List[str]
    evidence: List[str]
    risks: List[str]
    review_findings_summary: Dict[str, int]
    verification_status: str
    recommended_action: str
    proposed_actions: List[Dict[str, Any]] = field(default_factory=list)
    executor_id: Optional[str] = None
    changeops_decision_id: Optional[str] = None

    def render_markdown(self) -> str:
        triggers_str = "\n".join(f"- ⚠️ **{t}**: {HUMAN_BOUNDARY_TRIGGERS.get(t, t)}" for t in self.boundary_triggers)
        evidence_str = "\n".join(f"- {e}" for e in self.evidence) or "None recorded."
        risks_str = "\n".join(f"- {r}" for r in self.risks) or "None recorded."
        findings_text = (
            f"{self.review_findings_summary.get('total', 0)} total ("
            f"{self.review_findings_summary.get('blocker', 0)} blockers, "
            f"{self.review_findings_summary.get('high', 0)} highs)"
        )

        exec_section = ""
        if self.executor_id or self.changeops_decision_id:
            exec_section = f"""
## Bounded Execution Context
- **Executor:** `{self.executor_id or 'None'}`
- **ChangeOps Decision ID:** `{self.changeops_decision_id or 'None'}`
"""

        return f"""# 🛑 Human Authority Decision Packet: Task `{self.task_id}`

## Objective
{self.objective}

## Boundary Triggers (Human Authorization Required)
{triggers_str}
{exec_section}
## Proposed Change Summary
{self.change_summary}

## Verification Status
- **Automated Verification:** `{self.verification_status}`
- **Review Findings:** {findings_text}

## Key Evidence
{evidence_str}

## Identified Risks
{risks_str}

## Recommended Action
{self.recommended_action}

---
*Awaiting explicit human approval. Lack of response is treated as DENIED (fail-closed).*
"""


@dataclass
class BoundaryCheckResult:
    """Result of evaluating human authority boundaries."""

    requires_human_approval: bool
    triggered_boundaries: List[str] = field(default_factory=list)
    decision_packet: Optional[HumanDecisionPacket] = None


class HumanBoundaryGate:
    """Evaluates task intent, actions, and risk against human authority policies."""

    @classmethod
    def _build_pre_execution_packet(
        cls,
        task: TaskSpec,
        triggers: List[str],
        actions: List[ProposedAction],
        change_summary: str,
        evidence: List[str],
        risk_prefix: str = "Pre-execution boundary",
    ) -> BoundaryCheckResult:
        """
        Shared decision-packet construction for the pre-implementation
        boundary paths (evaluate_pre_execution and the non-delegated
        fallback of evaluate_with_delegated_authority), which otherwise
        differ only in change_summary/evidence text.
        """
        risks = [f"{risk_prefix} '{t}': {HUMAN_BOUNDARY_TRIGGERS.get(t, t)}" for t in triggers]
        packet = HumanDecisionPacket(
            task_id=task.task_id,
            objective=task.objective,
            change_summary=change_summary,
            boundary_triggers=triggers,
            evidence=evidence,
            risks=risks,
            review_findings_summary={"total": 0, "blocker": 0, "high": 0},
            verification_status="pre_execution",
            recommended_action="Review requested consequential action and authorize or reject execution.",
            proposed_actions=[a.to_dict() for a in actions],
            executor_id=actions[0].executor_id if actions and actions[0].executor_id else None,
        )
        return BoundaryCheckResult(requires_human_approval=True, triggered_boundaries=triggers, decision_packet=packet)

    @classmethod
    def evaluate_pre_execution(
        cls,
        task: TaskSpec,
        planned_actions: Optional[List[str]] = None,
        target_repo: Optional[Union[str, Path]] = None,
    ) -> BoundaryCheckResult:
        """
        Evaluates task objective, metadata, and planned actions BEFORE launching an implementation agent.
        Prevents unrestricted agents from executing consequential mutations prior to human authority.
        """
        repo_name = task.repository or (Path(target_repo).name if target_repo else "")
        actions = infer_proposed_actions(
            objective=task.objective,
            repo_name=repo_name,
            planned_actions=planned_actions,
            human_approval_requirements=task.human_approval_requirements,
        )

        triggers = [a.authority_boundary for a in actions if a.authority_boundary]
        if task.human_approval_requirements:
            for req in task.human_approval_requirements:
                if req in HUMAN_BOUNDARY_TRIGGERS and req not in triggers:
                    triggers.append(req)

        if task.risk_level == "critical" and "production_deployment" not in triggers:
            triggers.append("production_deployment")

        if not triggers:
            return BoundaryCheckResult(requires_human_approval=False)

        return cls._build_pre_execution_packet(
            task, triggers, actions,
            change_summary="Consequential action requested prior to implementation agent launch.",
            evidence=["Pre-execution authority evaluation"],
        )

    @classmethod
    def evaluate(
        cls,
        task: TaskSpec,
        planned_actions: List[str],
        change_summary: str = "",
        reconciliation: Optional[ReconciliationResult] = None,
        verification: Optional[VerificationPlan] = None,
        hygiene_policy: Optional[PolicyEvaluationResult] = None,
        non_independent_roles: Optional[List[str]] = None,
    ) -> BoundaryCheckResult:
        """
        Determines whether the post-verification task hits a human authority boundary.
        Distinguishes quality improvements (autonomously permitted) from debt acceptance / weakening.
        """
        actions = infer_proposed_actions(
            objective=task.objective,
            repo_name=task.repository,
            planned_actions=planned_actions,
            human_approval_requirements=task.human_approval_requirements,
        )
        triggers = [a.authority_boundary for a in actions if a.authority_boundary]

        if task.human_approval_requirements:
            for req in task.human_approval_requirements:
                if req in HUMAN_BOUNDARY_TRIGGERS and req not in triggers:
                    triggers.append(req)

        if hygiene_policy:
            if hygiene_policy.is_hard_rejected and "hygiene_policy_violation" not in triggers:
                triggers.append("hygiene_policy_violation")
            if hygiene_policy.verdict == PolicyChangeType.DEBT_ACCEPTANCE and "slop_debt_acceptance" not in triggers:
                triggers.append("slop_debt_acceptance")
            elif hygiene_policy.verdict == PolicyChangeType.WEAKENING and "hygiene_policy_weakening" not in triggers:
                triggers.append("hygiene_policy_weakening")
            elif hygiene_policy.verdict == PolicyChangeType.UNKNOWN and "security_policy_exception" not in triggers:
                triggers.append("security_policy_exception")
        else:
            actions_str = " ".join(planned_actions).lower()
            if "tombstone" in actions_str and "slop_debt_acceptance" not in triggers:
                triggers.append("slop_debt_acceptance")
            if "ceiling increase" in actions_str and "hygiene_policy_violation" not in triggers:
                triggers.append("hygiene_policy_violation")

        # 4. Inspect reconciliation findings for unresolved blockers or security human reviews
        if reconciliation and reconciliation.requires_human_judgment:
            if "security_policy_exception" not in triggers:
                triggers.append("security_policy_exception")

        # A change whose reviewer was its own author has not been independently
        # reviewed, however clean the verdict reads. On HOWLFRAM-BUG-50 failover
        # handed the implementer three of its own reviews and the run proceeded
        # as though independence held, so this now reaches the human either way.
        if non_independent_roles and "non_independent_review" not in triggers:
            triggers.append("non_independent_review")

        # 5. Critical risk tasks always trigger human authority
        if task.risk_level == "critical" and "production_deployment" not in triggers:
            triggers.append("production_deployment")

        if not triggers:
            return BoundaryCheckResult(requires_human_approval=False)

        # Build decision packet
        evidence_list: List[str] = []
        if verification:
            for step in verification.steps:
                evidence_list.append(f"Step '{step.name}': {step.status} (exit {step.exit_code})")

        if hygiene_policy and hygiene_policy.changes:
            for c in hygiene_policy.changes:
                evidence_list.append(f"Policy Change [{c.change_type.value.upper()}]: {c.description}")

        risks_list: List[str] = []
        for t in triggers:
            risks_list.append(f"Policy boundary '{t}': {HUMAN_BOUNDARY_TRIGGERS.get(t, t)}")

        findings_summary = {
            "total": len(reconciliation.findings) if reconciliation else 0,
            "blocker": reconciliation.unresolved_blockers if reconciliation else 0,
            "high": reconciliation.unresolved_highs if reconciliation else 0,
        }

        rec_action = "Review diff and evidence packet, then provide explicit approval or rejection."
        if "hygiene_policy_violation" in triggers:
            rec_action = "HARD REJECT: Prohibited ceiling inflation or policy violation must be remediated in code."

        summary_text = change_summary
        if not summary_text and hygiene_policy:
            summary_text = hygiene_policy.summary
        if not summary_text:
            summary_text = "Pending review & human confirmation."

        actions = infer_proposed_actions(
            objective=task.objective,
            repo_name=task.repository,
            planned_actions=planned_actions,
            human_approval_requirements=triggers,
        )
        executor_id = actions[0].executor_id if actions and actions[0].executor_id else None

        packet = HumanDecisionPacket(
            task_id=task.task_id,
            objective=task.objective,
            change_summary=summary_text,
            boundary_triggers=triggers,
            evidence=evidence_list,
            risks=risks_list,
            review_findings_summary=findings_summary,
            verification_status=verification.overall_status if verification else "unverified",
            recommended_action=rec_action,
            proposed_actions=[a.to_dict() for a in actions],
            executor_id=executor_id,
        )

        return BoundaryCheckResult(
            requires_human_approval=True,
            triggered_boundaries=triggers,
            decision_packet=packet,
        )

    @classmethod
    def evaluate_with_delegated_authority(
        cls,
        task: TaskSpec,
        planned_actions: List[str],
        envelope: Optional[Any] = None,
        target_repo: Optional[Union[str, Path]] = None,
        repo_slug: Optional[str] = None,
        files_changed: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> BoundaryCheckResult:
        """
        Delegated-authority pre-check (#59 Phase 11): ProposedAction ->
        identify authority class -> does the campaign's AuthorityEnvelope
        contain exact permission? If every triggered boundary resolves to
        DELEGATED_AUTHORITY_ALLOW, autonomous continuation is permitted
        without asking a human. Otherwise this falls through to today's
        evaluate_pre_execution()/evaluate() behavior unchanged -- this
        method never weakens or replaces existing human-boundary protection,
        it only adds a policy-driven allow path in front of it.

        The model never answers "am I allowed?" here: the only inputs that
        matter are the envelope (operator-created, tamper-verified) and the
        NEVER_DELEGATABLE_BOUNDARIES set (code-defined, not campaign data).
        """
        # Local imports to avoid a hard import-time dependency from
        # human_boundary.py (a foundational, widely-imported module) on the
        # newer authority modules, and to keep this delegated-authority path
        # entirely optional for callers that don't pass an envelope.
        from src.control_plane.authority_envelope import (
            AuthorityDecision,
            evaluate_action_against_envelope,
        )
        from src.control_plane.proposed_action import infer_proposed_actions_from_diff

        repo_name = task.repository or (Path(target_repo).name if target_repo else "")
        actions = infer_proposed_actions(
            objective=task.objective,
            repo_name=repo_name,
            planned_actions=planned_actions,
            human_approval_requirements=task.human_approval_requirements,
        )
        actions = actions + infer_proposed_actions_from_diff(files_changed or [], repo_name=repo_name)

        triggers = [a.authority_boundary for a in actions if a.authority_boundary]
        if task.risk_level == "critical" and "production_deployment" not in triggers:
            triggers.append("production_deployment")

        if not triggers:
            return BoundaryCheckResult(requires_human_approval=False)

        slug = repo_slug or repo_name
        all_delegated = True
        reasons: List[str] = []
        for trigger in triggers:
            if trigger in NEVER_DELEGATABLE_BOUNDARIES:
                all_delegated = False
                reasons.append(f"'{trigger}' is never delegatable regardless of envelope contents.")
                continue
            decision, reason = evaluate_action_against_envelope(envelope, trigger, slug)
            if decision != AuthorityDecision.DELEGATED_AUTHORITY_ALLOW:
                all_delegated = False
            reasons.append(reason)

        if all_delegated:
            return BoundaryCheckResult(requires_human_approval=False, triggered_boundaries=triggers)

        # Not fully delegated: build the human decision packet directly from
        # the triggers already computed above (which may include diff-based
        # self-modification triggers that evaluate_pre_execution()'s own
        # text-only inference would never see) rather than re-deriving
        # triggers from scratch and silently losing them.
        return cls._build_pre_execution_packet(
            task, triggers, actions,
            change_summary="Consequential action requested; delegated authority did not fully cover it.",
            evidence=["Delegated authority evaluation: " + "; ".join(reasons)],
            risk_prefix="Boundary",
        )


HUMAN_DECISION_SCHEMA_VERSION = "howlplane.human_decision/v1"


@dataclass
class RepositoryStateFingerprint(DataClassSerializationMixin):
    """Fingerprint of the repository state bound to an authorization decision."""

    commit_sha: str
    dirty: bool
    diff_sha256: str
    files_modified: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepositoryStateFingerprint":
        return cls(
            commit_sha=data.get("commit_sha", ""),
            dirty=data.get("dirty", False),
            diff_sha256=data.get("diff_sha256", ""),
            files_modified=data.get("files_modified", []),
        )


@dataclass
class HumanDecisionRecord(DataClassSerializationMixin):
    """Durable, structured human decision artifact scoped to a task and reviewed repository state."""

    task_id: str
    decision: str  # "approved" | "rejected"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    boundary_triggers: List[str] = field(default_factory=list)
    operator_source: str = "cli"
    reason: Optional[str] = None
    repository: str = ""
    repository_state: Optional[RepositoryStateFingerprint] = None
    decision_packet_sha256: Optional[str] = None
    changeops_decision_id: Optional[str] = None
    schema: str = HUMAN_DECISION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.repository_state:
            d["repository_state"] = self.repository_state.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HumanDecisionRecord":
        repo_state_data = data.get("repository_state")
        repo_state = (
            RepositoryStateFingerprint.from_dict(repo_state_data)
            if repo_state_data
            else None
        )
        return cls(
            task_id=data["task_id"],
            decision=data["decision"],
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            boundary_triggers=data.get("boundary_triggers", []),
            operator_source=data.get("operator_source", "cli"),
            reason=data.get("reason"),
            repository=data.get("repository", ""),
            repository_state=repo_state,
            decision_packet_sha256=data.get("decision_packet_sha256"),
            changeops_decision_id=data.get("changeops_decision_id"),
            schema=data.get("schema", HUMAN_DECISION_SCHEMA_VERSION),
        )


def compute_repository_fingerprint(
    repo_dir: Union[str, Path],
    task_run_dir: Optional[Union[str, Path]] = None,
) -> RepositoryStateFingerprint:
    """
    Computes a cryptographic fingerprint of the repository state bound to this task.
    Uses git HEAD commit SHA, status porcelain, and attributable diff content.
    """
    root = Path(repo_dir).resolve()
    res_sha = run_git_in_repo(root, ["rev-parse", "HEAD"])
    commit_sha = res_sha.stdout.strip() if res_sha.returncode == 0 else "HEAD_UNKNOWN"

    res_status = run_git_in_repo(root, ["status", "--porcelain"])
    status_out = res_status.stdout if res_status.returncode == 0 else ""
    dirty = bool(status_out.strip())

    files: List[str] = []
    for line in status_out.splitlines():
        if line.strip():
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[1].strip()
            if (
                path_part.startswith(".task_runs")
                or path_part.startswith(".howlchangeops")
                or path_part.startswith("logs/control_plane")
                or path_part.endswith(".jsonl")
            ):
                continue
            files.append(path_part)

    diff_text = ""
    if task_run_dir and (Path(task_run_dir) / "baseline.json").is_file():
        try:
            from src.control_plane.git_baseline import capture_delta, GitBaseline
            baseline_data = json.loads((Path(task_run_dir) / "baseline.json").read_text(encoding="utf-8"))
            baseline = GitBaseline.from_dict(baseline_data)
            delta = capture_delta(root, baseline)
            diff_text = delta.diff_content
        except Exception:
            pass

    if not diff_text:
        res_diff = run_git_in_repo(root, ["diff", "HEAD"])
        diff_text = res_diff.stdout if res_diff.returncode == 0 else ""
        for f_name in sorted(files):
            f_path = root / f_name
            if f_path.is_file():
                try:
                    diff_text += f"\n--- {f_name} ---\n" + f_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    diff_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    return RepositoryStateFingerprint(
        commit_sha=commit_sha,
        dirty=dirty,
        diff_sha256=diff_sha256,
        files_modified=sorted(files),
    )


def check_repository_drift(
    approved_fingerprint: RepositoryStateFingerprint,
    current_fingerprint: RepositoryStateFingerprint,
) -> Tuple[bool, Optional[str]]:
    """
    Compares the approved repository state fingerprint with current repository state.
    Returns (has_drift, drift_reason).
    """
    if approved_fingerprint.commit_sha != current_fingerprint.commit_sha:
        return True, f"HEAD commit changed (approved {approved_fingerprint.commit_sha[:8]}, current {current_fingerprint.commit_sha[:8]})"

    if approved_fingerprint.diff_sha256 != current_fingerprint.diff_sha256:
        return True, "Attributable diff content modified since authorization"

    if approved_fingerprint.files_modified != current_fingerprint.files_modified:
        return True, f"Modified files changed (approved {approved_fingerprint.files_modified}, current {current_fingerprint.files_modified})"

    return False, None


class HumanLifecycleManager:
    """
    Orchestrates human decision capture, state validation, drift verification,
    idempotency enforcement, bounded execution handoff, and task resumption.
    """

    @staticmethod
    def get_run_dir(target_repo: Union[str, Path], task_id: str) -> Path:
        return Path(target_repo).resolve() / ".task_runs" / task_id

    @staticmethod
    def load_task_spec(run_dir: Path) -> TaskSpec:
        task_file = run_dir / "task.yaml"
        if not task_file.is_file():
            raise TaskRunNotFoundError(f"Task spec not found at {task_file}")
        return TaskSpec.load_from_file(str(task_file))

    @staticmethod
    def load_decision(run_dir: Path) -> Optional[HumanDecisionRecord]:
        dec_file = run_dir / "human_decision.json"
        if not dec_file.is_file():
            return None
        return HumanDecisionRecord.load_from_file(str(dec_file))

    @classmethod
    def _prepare_decision(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
        decision: str,
        reason: Optional[str] = None,
    ) -> Tuple[Path, TaskSpec, RepositoryStateFingerprint, Optional[str], List[str], Optional[str]]:
        run_dir = cls.get_run_dir(target_repo, task_id)
        if not run_dir.is_dir() or not (run_dir / "task.yaml").is_file():
            raise TaskRunNotFoundError(f"Task run '{task_id}' not found in {target_repo}/.task_runs")

        task_spec = cls.load_task_spec(run_dir)

        existing = cls.load_decision(run_dir)
        if existing:
            if existing.decision == decision:
                if not reason or reason == existing.reason:
                    return run_dir, task_spec, None, None, None, None
            else:
                opp = "APPROVED" if decision == "rejected" else "REJECTED"
                raise ContradictoryDecisionError(
                    f"Task '{task_id}' was previously {opp}. Contradictory {decision} rejected (fail-closed)."
                )

        if decision == "approved" and task_spec.current_state != "awaiting_human":
            if task_spec.current_state == "complete":
                raise InvalidTaskStateError(f"Task '{task_id}' is already COMPLETE.")
            if task_spec.current_state == "failed":
                raise InvalidTaskStateError(f"Task '{task_id}' is in FAILED state. Cannot approve a failed task.")
            raise InvalidTaskStateError(
                f"Task '{task_id}' is in state '{task_spec.current_state}'. Only tasks in AWAITING_HUMAN state can be approved."
            )

        current_fp = compute_repository_fingerprint(target_repo, run_dir)
        dp_file = run_dir / "decision_packet.md"
        dp_sha = (
            hashlib.sha256(dp_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            if dp_file.is_file()
            else None
        )

        triggers = list(task_spec.human_approval_requirements or [])
        changeops_dec_id = None
        if dp_file.is_file():
            dp_text = dp_file.read_text(encoding="utf-8")
            for line in dp_text.splitlines():
                if line.startswith("- ⚠️ **") and "**:" in line:
                    t_name = line.split("**")[1].strip()
                    if t_name and t_name not in triggers:
                        triggers.append(t_name)
                elif "ChangeOps Decision ID:" in line:
                    dec_val = line.split("`")[1].strip() if "`" in line else ""
                    if dec_val and dec_val != "None":
                        changeops_dec_id = dec_val

        return run_dir, task_spec, current_fp, dp_sha, triggers, changeops_dec_id

    @classmethod
    def approve(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
        reason: Optional[str] = None,
        operator_source: str = "cli",
        ledger: Optional[EvidenceLedger] = None,
    ) -> HumanDecisionRecord:
        """
        Records an explicit human approval for a task awaiting authorization.
        Enforces idempotency, fail-closed contradictory checks, repository state binding,
        and links authorization to HowlChangeOps when applicable.
        """
        run_dir, task_spec, current_fp, dp_sha, triggers, changeops_dec_id = cls._prepare_decision(
            target_repo, task_id, "approved", reason=reason
        )
        if current_fp is None:
            return cls.load_decision(run_dir)

        patch_file = run_dir / "diff.patch"
        if patch_file.is_file():
            reviewed_diff = patch_file.read_text(encoding="utf-8")
            reviewed_sha = hashlib.sha256(reviewed_diff.encode("utf-8")).hexdigest()
            if current_fp.diff_sha256 != reviewed_sha:
                raise RepositoryDriftError(
                    f"Repository code has changed since task '{task_id}' was reviewed. Approval cannot authorize unreviewed changes."
                )

        record = HumanDecisionRecord(
            task_id=task_id,
            decision="approved",
            timestamp=datetime.now(timezone.utc).isoformat(),
            boundary_triggers=triggers,
            operator_source=operator_source,
            reason=reason,
            repository=Path(target_repo).name,
            repository_state=current_fp,
            decision_packet_sha256=dp_sha,
            changeops_decision_id=changeops_dec_id,
        )
        record.save_to_file(run_dir / "human_decision.json")

        # Link to HowlChangeOps approval if available
        if changeops_dec_id:
            try:
                from src.control_plane.executor import HowlChangeOpsExecutor
                executor = HowlChangeOpsExecutor()
                if executor.is_available():
                    executor.approve(changeops_dec_id, target_repo, run_dir)
            except Exception:
                pass

        if ledger:
            ledger.append_entry(
                EvidenceEntry(
                    task_id=task_id,
                    agent_id="human_operator",
                    action="human_approval",
                    result="approved",
                    human_decision="approved",
                    artifact=str(run_dir / "human_decision.json"),
                    metadata={
                        "reason": reason,
                        "operator_source": operator_source,
                        "boundaries": triggers,
                        "commit_sha": current_fp.commit_sha,
                        "changeops_decision_id": changeops_dec_id,
                    },
                )
            )

        return record

    @classmethod
    def reject(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
        reason: Optional[str] = None,
        operator_source: str = "cli",
        ledger: Optional[EvidenceLedger] = None,
    ) -> HumanDecisionRecord:
        """
        Records an explicit human rejection for a task.
        Enforces fail-closed behavior, transitions task to FAILED, and records evidence.
        """
        run_dir, task_spec, current_fp, dp_sha, triggers, changeops_dec_id = cls._prepare_decision(
            target_repo, task_id, "rejected", reason=reason
        )
        if current_fp is None:
            return cls.load_decision(run_dir)

        record = HumanDecisionRecord(
            task_id=task_id,
            decision="rejected",
            timestamp=datetime.now(timezone.utc).isoformat(),
            boundary_triggers=triggers,
            operator_source=operator_source,
            reason=reason,
            repository=Path(target_repo).name,
            repository_state=current_fp,
            decision_packet_sha256=dp_sha,
            changeops_decision_id=changeops_dec_id,
        )
        record.save_to_file(run_dir / "human_decision.json")

        rej_reason = f"Human operator rejected authorization: {reason or 'No reason provided'}"
        try:
            from src.control_plane.checkpoints import CheckpointManager
            active_stage = task_spec.current_state if task_spec.current_state not in ("failed", "cancelled") else None
            CheckpointManager.fail_stage(
                run_dir,
                stage=active_stage,
                reason=rej_reason,
                result_summary={"error": rej_reason, "decision": "rejected"},
            )
        except Exception:
            pass

        task_spec.transition_to(
            "failed",
            rej_reason,
        )
        task_spec.save_to_file(str(run_dir / "task.yaml"))

        if ledger:
            ledger.append_entry(
                EvidenceEntry(
                    task_id=task_id,
                    agent_id="human_operator",
                    action="human_rejection",
                    result="rejected",
                    human_decision="rejected",
                    artifact=str(run_dir / "human_decision.json"),
                    metadata={"reason": reason, "operator_source": operator_source},
                )
            )

        return record

    @classmethod
    def resume(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
        orchestrator: Optional[Any] = None,
        ledger: Optional[EvidenceLedger] = None,
        control_plane_root: Optional[Union[str, Path]] = None,
    ) -> Any:
        """
        Resumes task execution from durable task-run artifacts.
        Validates approval status, repository drift, executes bounded consequential actions,
        verifies execution receipts, and transitions to COMPLETE only upon verified proof.
        """
        from src.control_plane.orchestrator import OrchestrationResult
        from src.control_plane.executor import (
            ExecutionReceipt,
            ExecutorRegistry,
            ExecutionFailedError,
            InvalidReceiptError,
            UnsupportedActionError,
        )
        from src.control_plane.locking import TaskLock
        from src.control_plane.proposed_action import infer_proposed_actions, ProposedAction

        run_dir = cls.get_run_dir(target_repo, task_id)
        if not run_dir.is_dir() or not (run_dir / "task.yaml").is_file():
            raise TaskRunNotFoundError(f"Task run '{task_id}' not found in {target_repo}/.task_runs")

        # Recovery is governed execution, so it leaves evidence like any other
        # stage. SLOPFIX-06's two failed resumes mutated progress and lock state
        # and recorded nothing, leaving no durable account of why the documented
        # recovery path did not work.
        def audit(action, result=None, **metadata):
            if not ledger:
                return
            try:
                ledger.append_entry(
                    EvidenceEntry(
                        task_id=task_id,
                        agent_id="control_plane",
                        action=action,
                        command=f"ai resume {task_id}",
                        result=result,
                        repository=str(target_repo),
                        metadata=metadata,
                    )
                )
            except Exception:
                pass

        lock_state = cls._describe_lock_state(target_repo, task_id)
        audit("resume_requested", "REQUESTED", lock_state=lock_state)
        audit("resume_lock_state", lock_state.get("blocking_state"), **lock_state)

        task_lock = TaskLock(
            target_repo, task_id, operation="resume", command=f"ai resume {task_id}"
        )
        try:
            task_lock.acquire()
        except Exception as err:
            audit(
                "resume_failed",
                "LOCK_UNAVAILABLE",
                error=str(err),
                lock_state=lock_state,
            )
            raise

        try:
            task_spec = cls.load_task_spec(run_dir)
            audit(
                "resume_started",
                "STARTED",
                previous_state=task_spec.current_state,
                lock_state=lock_state,
            )

            if task_spec.current_state == "complete":
                return OrchestrationResult(
                    task_id=task_id,
                    task_spec=task_spec,
                    final_state="complete",
                    exit_code=0,
                    run_dir=str(run_dir),
                )

            if task_spec.current_state == "cancelled":
                raise InvalidTaskStateError(f"Task '{task_id}' was CANCELLED and cannot be resumed.")

            if task_spec.current_state == "failed":
                dec = cls.load_decision(run_dir)
                if dec and dec.decision == "rejected":
                    raise InvalidTaskStateError(
                        f"Task '{task_id}' was rejected by human operator and cannot be resumed."
                    )
                raise InvalidTaskStateError(
                    f"Task '{task_id}' is in FAILED state. Cannot resume a failed task without re-running."
                )

            if task_spec.current_state == "awaiting_human":
                decision = cls.load_decision(run_dir)
                if not decision:
                    raise ApprovalRequiredError(
                        f"Task '{task_id}' is AWAITING_HUMAN and requires explicit human approval before resuming. Run 'ai approve {task_id}'."
                    )
                if decision.decision == "rejected":
                    raise InvalidTaskStateError(f"Task '{task_id}' authorization was REJECTED by human operator.")

                if decision.decision == "approved":
                    # Validate repository state binding (drift check)
                    current_fp = compute_repository_fingerprint(target_repo, run_dir)
                    if decision.repository_state:
                        has_drift, drift_reason = check_repository_drift(decision.repository_state, current_fp)
                        if has_drift:
                            if ledger:
                                ledger.append_entry(
                                    EvidenceEntry(
                                        task_id=task_id,
                                        agent_id="control_plane",
                                        action="stale_approval_detected",
                                        result=f"Repository drift detected: {drift_reason}",
                                        metadata={"drift_reason": drift_reason},
                                    )
                                )
                            raise StaleApprovalError(
                                f"Repository state has drifted since approval was granted ({drift_reason}). "
                                f"Approval is STALE. Re-review and re-approval required."
                            )

                    # Check if this task requires bounded consequential execution
                    exec_boundaries = [b for b in decision.boundary_triggers if b in EXECUTABLE_BOUNDARIES]
                    if not exec_boundaries and task_spec.human_approval_requirements:
                        exec_boundaries = [b for b in task_spec.human_approval_requirements if b in EXECUTABLE_BOUNDARIES]

                    receipt_file = run_dir / "execution_receipt.json"

                    if exec_boundaries:
                        # Bounded consequential execution is required! Approval alone != Complete
                        if receipt_file.is_file():
                            try:
                                receipt = ExecutionReceipt.load_from_file(receipt_file)
                            except Exception as e:
                                raise InvalidReceiptError(f"Execution receipt at {receipt_file} is malformed: {e}")

                            executor = (
                                ExecutorRegistry.get_executor(receipt.executor)
                                or ExecutorRegistry.get_executor_for_action(receipt.action_type)
                            )
                            if not executor:
                                raise InvalidReceiptError(f"Unknown executor '{receipt.executor}' in execution receipt")

                            is_valid, val_reason = executor.verify_receipt(
                                receipt,
                                expected_action=receipt.action_type,
                                expected_repo=Path(target_repo).name,
                                expected_commit=current_fp.commit_sha,
                                run_dir=run_dir,
                            )
                            if not is_valid:
                                raise InvalidReceiptError(f"Execution receipt verification failed: {val_reason}")
                        else:
                            # Infer the consequential actions to execute
                            proposed_actions = infer_proposed_actions(
                                objective=task_spec.objective,
                                repo_name=Path(target_repo).name,
                                human_approval_requirements=decision.boundary_triggers,
                            )
                            if not proposed_actions:
                                primary_b = exec_boundaries[0]
                                proposed_actions = [
                                    ProposedAction(
                                        action_type=primary_b,
                                        target_repo=Path(target_repo).name,
                                        authority_boundary=primary_b,
                                        risk_level="critical" if primary_b in ("infrastructure_apply", "destructive_database_change") else "high",
                                    )
                                ]

                            for action in proposed_actions:
                                executor = ExecutorRegistry.get_executor_for_action(action.action_type)
                                if action.executor_id and not executor:
                                    executor = ExecutorRegistry.get_executor(action.executor_id)

                                if not executor or not executor.is_available() or not executor.supports_action(action.action_type):
                                    raise UnsupportedActionError(
                                        f"AUTHORIZED ACTION CANNOT EXECUTE: No configured bounded executor supports '{action.action_type}'. "
                                        f"Task cannot complete without trusted bounded execution."
                                    )

                                # Resolve or evaluate decision_id
                                decision_id = action.decision_id or decision.changeops_decision_id
                                if not decision_id:
                                    verdict, dec_id, eval_reason = executor.evaluate(action, target_repo, run_dir)
                                    if verdict == "DENY":
                                        raise ExecutionFailedError(f"Bounded executor policy denied execution: {eval_reason}")
                                    decision_id = dec_id

                                if not decision_id:
                                    raise ExecutionFailedError(
                                        f"Failed to obtain decision ID from bounded executor for '{action.action_type}'"
                                    )

                                # Check if already executed natively before calling execute (Prevent Replay Hazard!)
                                q_status, q_receipt, q_msg = executor.query_execution_status(
                                    decision_id=decision_id,
                                    repo_path=target_repo,
                                    task_run_dir=run_dir,
                                    action=action,
                                    task_id=task_id,
                                )

                                if q_status == "already_executed" and q_receipt:
                                    # Native receipt successfully recovered! Verify and advance
                                    is_valid, val_reason = executor.verify_receipt(
                                        q_receipt,
                                        expected_action=action.action_type,
                                        expected_repo=Path(target_repo).name,
                                        expected_commit=current_fp.commit_sha,
                                        run_dir=run_dir,
                                    )
                                    if not is_valid:
                                        raise InvalidReceiptError(f"Recovered native execution receipt verification failed: {val_reason}")
                                    continue
                                elif q_status == "ambiguous":
                                    raise ExecutionFailedError(
                                        f"Ambiguous execution state for decision '{decision_id}': {q_msg}. Human intervention required."
                                    )
                                elif q_status == "failed":
                                    raise ExecutionFailedError(
                                        f"Prior native execution for decision '{decision_id}' failed: {q_msg}."
                                    )

                                # Submit approval linkage
                                app_ok, app_msg = executor.approve(decision_id, target_repo, run_dir)
                                if not app_ok and "already approved" not in (app_msg or "").lower():
                                    if "key" in (app_msg or "").lower() or "not set" in (app_msg or "").lower():
                                        raise ExecutionFailedError(f"Bounded execution authorization failed: {app_msg}")

                                # Execute bounded action
                                exec_res = executor.execute(decision_id, target_repo, run_dir, action, task_id)
                                if exec_res.status == "stale":
                                    if ledger:
                                        ledger.append_entry(
                                            EvidenceEntry(
                                                task_id=task_id,
                                                agent_id=executor.name,
                                                action="stale_evidence_detected",
                                                result=exec_res.error_message,
                                                metadata={"decision_id": decision_id},
                                            )
                                        )
                                    raise StaleApprovalError(f"Bounded executor detected stale evidence / TOCTOU drift: {exec_res.error_message}")

                                if exec_res.status != "success" or not exec_res.receipt:
                                    raise ExecutionFailedError(f"Bounded execution failed: {exec_res.error_message}")

                                # Verify receipt
                                is_valid, val_reason = executor.verify_receipt(
                                    exec_res.receipt,
                                    expected_action=action.action_type,
                                    expected_repo=Path(target_repo).name,
                                    expected_commit=current_fp.commit_sha,
                                    run_dir=run_dir,
                                )
                                if not is_valid:
                                    raise InvalidReceiptError(f"Execution receipt verification failed: {val_reason}")

                                if ledger:
                                    ledger.append_entry(
                                        EvidenceEntry(
                                            task_id=task_id,
                                            agent_id=executor.name,
                                            action="bounded_execution_completed",
                                            result="success",
                                            artifact=exec_res.receipt_path,
                                            metadata={
                                                "executor": executor.name,
                                                "decision_id": decision_id,
                                                "action_type": action.action_type,
                                                "verification_status": exec_res.verification_status,
                                            },
                                        )
                                    )

                    # Valid approval (+ valid bounded execution receipt if required) -> complete
                    if ledger:
                        ledger.append_entry(
                            EvidenceEntry(
                                task_id=task_id,
                                agent_id="control_plane",
                                action="task_resumed",
                                metadata={"task_id": task_id, "resumed_from": "awaiting_human"},
                            )
                        )

                    task_spec.transition_to(
                        "complete",
                        f"Human authority approved ({decision.reason or 'No reason specified'}): all gates and bounded execution satisfied.",
                    )
                    task_spec.save_to_file(str(run_dir / "task.yaml"))

                    if ledger:
                        ledger.append_entry(
                            EvidenceEntry(
                                task_id=task_id,
                                agent_id="control_plane",
                                action="task_completed",
                                task_class=task_spec.task_class,
                                risk_level=task_spec.risk_level,
                                reasoning_tier=task_spec.recommended_reasoning_tier,
                                metadata={"human_approved": True, "resumed": True},
                            )
                        )

                    summary_file = run_dir / "summary.md"
                    if not summary_file.is_file():
                        summary_file.write_text(
                            f"# Governed Task Run Summary: `{task_id}`\n\n"
                            f"- **Objective:** {task_spec.objective}\n"
                            f"- **Final State:** `COMPLETE` (Human Approved & Verified)\n"
                            f"- **Approved At:** {decision.timestamp}\n",
                            encoding="utf-8",
                        )

                    return OrchestrationResult(
                        task_id=task_id,
                        task_spec=task_spec,
                        final_state="complete",
                        exit_code=0,
                        run_dir=str(run_dir),
                    )

            # For tasks interrupted in intermediate states (discovered, planned,
            # implementing, reviewing, verifying, interrupted). The orchestrator
            # needs the same task lock this method already holds, so it is handed
            # the ownership token rather than being made to acquire it again --
            # which is what deadlocked every documented recovery
            # (HOWLFRAM-SLOPFIX-06).
            if orchestrator:
                outcome = orchestrator.run(
                    task_spec, lock_ownership=task_lock.ownership
                )
            else:
                from src.control_plane.orchestrator import GovernedTaskOrchestrator
                orch = GovernedTaskOrchestrator(
                    target_repo=target_repo,
                    control_plane_root=control_plane_root,
                )
                outcome = orch.run(task_spec, lock_ownership=task_lock.ownership)
            audit(
                "resume_completed",
                getattr(outcome, "final_state", None),
                exit_code=getattr(outcome, "exit_code", None),
                checkpoint=cls._latest_checkpoint_stage(run_dir),
            )
            return outcome
        except Exception as err:
            audit(
                "resume_failed",
                type(err).__name__,
                error=str(err),
                checkpoint=cls._latest_checkpoint_stage(run_dir),
            )
            raise
        finally:
            task_lock.release()

    @staticmethod
    def _latest_checkpoint_stage(run_dir: Path) -> Optional[str]:
        """Names the stage a recovery attempt actually left behind, if any."""
        try:
            from src.control_plane.checkpoints import CheckpointManager

            checkpoint = CheckpointManager.load_latest_checkpoint(run_dir)
            if checkpoint is None:
                return None
            return f"{checkpoint.stage}:{checkpoint.status}"
        except Exception:
            return None

    @staticmethod
    def _describe_lock_state(target_repo, task_id: str) -> Dict[str, Any]:
        """Classifies every lock relevant to a task before recovery touches it.

        Recorded up front so a failed resume explains itself: which lock stood
        in the way, whose it was, and whether it was reclaimable.
        """
        from src.control_plane.atomic_io import safe_load_json
        from src.control_plane.locking import (
            classify_lock_owner,
            get_repo_lock_path,
            get_task_lock_path,
        )

        locks: Dict[str, Any] = {}
        blocking = "NONE"
        for scope, path in (
            ("task_run", get_task_lock_path(target_repo, task_id)),
            ("repository", get_repo_lock_path(target_repo)),
        ):
            if not path.exists():
                locks[scope] = {"held": False}
                continue
            try:
                owner = safe_load_json(path)
            except Exception as err:
                locks[scope] = {"held": True, "unreadable": str(err)}
                blocking = "UNREADABLE"
                continue
            state, reason = classify_lock_owner(
                owner.get("pid", -1),
                owner.get("hostname", ""),
                owner.get("process_create_time", 0.0),
            )
            locks[scope] = {
                "held": True,
                "path": str(path),
                "owner_state": state.value,
                "reason": reason,
                "pid": owner.get("pid"),
                "hostname": owner.get("hostname"),
                "task_id": owner.get("task_id"),
                "operation": owner.get("operation"),
            }
            if state.value != "STALE" or blocking == "NONE":
                blocking = state.value
        return {"locks": locks, "blocking_state": blocking}

    @classmethod
    def cancel(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
        reason: Optional[str] = None,
        ledger: Optional[EvidenceLedger] = None,
    ) -> Any:
        """
        Cancels an active or interrupted task run safely.
        Terminates child processes, releases locks, and transitions state to 'cancelled'.
        Never reverts or destroys repository working tree changes.
        """
        from src.control_plane.checkpoints import CheckpointManager
        from src.control_plane.locking import TaskLock
        from src.control_plane.orchestrator import OrchestrationResult
        from src.control_plane.process_manager import ProcessTracker

        run_dir = cls.get_run_dir(target_repo, task_id)
        if not run_dir.is_dir() or not (run_dir / "task.yaml").is_file():
            raise TaskRunNotFoundError(f"Task run '{task_id}' not found in {target_repo}/.task_runs")

        with TaskLock(target_repo, task_id, operation="cancel", command=f"ai cancel {task_id}"):
            task_spec = cls.load_task_spec(run_dir)

            if task_spec.current_state == "complete":
                raise InvalidTaskStateError(f"Task '{task_id}' is already COMPLETE and cannot be cancelled.")

            if task_spec.current_state == "cancelled":
                return OrchestrationResult(
                    task_id=task_id,
                    task_spec=task_spec,
                    final_state="cancelled",
                    exit_code=0,
                    run_dir=str(run_dir),
                )

            # Terminate active process if running
            terminated, term_msg = ProcessTracker.terminate_task_process(run_dir)

            cancel_reason = reason or "Cancelled by human operator via CLI"
            task_spec.transition_to("cancelled", cancel_reason)
            task_spec.save_to_file(str(run_dir / "task.yaml"))

            CheckpointManager.record_interrupted(
                run_dir,
                stage="cancelled",
                reason=cancel_reason,
                metadata={"process_terminated": terminated, "process_message": term_msg},
            )

            if ledger:
                ledger.append_entry(
                    EvidenceEntry(
                        task_id=task_id,
                        agent_id="control_plane",
                        action="task_cancelled",
                        result="cancelled",
                        metadata={"reason": cancel_reason, "process_terminated": terminated},
                    )
                )

            return OrchestrationResult(
                task_id=task_id,
                task_spec=task_spec,
                final_state="cancelled",
                exit_code=0,
                run_dir=str(run_dir),
            )
