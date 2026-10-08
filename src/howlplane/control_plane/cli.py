#!/usr/bin/env python3
"""
cli.py

Deterministic canonical command-line interface for the multi-agent engineering control plane.
Consolidates all control plane subcommands and configuration resolution (HOWL-CANON-006).
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

_repo_root = str(Path(__file__).resolve().parents[3])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
_src = str(Path(_repo_root) / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from howlplane.control_plane import __version__, agent_readiness
from howlplane.control_plane.agent_registry import AgentRegistry
from howlplane.control_plane.atomic_io import safe_load_json
from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger, EvidenceSchemaError
from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
from howlplane.control_plane.git_env import run_git_in_repo
from howlplane.control_plane.locking import get_repo_lock_path, get_task_lock_path, is_process_alive
from howlplane.control_plane.recovery import CrashRecoveryEngine
from howlplane.control_plane.howlframe_runner import (
    HowlFrameAuditRunner,
    find_howlframe_binary,
    get_howlframe_version,
    get_dogfood_mode,
    DEFAULT_INSTRUCTION_BUDGET,
)
from howlplane.control_plane.human_boundary import (
    HumanBoundaryGate,
    HumanLifecycleManager,
    HumanDecisionRecord,
    compute_repository_fingerprint,
    check_repository_drift,
)
from howlplane.control_plane.metrics import MetricsCalculator
from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig, OrchestrationResult
from howlplane.control_plane.project_adapter import ProjectAdapter, ProjectContext
from howlplane.control_plane.progress import format_elapsed, format_last_heartbeat
from howlplane.control_plane.reconciliation import ReconciliationResult, ReviewFinding, ReviewReconciler
from howlplane.control_plane.resource_cli import inventory_document, render_inventory, render_route
from howlplane.control_plane.reviewers import list_reviewer_roles, get_reviewer_role
from howlplane.control_plane.router import TaskRouter, RoutingDecision
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.config_loader import SemanticRecommendationSettings
from howlplane.control_plane.task_spec import TaskSpec
from howlplane.control_plane.factory.factory_cli import (  # noqa: F401  re-exported for tests and monkeypatching
    _worker_display,
    _owner_decisions,
    collect_factory_status,
    cmd_factory_status,
    cmd_factory_stop,
    cmd_factory_resume,
    cmd_factory_start,
    _print_factory_result,
    cmd_factory_logs,
    _print_worker_table,
    cmd_factory_doctor,
    cmd_factory_bootstrap,
    cmd_factory,
)
from howlplane.control_plane.verification import VerificationPlan
from howlplane.control_plane.governance_cli import (  # noqa: F401  re-exported for tests and monkeypatching
    _handle_work_item_decision,
    _handle_proposal_decision,
    _handle_decision,
    cmd_approve,
    cmd_reject,
    cmd_resume,
    cmd_cancel,
    _lock_candidates,
    _lock_relevance,
    cmd_unlock,
)
from howlplane.control_plane.factory.factory_run_cli import (  # noqa: F401  re-exported for tests and monkeypatching
    _resolve_factory_campaign,
    _select_factory_authority,
    _build_factory_supervisor,
    cmd_factory_run_once,
    cmd_factory_run,
    _factory_state_store,
    _factory_workspace,
    _factory_readiness,
    _factory_preflight,
)
from howlplane.control_plane.status_cli import cmd_status, cmd_doctor, cmd_system_doctor, cmd_agents  # noqa: F401


class ControlPlaneError(Exception):
    """Base exception for control plane launcher errors."""
    pass


class TargetRepositoryNotFoundError(ControlPlaneError):
    """Raised when no valid Git target repository is discovered."""
    pass


class ControlPlaneNotFoundError(ControlPlaneError):
    """Raised when HowlPlane control plane cannot be located."""
    pass


def find_git_repo_root(start_dir: Optional[Union[str, Path]] = None) -> Path:
    """Discovers the root directory of the current Git repository using `git rev-parse`."""
    target = Path(start_dir or os.getcwd()).resolve()
    try:
        res = run_git_in_repo(target, ["rev-parse", "--show-toplevel"])
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except Exception:
        pass

    curr = target
    while curr != curr.parent:
        if (curr / ".git").exists():
            return curr
        curr = curr.parent

    raise TargetRepositoryNotFoundError(
        f"ERROR: no target Git repository found in '{target}'. HowlPlane must be run inside a Git repository."
    )


def find_control_plane_root(override_path: Optional[str] = None) -> Path:
    """Discovers the HowlPlane repository path using the 5-step precedence."""
    if override_path:
        p = Path(override_path).expanduser().resolve()
        if (p / "src" / "howlplane" / "control_plane").is_dir() or (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
            return p
        raise ControlPlaneNotFoundError(
            f"ERROR: specified HowlPlane control plane path does not exist: {override_path}"
        )

    # 1. Primary canonical environment variable: HOWLPLANE_HOME / HOWLPLANE_DIR
    env_path = os.environ.get("HOWLPLANE_HOME") or os.environ.get("HOWLPLANE_DIR")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if (p / "src" / "howlplane" / "control_plane").is_dir() or (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
            return p
        raise ControlPlaneNotFoundError(
            f"ERROR: HOWLPLANE_HOME environment variable points to invalid path: {env_path}"
        )

    # 2. Deprecated legacy environment variable: AI_KNOWLEDGE_LIBRARY
    legacy_env = os.environ.get("AI_KNOWLEDGE_LIBRARY")
    if legacy_env:
        p = Path(legacy_env).expanduser().resolve()
        if (p / "src" / "howlplane" / "control_plane").is_dir() or (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
            print(
                "WARNING: AI_KNOWLEDGE_LIBRARY environment variable is deprecated; please use HOWLPLANE_HOME instead.",
                file=sys.stderr,
            )
            return p
        raise ControlPlaneNotFoundError(
            f"ERROR: legacy AI_KNOWLEDGE_LIBRARY environment variable points to invalid path: {legacy_env}"
        )

    # 3. Config file candidates (canonical ~/.config/howlplane/ followed by legacy fallbacks)
    candidates = [
        Path.home() / ".config" / "howlplane" / "config.toml",
        Path.home() / ".config" / "ai-control-plane" / "config.toml",
        Path.home() / ".config" / "ai" / "config.toml",
    ]
    for cfg in candidates:
        if cfg.is_file():
            try:
                txt = cfg.read_text(encoding="utf-8")
                match = re.search(r'path\s*=\s*["\']([^"\']+)["\']', txt)
                if match:
                    p = Path(match.group(1)).expanduser().resolve()
                    if (p / "src" / "howlplane" / "control_plane").is_dir() or (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
                        return p
                    raise ControlPlaneNotFoundError(f"ERROR: configured path in '{cfg}' is invalid: {match.group(1)}")
            except ControlPlaneNotFoundError:
                raise
            except Exception:
                pass

    # 4. Self repository root detection
    self_root = Path(__file__).resolve().parents[3]
    if ((self_root / "src" / "howlplane" / "control_plane").is_dir() or (self_root / "src" / "control_plane").is_dir()) and (self_root / "AGENTS.md").is_file():
        return self_root

    raise ControlPlaneNotFoundError(
        "ERROR: configured HowlPlane control plane not found.\n"
        "Please set the HOWLPLANE_HOME environment variable or configure ~/.config/howlplane/config.toml:\n\n"
        "  [control_plane]\n"
        "  path = \"/path/to/howlplane\"\n"
    )


def _resolve_repo(args: argparse.Namespace) -> Path:
    target = getattr(args, "repo", None) or getattr(args, "repo_dir", None) or getattr(args, "project_dir", None)
    return find_git_repo_root(target)


def _resolve_ledger_file(args: argparse.Namespace, cp_root: Optional[Path] = None) -> Optional[str]:
    explicit = getattr(args, "ledger_file", None)
    if explicit:
        return explicit
    if cp_root is None:
        try:
            cp_root = find_control_plane_root(getattr(args, "control_plane_dir", None))
        except Exception:
            return None
    ledger_path = cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl"
    return str(ledger_path)


def infer_task_metadata(
    objective: str,
    repo_name: str,
    explicit_id: Optional[str] = None,
    explicit_risk: Optional[str] = None,
    explicit_tier: Optional[str] = None,
    explicit_class: Optional[str] = None,
) -> Tuple[str, str, str, str]:
    """Infers task ID, task class, risk level, and reasoning tier from objective text."""
    clean_obj = objective.strip()
    lowered = clean_obj.lower()

    if explicit_id:
        task_id = explicit_id
    else:
        issue_match = re.search(r'(?:issue|bug|item|task)\s*#?\s*([A-Za-z0-9_-]+)', clean_obj, re.IGNORECASE)
        if issue_match:
            issue_val = issue_match.group(1).upper()
            if issue_val.startswith("TASK-") or issue_val.startswith("IMP-") or issue_val.startswith("ISSUE-"):
                task_id = issue_val
            else:
                pfx = re.sub(r'[^A-Za-z0-9]', '', repo_name).upper()[:8] or "TASK"
                task_id = f"{pfx}-{issue_val}"
        else:
            h_suf = hashlib.sha256(f"{repo_name}:{clean_obj}".encode("utf-8")).hexdigest()[:6].upper()
            pfx = re.sub(r'[^A-Za-z0-9]', '', repo_name).upper()[:8] or "TASK"
            task_id = f"{pfx}-{h_suf}"

    if explicit_class:
        task_class = explicit_class
    elif any(k in lowered for k in ["security", "vuln", "auth", "cve", "patch", "exploit"]):
        task_class = "security_patch"
    elif any(k in lowered for k in ["fix", "bug", "crash", "error", "defect", "broken", "fail"]):
        task_class = "bug_fix"
    elif any(k in lowered for k in ["refactor", "clean", "simplify", "restructure"]):
        task_class = "refactor"
    elif any(k in lowered for k in ["test", "falsif", "mock", "assert", "coverage"]):
        task_class = "test_improvement"
    elif any(k in lowered for k in ["doc", "readme", "comment", "guide"]):
        task_class = "docs"
    elif any(k in lowered for k in ["deploy", "infra", "terraform", "helm", "k8s", "docker"]):
        task_class = "infrastructure"
    else:
        task_class = "feature"

    if explicit_risk:
        risk_level = explicit_risk
    elif any(k in lowered for k in ["critical", "production", "deploy", "terraform apply", "drop table", "credential", "secret"]):
        risk_level = "critical"
    elif any(k in lowered for k in ["security", "vuln", "auth", "boundary", "migration", "k8s", "ingress", "infra"]):
        risk_level = "high"
    elif any(k in lowered for k in ["doc", "typo", "readme", "comment", "format", "style", "lint"]):
        risk_level = "low"
    else:
        risk_level = "medium"

    if explicit_tier:
        reasoning_tier = explicit_tier
    elif risk_level in ("high", "critical") or task_class in ("security_patch", "infrastructure"):
        reasoning_tier = "tier_1"
    elif risk_level == "low" and task_class == "docs":
        reasoning_tier = "tier_3"
    else:
        reasoning_tier = "tier_2"

    return task_id, task_class, risk_level, reasoning_tier


def format_agent_launch_command(agent_id: str, spec: TaskSpec, run_dir: Path, target_repo: Path) -> str:
    """Generates the exact recommended agent launch command for the selected agent."""
    t_path = run_dir / "task.yaml"
    rel_p = str(t_path.relative_to(target_repo)) if t_path.is_relative_to(target_repo) else str(t_path)

    if agent_id == "agy":
        return f'agy -p "Task: {spec.task_id} - {spec.objective}. Review task spec at {rel_p} and execute." --mode accept-edits'
    elif agent_id == "claude_code":
        return f'claude "Execute governed task {spec.task_id}: {spec.objective} using control plane spec at {rel_p}"'
    elif agent_id == "codex":
        return f'codex "Execute task {spec.task_id}: {spec.objective} per {rel_p}"'
    elif agent_id == "devin_cli":
        return f'devin run --task-file {rel_p}'
    elif agent_id == "local_ollama":
        return f'ollama run qwen2.5-coder:32b "Task {spec.task_id}: {spec.objective}"'
    return f'# Launch {agent_id} for task spec at {rel_p}'


def create_task_plan(
    ctx: ProjectContext,
    target_repo: Path,
    cp_root: Optional[Path],
    args: argparse.Namespace,
    resource_pool: Optional[ProviderPoolManager] = None,
) -> Tuple[TaskSpec, RoutingDecision]:
    """Helper that constructs and routes a TaskSpec from CLI arguments."""
    tid, tclass, risk, tier = infer_task_metadata(
        objective=args.objective,
        repo_name=ctx.name,
        explicit_id=getattr(args, "task_id", None),
        explicit_risk=getattr(args, "risk", None),
        explicit_tier=getattr(args, "tier", None),
        explicit_class=getattr(args, "task_class", None),
    )
    skills = list(dict.fromkeys(ctx.skills + (getattr(args, "skills", None) or ["software_development"])))
    meta = {"target_repo_path": str(target_repo)}
    if cp_root:
        meta["control_plane_path"] = str(cp_root)

    spec = TaskSpec(
        task_id=tid,
        repository=ctx.name,
        objective=args.objective,
        acceptance_criteria=getattr(args, "criteria", None) or [f"Complete objective: {args.objective}", "Pass deterministic verification suite"],
        constraints=getattr(args, "constraints", None) or ["Adhere to project AGENTS.md and control plane policies"],
        task_class=tclass,
        risk_level=risk,
        required_skills=skills,
        recommended_reasoning_tier=tier,
        preferred_agent=getattr(args, "agent", None),
        metadata=meta,
    )
    decision = TaskRouter(resource_pool=resource_pool).route(spec)
    return spec, decision


def _print_failover_accounting(res: OrchestrationResult) -> None:
    """Explains multi-attempt implementation so exhaustion is never a mystery."""
    attempts = res.implementation_attempts or []
    summary = res.failover_summary or {}
    if len(attempts) <= 1 and res.final_state != "failed":
        print("")
        return
    print("")
    print("Implementation attempts:")
    for attempt in attempts:
        label = attempt.get("resource_id") or "unknown"
        outcome = (
            "SUCCESS" if attempt.get("success")
            else (attempt.get("failure_class") or "FAILED")
        )
        print(f"  {attempt.get('attempt')}. {label:<14} {outcome}")
    if not attempts:
        print("  (none recorded)")
    if summary:
        print("")
        print("Failover:")
        print(
            "  Attempts used:                "
            f"{summary.get('attempts_used')}/{summary.get('attempts_allowed')}"
        )
        remaining = summary.get("remaining_eligible") or []
        print(
            "  Remaining eligible resources: "
            f"{', '.join(remaining) if remaining else 'none'}"
        )
        print(f"  Termination reason:           {summary.get('termination_reason')}")
        excluded = summary.get("excluded") or {}
        if excluded:
            print("  Excluded resources:")
            for resource_id, reason in sorted(excluded.items()):
                print(f"    {resource_id:<14} {reason}")
    print("")


def _print_orchestration_summary(
    res: OrchestrationResult,
    ctx: ProjectContext,
    spec: TaskSpec,
    decision: RoutingDecision,
) -> None:
    """Renders the standard terminal output after governed task execution."""
    status_header = "COMPLETE" if res.final_state == "complete" else (
        "AWAITING HUMAN APPROVAL" if res.final_state == "awaiting_human" else "FAILED"
    )
    print("=" * 60)
    print(f"HOWLPLANE — GOVERNED TASK {status_header}")
    print("=" * 60)
    print(f"Task:              {spec.task_id}")
    print(f"Repository:        {ctx.name}")
    print(f"Risk:              {spec.risk_level.upper()}")
    print("")
    print("Project Context:")
    print("  ProjectAdapter:  OK")
    hf_status = res.howlframe_audit_status
    if not hf_status and getattr(res, "run_dir", None):
        audit_path = Path(res.run_dir) / "howlframe_audit.json"
        if audit_path.exists():
            try:
                audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
                hf_status = audit_data.get("audit_status") or audit_data.get("status")
            except Exception:
                pass

    if hf_status:
        hf_str = hf_status
    elif res.howlframe_audit_match is True:
        hf_str = "PASS / MATCH (shadow)"
    elif res.howlframe_audit_match is False:
        hf_str = "MISMATCH"
    else:
        hf_str = "NOT COMPUTED"
    print(f"  HowlFrame:       {hf_str}")
    print("")
    print("Routing:")
    final_impl_name = res.executing_provider or decision.selected_agent_name
    print(f"  Implementation:  {final_impl_name}")
    if res.executing_provider and res.executing_provider != decision.selected_agent_id:
        print(f"  Initial route:   {decision.selected_agent_name}")
    print(f"  Reasoning Tier:  {decision.reasoning_tier}")
    print("")
    print("Implementation:")
    delta = res.final_delta
    is_failed_impl = (
        res.final_state == "failed"
        and res.provider_execution is not None
        and not res.provider_execution.success
    )
    if is_failed_impl:
        print("  Status:                     FAILED")
        provider_name = res.executing_provider or decision.selected_agent_name
        print(f"  Provider:                   {provider_name}")
        has_partial = delta is not None and not delta.is_empty
        print(f"  Partial repository changes: {'YES' if has_partial else 'NO'}")
        fc = (len(delta.files_modified) + len(delta.files_added)) if has_partial else 0
        ins = delta.insertions if has_partial else 0
        dels = delta.deletions if has_partial else 0
        print(f"  Files Changed:              {fc}")
        print(f"  Insertions:                  {ins}")
        print(f"  Deletions:                  {dels}")
        if has_partial:
            print("  Changes reviewed:           NO")
            print("  Changes verified:           NO")
    else:
        fc = (len(delta.files_modified) + len(delta.files_added)) if delta else 0
        ins = delta.insertions if delta else 0
        dels = delta.deletions if delta else 0
        print(f"  Files Changed:   {fc}")
        print(f"  Insertions:       {ins}")
        print(f"  Deletions:       {dels}")
    _print_failover_accounting(res)
    print("Review:")
    if res.review_cycles:
        last_cycle = res.review_cycles[-1]
        non_independent = set(getattr(last_cycle, "non_independent_roles", []) or [])
        for role_id in decision.recommended_reviewers:
            role_res = last_cycle.reviewer_results.get(role_id)
            role_label = role_id.replace("-reviewer", "").capitalize()
            if role_res is None:
                status_text = "NOT RUN"
            elif role_res.status == "reviewer_failure":
                status_text = "FAILED (no verdict)"
            elif role_res.findings:
                sev_summary = f"{role_res.findings[0].severity.upper()}"
                status_text = f"{sev_summary} → REMEDIATED" if res.final_state == "complete" and res.remediation_cycles_count > 0 else f"{sev_summary} ({len(role_res.findings)} findings)"
            else:
                status_text = "PASS"
            if role_id in non_independent:
                status_text = f"{status_text} ⚠ SELF-REVIEW (implementer)"
            print(f"  {role_label:<17} {status_text}")
    else:
        print("  (No review cycles executed)")
    print("")
    print("Remediation:")
    print(f"  Cycles:           {res.remediation_cycles_count}")
    print("")
    print("Verification:")
    if res.verification_plan and res.verification_plan.steps:
        executed_steps = [
            s for s in res.verification_plan.steps
            if s.exit_code is not None or s.status in ("verified", "failed")
        ]
        if not executed_steps:
            total_steps = len(res.verification_plan.steps)
            print(f"  Discovered:      {total_steps} steps")
            print("  Executed:        0")
            if res.final_state == "failed":
                print("  Status:          NOT RUN — implementation failed before verification")
            else:
                print(f"  Status:          NOT RUN — task {res.final_state} before verification")
        else:
            for s in res.verification_plan.steps:
                status_tag = "VERIFIED" if s.status == "verified" else s.status.upper()
                print(f"  {s.name:<17} {status_tag}")
    else:
        print("  (No automated verification steps discovered)")
    print("")
    print("Human Authority:")
    req_human = (res.final_state == "awaiting_human") or bool(
        res.boundary_result and res.boundary_result.requires_human_approval
    )
    print(f"  Required:         {'Yes (🛑 Triggered)' if req_human else 'No'}")
    print("")
    print("Evidence:")
    print(f"  {res.run_dir}/")
    print("")
    final_verdict = (
        "VERIFIED COMPLETE" if res.final_state == "complete"
        else ("AWAITING HUMAN AUTHORIZATION" if res.final_state == "awaiting_human" else "FAILED")
    )
    print(f"Final State:\n  {final_verdict}")
    print("=" * 60)


TERMINAL_TASK_STATES = frozenset({"complete", "cancelled", "failed"})


def cmd_work(args: argparse.Namespace) -> int:
    """Executes the governed work command from any target repository."""
    target_repo = _resolve_repo(args)
    cp_root = find_control_plane_root(getattr(args, "control_plane_dir", None))

    if not getattr(args, "skip_doctor", False):
        from howlplane.control_plane.doctor import check_dependencies, check_git_status
        dep_res = check_dependencies()
        if dep_res.status == "error" and not getattr(args, "force", False):
            print(f"ERROR: control-plane preflight failed: {dep_res.message}", file=sys.stderr)
            return 1
        git_res = check_git_status(target_repo)
        if git_res.status == "error" and not getattr(args, "force", False):
            print(f"ERROR: target repository preflight failed: {git_res.message}", file=sys.stderr)
            return 1

    ctx = ProjectAdapter.discover(target_repo)
    resource_pool = ProviderPoolManager.from_config()
    spec, decision = create_task_plan(
        ctx, target_repo, cp_root, args, resource_pool=resource_pool
    )

    planned_actions = getattr(args, "actions", None) or []
    progress_mode = getattr(args, "progress", "auto")
    if getattr(args, "quiet", False):
        progress_mode = "never"

    orchestrator = GovernedTaskOrchestrator(
        target_repo=target_repo,
        control_plane_root=cp_root,
        config=OrchestrationConfig(
            force=getattr(args, "force", False),
            skip_doctor=getattr(args, "skip_doctor", False),
            provider_pool=resource_pool,
            progress_mode=progress_mode,
        ),
    )

    if getattr(args, "execute", False):
        res = orchestrator.run(spec, planned_actions=planned_actions)
        _print_orchestration_summary(res, ctx, spec, decision)
        return res.exit_code

    # Dry run / task preparation mode (default when --execute is omitted)
    ctx, decision, plan, run_dir, shadow_audit_res = orchestrator.prepare_task_plan(spec, planned_actions)
    boundary_res = HumanBoundaryGate.evaluate(spec, planned_actions=planned_actions, verification=plan)
    if not decision.selected_agent_id and not boundary_res.requires_human_approval:
        print(json.dumps(decision.metadata["blocked_outcome"], indent=2))
        return 3
    launch_cmd = format_agent_launch_command(decision.selected_agent_id, spec, run_dir, target_repo)
    df_mode = get_dogfood_mode()

    print("=" * 60)
    print("AI ENGINEERING CONTROL PLANE — TASK INITIALIZED")
    print("=" * 60)
    print(f"Target Repository:    {target_repo} ({ctx.name})")
    print(f"Project Stack:        {', '.join(ctx.project_types) or 'generic'}")
    print(f"Project AGENTS.md:    {'Present' if ctx.has_agents_md else 'Not found (using global policy)'}")
    print(f"Hygiene Policy:       {ctx.hygiene_status}")
    print(f"Task ID:              {spec.task_id}")
    print(f"Objective:            {spec.objective}")
    print(f"Risk Level:           {spec.risk_level.upper()}")
    print(f"Reasoning Tier:       {decision.reasoning_tier}")
    if decision.is_override:
        print(f"Override Note:        {decision.override_reason}")
    if df_mode == "shadow" and shadow_audit_res:
        print("-" * 60)
        print("HOWLFRAME SHADOW AUDIT (DOGFOODING):")
        print(f"  Result:             {shadow_audit_res.audit_status or 'N/A'} ({shadow_audit_res.status}) [{shadow_audit_res.duration_seconds}s]")
        if shadow_audit_res.findings:
            print(f"  Findings:           {', '.join(shadow_audit_res.findings)}")
        if shadow_audit_res.comparison_notes:
            print(f"  Disagreements:      {', '.join(shadow_audit_res.comparison_notes)}")
    print("-" * 60)
    print("TASK ROUTING DECISION:")
    print(f"Selected Agent:       {decision.selected_agent_name} (`{decision.selected_agent_id}`)")
    print(f"Rationale:            {decision.rationale}")
    print(f"Reviewer Roles:       {', '.join(decision.recommended_reviewers)}")
    print("-" * 60)
    print("DETERMINISTIC VERIFICATION PLAN:")
    if plan.steps:
        for idx, s in enumerate(plan.steps, 1):
            print(f"  {idx}. [{s.category}] {s.name}")
    else:
        print("  (No automatic test/build steps discovered)")
    print("-" * 60)
    print("HUMAN AUTHORITY BOUNDARY:")
    if boundary_res.requires_human_approval:
        print("  🛑 AWAITING HUMAN APPROVAL (Boundary Triggered)")
        for b in boundary_res.triggered_boundaries:
            print(f"     - Boundary: {b}")
        if boundary_res.decision_packet:
            dp_path = run_dir / "decision_packet.md"
            dp_path.write_text(boundary_res.decision_packet.render_markdown(), encoding="utf-8")
            print(f"     - Decision packet written to: {dp_path}")
    else:
        print("  ✓ All actions within Autonomous Operating Authority")
    print("-" * 60)
    print("RUN ARTIFACTS PREPARED:")
    print(f"Run Directory:        {run_dir}")
    print(f"- Task Spec:          {run_dir / 'task.yaml'}")
    print(f"- Review Briefs:      {run_dir / 'reviews'}/ ({len(decision.recommended_reviewers)} briefs)")
    print(f"- Findings Template:  {run_dir / 'findings_template.yaml'}")
    print(f"- Verification Plan:  {run_dir / 'verification_plan.json'}")
    print("-" * 60)
    print("RECOMMENDED AGENT LAUNCH COMMAND:")
    print(f"  {launch_cmd}")
    print("=" * 60)

    return 2 if boundary_res.requires_human_approval else 0


def cmd_route(args: argparse.Namespace) -> int:
    """Lightweight read-only routing of an objective against the current target repository."""
    target_repo = _resolve_repo(args)
    ctx = ProjectAdapter.discover(target_repo)
    pool = ProviderPoolManager.from_config(read_only=True, probe_on_start=False)
    semantic_mode = getattr(args, "semantic", None)
    if semantic_mode:
        try:
            pool.configure_semantic(
                SemanticRecommendationSettings.model_validate(
                    {**pool.semantic_settings.model_dump(), "mode": semantic_mode}
                )
            )
        except ValueError as exc:
            print(f"Error: invalid semantic recommendation settings: {exc}", file=sys.stderr)
            return 2
    spec, _decision = create_task_plan(
        ctx, target_repo, None, args, resource_pool=pool
    )
    selection = pool.select_resource(
        spec,
        role=getattr(args, "role", "implementation"),
        explicit_resource_id=getattr(args, "agent", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(selection.to_dict(), indent=2))
    else:
        print(f"Target Repository: {target_repo} ({ctx.name})")
        print(f"Objective: {spec.objective}\n")
        print(render_route(selection))
    return 0 if selection.selected else 3


def cmd_providers(args: argparse.Namespace) -> int:
    """Shows inventory or resets exactly one current capacity record."""
    action = getattr(args, "provider_action", None)
    resource_id = getattr(args, "resource_id", None)
    if action == "reset":
        if not resource_id:
            print("ERROR: howlplane providers reset requires a resource ID", file=sys.stderr)
            return 1
        pool = ProviderPoolManager.from_config(probe_on_start=False)
        state = pool.reset_resource(resource_id, reprobe=True)
        try:
            import howlplane.control_plane.launcher as _launcher
            ledger_cls = getattr(_launcher, "EvidenceLedger", EvidenceLedger)
        except Exception:
            ledger_cls = EvidenceLedger
        ledger_cls().append_entry(EvidenceEntry(
            task_id="AI-RESOURCE-POOL",
            agent_id="operator",
            action="provider_capacity_reset",
            result="reset",
            metadata={
                "resource_id": resource_id,
                "capacity": state.status.value,
                "readiness": state.readiness.value,
            },
        ))
        print(json.dumps(state.to_dict(), indent=2) if getattr(args, "json", False) else (
            f"Reset {resource_id}: readiness={state.readiness.value}; "
            f"capacity={state.status.value}"
        ))
        return 0
    pool = ProviderPoolManager.from_config(read_only=True, probe_on_start=True)
    print(
        json.dumps(inventory_document(pool), indent=2)
        if getattr(args, "json", False) else render_inventory(pool)
    )
    return 0




def cmd_init_task(args: argparse.Namespace) -> int:
    """Creates a new TaskSpec file."""
    spec = TaskSpec(
        task_id=args.task_id,
        repository=args.repo,
        objective=args.objective,
        acceptance_criteria=args.criteria or [],
        constraints=args.constraints or [],
        task_class=args.task_class or "feature",
        risk_level=args.risk,
        required_skills=args.skills or [],
        recommended_reasoning_tier=args.tier,
        preferred_agent=args.preferred_agent,
    )
    out_path = args.output or f"task_{spec.task_id}.yaml"
    spec.save_to_file(out_path)
    print(f"Task specification written to: {out_path}")
    return 0


def cmd_route_task(args: argparse.Namespace) -> int:
    """Routes a task specification to an agent and reviewers."""
    spec = TaskSpec.load_from_file(args.task_file)
    router = TaskRouter()
    decision = router.route(spec)
    print(decision.render_text(spec.task_id))
    return 0


def cmd_briefs(args: argparse.Namespace) -> int:
    """Generates independent review briefs for a task diff."""
    spec = TaskSpec.load_from_file(args.task_file)
    diff_text = Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else ""

    router = TaskRouter()
    decision = router.route(spec)
    reviewers_to_run = args.roles or decision.recommended_reviewers

    out_dir = Path(args.output_dir or "review_briefs")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating review briefs for {len(reviewers_to_run)} roles in {out_dir}/...")
    for role_id in reviewers_to_run:
        role = get_reviewer_role(role_id)
        if not role:
            print(f"Warning: Unknown reviewer role '{role_id}', skipping.")
            continue
        brief = role.render_brief(task=spec, diff_content=diff_text)
        brief_file = out_dir / f"brief_{role_id}.md"
        brief_file.write_text(brief, encoding="utf-8")
        print(f" - Wrote: {brief_file}")
    return 0


def cmd_prepare_run(args: argparse.Namespace) -> int:
    """Prepares structured task run directory and reviewer briefs for cross-agent handoffs."""
    spec = TaskSpec.load_from_file(args.task_file)
    diff_text = Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else ""

    run_dir = Path(args.run_dir or f".task_runs/{spec.task_id}")
    reviews_dir = run_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    # Save copy of task spec and diff
    spec.save_to_file(str(run_dir / "task.yaml"))
    if diff_text:
        (run_dir / "diff.patch").write_text(diff_text, encoding="utf-8")

    router = TaskRouter()
    decision = router.route(spec)
    reviewers_to_run = args.roles or decision.recommended_reviewers

    print(f"Preparing dogfood task run in {run_dir}/...")
    print(f"- Implementation Agent: {decision.selected_agent_name} (`{decision.selected_agent_id}`)")
    print(f"- Generating {len(reviewers_to_run)} independent reviewer briefs in {reviews_dir}/:")

    for role_id in reviewers_to_run:
        role = get_reviewer_role(role_id)
        if not role:
            continue
        brief = role.render_brief(task=spec, diff_content=diff_text)
        brief_file = reviews_dir / f"{role_id}.md"
        brief_file.write_text(brief, encoding="utf-8")
        print(f"   ✓ {brief_file.name}")

    # Write findings template
    template_file = run_dir / "findings_template.yaml"
    template_content = """# Review Findings Template
# Collect structured findings from independent reviewer roles below.
findings:
  # Example:
  # - id: "F001"
  #   reviewer_role: "test-falsifier"
  #   title: "Missing negative test case for edge input"
  #   severity: "high"
  #   category: "test_gap"
  #   location: "tests/test_feature.py:45"
  #   description: "Test does not check None input handling."
  #   evidence: "Calling feature(None) raises unhandled exception."
  #   suggested_fix: "Add test_feature_handles_none_gracefully."
"""
    template_file.write_text(template_content, encoding="utf-8")
    print(f"- Template written: {template_file}")
    print(f"\nTask run initialized successfully at {run_dir}.")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Reconciles findings from a YAML/JSON findings file."""
    import yaml
    findings_raw = yaml.safe_load(Path(args.findings_file).read_text(encoding="utf-8"))
    if isinstance(findings_raw, dict):
        findings_raw = findings_raw.get("findings", []) or []

    findings = [ReviewFinding.from_dict(f) for f in findings_raw]
    res = ReviewReconciler.reconcile(findings)
    md_report = res.render_markdown()
    print(md_report)

    out_file = args.output
    if not out_file and args.run_dir:
        out_file = str(Path(args.run_dir) / "reconciliation_report.md")

    if out_file:
        Path(out_file).write_text(md_report, encoding="utf-8")
        print(f"\nSaved report to {out_file}")

    if res.unresolved_blockers > 0:
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Executes a verification plan for a project or task."""
    target_repo = _resolve_repo(args)
    context = ProjectAdapter.discover(target_repo)
    task_id = getattr(args, "task_id", None) or "VERIFY-RUN"

    plan = ProjectAdapter.create_verification_plan(context, task_id=task_id)
    print(f"Running verification plan for project '{context.name}' ({len(plan.steps)} steps)...")
    status = plan.execute_all(cwd=str(target_repo))

    for step in plan.steps:
        mark = "✓" if step.status == "verified" else "✗"
        interp_info = f" [using {step.interpreter}]" if step.interpreter else ""
        print(f"[{mark}] {step.name}: {step.status} (exit {step.exit_code}, {step.duration_seconds}s){interp_info}")
        if step.status == "failed" and step.stderr:
            print(f"    Error: {step.stderr.strip()[:200]}")

    print(f"\nOverall Verification Status: {status.upper()}")

    out_file = args.output
    if not out_file and args.run_dir:
        out_file = str(Path(args.run_dir) / "verification.json")

    if out_file:
        Path(out_file).parent.mkdir(parents=True, exist_ok=True)
        Path(out_file).write_text(plan.to_json(), encoding="utf-8")
        print(f"Saved verification output to {out_file}")

    return 0 if status == "passed" else 1


def cmd_record(args: argparse.Namespace) -> int:
    """Appends an event entry to the evidence ledger."""
    import json
    ledger = EvidenceLedger(args.ledger_file)
    findings_sum = None
    if args.findings_json:
        try:
            findings_sum = json.loads(args.findings_json)
        except Exception:
            pass

    meta = {}
    if args.failure_mode:
        meta["failure_mode"] = args.failure_mode
    if args.repository:
        meta["repository"] = args.repository
    if args.reviewer_role:
        meta["reviewer_role"] = args.reviewer_role

    entry = EvidenceEntry(
        task_id=args.task_id,
        agent_id=args.agent_id,
        action=args.action,
        command=args.command,
        result=args.result,
        artifact=args.artifact,
        task_class=args.task_class,
        risk_level=args.risk_level,
        reasoning_tier=args.reasoning_tier,
        implementing_agent=args.implementing_agent or args.actual_agent,
        recommended_agent=args.recommended_agent,
        actual_agent=args.actual_agent or args.implementing_agent,
        is_override=args.is_override,
        override_reason=args.override_reason,
        defect_type=args.defect_type,
        orchestration_action=args.orchestration_action,
        repository=args.repository,
        reviewing_agents=args.reviewing_agents,
        remediation_cycles=args.remediation_cycles,
        control_plane_caught_defect=args.defect_caught,
        findings_summary=findings_sum,
        metadata=meta,
    )
    try:
        ledger.append_entry(entry)
    except EvidenceSchemaError as exc:
        raise OperatorFailure(OperatorError(
            "EVIDENCE_SCHEMA_INVALID", f"The evidence entry was not recorded: {exc}",
            "Evidence must match schemas/evidence-entry.schema.json before it is written.",
            "Use a known action name (see the schema's action list) and valid field types."))
    print(f"Recorded evidence entry '{entry.entry_id}' for task '{entry.task_id}'.")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Calculates and displays engineering history metrics."""
    ledger = EvidenceLedger(args.ledger_file)
    entries, diagnostics = ledger.read_entries()
    if diagnostics:
        print(
            f"warning: {len(diagnostics)} evidence record(s) are malformed or schema-invalid "
            f"(first: line {diagnostics[0].line_number}, {diagnostics[0].kind}); run `howlplane doctor`.",
            file=sys.stderr,
        )
    summary = MetricsCalculator.calculate(entries)
    if getattr(args, "format", "markdown") == "json":
        import json
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        print(summary.render_markdown())
    return 0


def cmd_boundary(args: argparse.Namespace) -> int:
    """Evaluates human authority boundaries for a task."""
    spec = TaskSpec.load_from_file(args.task_file)
    actions = args.actions or []
    res = HumanBoundaryGate.evaluate(spec, planned_actions=actions)

    if res.requires_human_approval and res.decision_packet:
        md = res.decision_packet.render_markdown()
        print(md)
        out_file = args.output
        if not out_file and getattr(args, "run_dir", None):
            out_file = str(Path(args.run_dir) / "decision_packet.md")
        if out_file:
            Path(out_file).parent.mkdir(parents=True, exist_ok=True)
            Path(out_file).write_text(md, encoding="utf-8")
            print(f"\nSaved decision packet to {out_file}")
        return 2  # Signal awaiting human
    else:
        print("✓ All actions within autonomous operating authority (no human boundary triggered).")
        return 0




def cmd_howlframe_audit(args: argparse.Namespace) -> int:
    """Executes HowlFrame project context audit on target repository."""
    import json
    target_repo = _resolve_repo(args)
    ledger_file = _resolve_ledger_file(args)
    context = ProjectAdapter.discover(target_repo)
    max_instructions = getattr(args, "max_instructions", DEFAULT_INSTRUCTION_BUDGET)
    task_id = getattr(args, "task_id", None)
    ledger = EvidenceLedger(ledger_file) if ledger_file else None

    print(f"Running HowlFrame project context audit for '{context.name}'...")
    res = HowlFrameAuditRunner.run_audit(
        context=context,
        max_instructions=max_instructions,
        task_id=task_id,
        ledger=ledger,
        record_evidence=True,
    )

    print("=" * 60)
    print(f"HOWLFRAME PROJECT CONTEXT AUDIT: {context.name}")
    print("=" * 60)
    print(f"Comparison Result:  {res.status}")
    print(f"Audit Status:       {res.audit_status or 'N/A'}")
    print(f"Execution Duration: {res.duration_seconds}s")
    print(f"Instruction Budget: {res.instruction_budget}")
    if res.howlframe_version:
        print(f"HowlFrame Version:  {res.howlframe_version}")
    if res.findings:
        print("Audit Findings:")
        for f in res.findings:
            print(f"  - {f}")
    if res.comparison_notes:
        print("Comparison Notes:")
        for n in res.comparison_notes:
            print(f"  - {n}")
    if res.error_message:
        print(f"Error Message:      {res.error_message}")
    print("=" * 60)

    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))

    if res.status in ("MATCH", "HOWLFRAME_UNAVAILABLE"):
        return 0
    elif res.status == "MISMATCH":
        return 1
    else:
        return 2


def cmd_explore(args: argparse.Namespace) -> int:
    """Executes a governed HowlDream exploration cycle."""
    import json
    from howlplane.control_plane.howldream_runner import (
        HowlDreamRunner,
        ExplorationPolicy,
        ExplorationBudget,
    )

    policy_str = getattr(args, "policy", "MANUAL")
    try:
        policy = ExplorationPolicy(policy_str)
    except ValueError:
        policy = ExplorationPolicy.MANUAL

    budget = ExplorationBudget(
        max_candidates=getattr(args, "budget_candidates", 5),
        max_trials=getattr(args, "budget_trials", 3),
        max_tokens=getattr(args, "budget_tokens", 2048),
    )

    repo_dir = Path(getattr(args, "repo_dir", None) or ".").resolve()
    runner = HowlDreamRunner(policy=policy)

    if not getattr(args, "json", False):
        print(f"Running HowlDream exploration for objective: {args.objective}")
        print(f"Policy: {policy.value}, Working directory: {repo_dir}")

    res = runner.dispatch_exploration(
        objective=args.objective,
        budget=budget,
        policy=policy,
        repo_dir=repo_dir,
    )

    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print("=" * 60)
        print("HOWLDREAM EXPLORATION RESULT")
        print("=" * 60)
        print(f"Status:             {res.status}")
        print(f"Policy:             {res.policy}")
        print(f"Objective:          {res.objective}")
        print(f"Candidates Found:   {len(res.candidates)}")
        print(f"Assessments Done:   {len(res.assessments)}")
        print(f"Developed (Sandbox):{len(res.development_results)}")
        if res.envelope_path:
            print(f"Envelope:           {res.envelope_path}")
        if res.error_message:
            print(f"Message:            {res.error_message}")
        print(f"Duration:           {res.duration_seconds}s")
        print("=" * 60)

    if res.status in ("SUCCESS", "NO_CANDIDATES", "SKIPPED", "UNAVAILABLE"):
        return 0
    elif res.status == "REJECTED":
        return 1
    else:
        return 2


def cmd_trace(args: argparse.Namespace) -> int:
    """Traces descent lineage DAG for an exploration run or candidate."""
    import json
    repo_dir = Path(getattr(args, "repo_dir", None) or ".").resolve()
    trace_id = args.trace_id

    try:
        has_howldream = False
        try:
            from howldream.contracts import DescentDAG

            has_howldream = True
        except ImportError:
            DescentDAG = None

        found_chain = None
        for envelope_path in repo_dir.glob("**/exploration_envelope.json"):
            if envelope_path.is_file():
                with open(envelope_path, "r", encoding="utf-8") as f:
                    env_data = json.load(f)
                dag_data = env_data.get("descent_dag") or env_data.get("lineage_dag")
                if not dag_data:
                    continue

                if has_howldream and DescentDAG is not None:
                    dag = DescentDAG.model_validate(dag_data)
                    if trace_id in dag.nodes:
                        chain = dag.trace(trace_id)
                        found_chain = [node.model_dump() for node in chain]
                        break
                else:
                    nodes = dag_data.get("nodes", {})
                    if isinstance(nodes, list):
                        nodes = {n.get("node_id"): n for n in nodes if isinstance(n, dict)}
                    if trace_id in nodes:
                        edges = dag_data.get("edges", [])
                        visited = set()
                        chain = []

                        def _walk(curr: str):
                            if curr in visited or curr not in nodes:
                                return
                            visited.add(curr)
                            chain.append(nodes[curr])
                            for edge in edges:
                                if isinstance(edge, dict) and edge.get("target") == curr:
                                    src = edge.get("source")
                                    if src:
                                        _walk(src)

                        _walk(trace_id)
                        found_chain = chain
                        break

        if found_chain is not None:
            if getattr(args, "json", False):
                print(json.dumps(found_chain, indent=2))
            else:
                print(f"Lineage trace for '{trace_id}' ({len(found_chain)} nodes):")
                for node in found_chain:
                    node_type = node.get("node_type", "unknown")
                    node_id = node.get("node_id", "")
                    label = node.get("label", "")
                    print(f"  - [{node_type}] {node_id}: {label}")
            return 0
        else:
            print(f"Node or candidate '{trace_id}' not found in lineage DAGs.")
            return 1
    except Exception as exc:
        print(f"Error tracing '{trace_id}': {exc}")
        return 1



def _grouped_help_formatter(groups, default_title):
    """Help formatter that lists subcommands in named groups.

    Every registered subcommand still appears: anything not named in `groups`
    lands under `default_title`, so adding a command can never hide it.
    """
    class GroupedHelpFormatter(argparse.RawDescriptionHelpFormatter):
        def _format_action(self, action):
            if not isinstance(action, argparse._SubParsersAction):
                return super()._format_action(action)
            entries = {a.dest: a for a in action._get_subactions()}
            placed = set()
            out = []
            sections = [(title, [n for n in names if n in entries]) for title, names in groups]
            sections.append((default_title, [n for n in entries if not any(n in names for _, names in groups)]))
            for title, names in sections:
                if not names:
                    continue
                out.append(f"\n{title}:\n")
                for name in names:
                    placed.add(name)
                    out.append(super()._format_action(entries[name]))
            return "".join(out)

    return GroupedHelpFormatter


_TOP_LEVEL_HELP_GROUPS = [
    ("Everyday", ["setup", "start", "status", "logs", "stop", "approve", "reject", "doctor"]),
    ("Governance and recovery", ["resume", "cancel", "unlock", "tia", "config"]),
    ("Diagnostics and inspection", ["agents", "providers", "route", "trace", "explore", "metrics", "report"]),
    ("Creative pipeline", ["creative"]),
    ("Factory internals", ["factory"]),
]

_FACTORY_HELP_GROUPS = [
    ("Everyday (start, status, logs, stop)", ["start", "status", "logs", "stop"]),
    ("Setup and recovery", ["doctor", "prepare", "resume"]),
]

_TOP_LEVEL_QUICKSTART = """\
New here? Run these inside a Git repository:
  howlplane setup       get this repository ready (once)
  howlplane start       start working
  howlplane status      see what is happening and what, if anything, you need to do
  howlplane logs        see the details (add --follow to watch)
  howlplane stop        pause safely; howlplane start continues

When it asks for you: howlplane approve ID  (or reject ID).
Something wrong? howlplane doctor.
Add --json for scripts. Add --debug for a full traceback; NO_COLOR or --color never for plain text.
"""

class _FriendlyArgumentParser(argparse.ArgumentParser):
    """Argparse that answers a mistyped command with one line, not a 40-name dump."""

    def error(self, message: str) -> Any:
        import re
        import sys
        match = re.search(r"argument <command>: invalid choice: '([^']*)'", message)
        if match:
            self.exit(2, f"{self.prog}: unknown command '{match.group(1)}'. "
                         f"Run `{self.prog} --help` to see what is available.\n")
        super().error(message)


def build_parser(program_name: str = "howlplane") -> argparse.ArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--control-plane-dir",
        "-C",
        help="Explicit path to HowlPlane control plane repository",
    )
    common_parser.add_argument(
        "--repo",
        "-R",
        help="Target Git repository directory (defaults to current working directory discovery)",
    )

    task_base_parser = argparse.ArgumentParser(add_help=False)
    task_base_parser.add_argument("objective", help="Task objective or description")
    task_base_parser.add_argument("--task-id", help="Explicit task ID")
    task_base_parser.add_argument("--risk", choices=["low", "medium", "high", "critical"], help="Risk level override")
    task_base_parser.add_argument("--tier", choices=["tier_1", "tier_2", "tier_3"], help="Reasoning tier override")
    task_base_parser.add_argument("--agent", help="Preferred agent override")

    parser = _FriendlyArgumentParser(
        prog=program_name,
        description="Deterministic Multi-Agent Engineering Control Plane CLI\n\n" + _TOP_LEVEL_QUICKSTART,
        formatter_class=_grouped_help_formatter(_TOP_LEVEL_HELP_GROUPS, "Advanced engineering"),
        parents=[common_parser],
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{program_name} {__version__}",
    )
    # Handled by main() before parsing so they work before or after the command.
    parser.add_argument("--color", metavar="MODE", help="auto (default), always or never; NO_COLOR is honored")
    parser.add_argument("--debug", action="store_true", help="Print full tracebacks for unexpected failures")

    parser._positionals.title = "Commands"
    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>", help="Command to execute")

    from howlplane.control_plane.orchestration import add_parser as add_orchestration_parser
    add_orchestration_parser(subparsers, common_parser)

    # work
    p_work = subparsers.add_parser(
        "work",
        parents=[common_parser, task_base_parser],
        help="Run governed control-plane workflow on current repository",
    )
    p_work.add_argument("--task-class", help="Task class")
    p_work.add_argument("--criteria", nargs="*", help="Acceptance criteria list")
    p_work.add_argument("--constraints", nargs="*", help="Constraints list")
    p_work.add_argument("--actions", nargs="*", help="Planned actions for authority boundary checks")
    p_work.add_argument("--execute", "-x", action="store_true", help="Launch recommended agent CLI and execute closed loop")
    p_work.add_argument("--dry-run", action="store_true", help="Generate plan without launching")
    p_work.add_argument("--skip-doctor", action="store_true", help="Skip preflight diagnostics")
    p_work.add_argument("--force", action="store_true", help="Proceed even if preflight has warnings")
    p_work.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Operator progress output mode (auto, always, never)",
    )
    p_work.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress operator progress output",
    )

    # route
    p_route = subparsers.add_parser(
        "route",
        parents=[common_parser, task_base_parser],
        help="Route an objective against the current repository",
    )
    p_route.add_argument(
        "--role",
        choices=["planning", "implementation", "remediation", "review"],
        default="implementation",
    )
    p_route.add_argument("--json", action="store_true", help="Output JSON decision")
    p_route.add_argument(
        "--semantic",
        choices=["off", "shadow", "active"],
        default=None,
        help="Override the configured HowlInstinct mode for this route only",
    )

    # providers
    p_providers = subparsers.add_parser(
        "providers", parents=[common_parser], help="Show the configured AI resource pool"
    )
    p_providers.add_argument("provider_action", nargs="?", choices=["reset"])
    p_providers.add_argument("resource_id", nargs="?")
    p_providers.add_argument("--json", action="store_true", help="Output versioned JSON")

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor", parents=[common_parser], help="Check that HowlPlane is ready and healthy (system, workers, Factory)")
    doctor_scope = p_doctor.add_mutually_exclusive_group()
    doctor_scope.add_argument("--system", action="store_true", help="Only the system checks (Python, Git, configuration)")
    doctor_scope.add_argument("--agents", action="store_true", help="Only the AI worker checks")
    doctor_scope.add_argument("--factory", action="store_true", help="Only the repository and Factory checks")
    doctor_scope.add_argument("--ready", action="store_true",
                              help="Print READY or NOT READY with the fix for each blocker; exit 1 when not ready")
    doctor_scope.add_argument("--creative", action="store_true",
                              help="Preflight the Dream -> Writer -> Create pipeline (PASS/WARN/FAIL)")
    p_doctor.add_argument("--command-config", help="With --creative: remote provider profile to validate")
    p_doctor.add_argument("--workspace", help="With --creative: workspace to check for write access")
    p_doctor.add_argument("--live", action="store_true", help="Run a short read-only smoke test of each worker")
    _add_workspace_trust_argument(p_doctor)
    p_doctor.add_argument("--json", action="store_true", help="Output versioned JSON")

    from howlplane.control_plane import creative_pipeline
    creative_pipeline.add_parser(subparsers, common_parser)

    # agents
    p_agents = subparsers.add_parser("agents", parents=[common_parser], help="Inspect supported agent CLIs")
    agents_sub = p_agents.add_subparsers(dest="agents_action")
    p_agents_doctor = agents_sub.add_parser(
        "doctor", help="Verify each agent CLI's readiness, unattended execution, models, and capacity visibility"
    )
    _add_readiness_arguments(p_agents_doctor)
    p_agents_doctor.add_argument("--agent", action="append", choices=list(agent_readiness.AGENT_ORDER),
                                 help="Limit to one agent (repeatable)")
    p_agents_doctor.add_argument("--refresh", action="store_true", help="Ignore cached evidence and probe again")
    p_agents_doctor.add_argument("--smoke-timeout", type=int, default=agent_readiness.DEFAULT_SMOKE_TIMEOUT_SECONDS,
                                 metavar="SECONDS", help="Per-agent limit for the --live smoke (default: 60)")
    p_agents_doctor.add_argument("--repo", metavar="PATH",
                                 help="Also report workspace readiness (trust) for this directory; "
                                      "with --live the read-only smoke runs there")
    _add_workspace_trust_argument(p_agents_doctor)

    # status
    p_status_top = subparsers.add_parser(
        "status", parents=[common_parser], help="Show what HowlPlane is doing and what, if anything, you need to do")
    p_status_top.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_status_top.add_argument("--target-repo", help="Repository to resolve (advanced)")
    p_status_top.add_argument("--json", action="store_true", help="Output JSON result")
    p_status_top.add_argument("--verbose", "-v", action="store_true",
                              help="Show project diagnostics: verification plan, locks, task runs")

    p_start_top = subparsers.add_parser(
        "start", parents=[common_parser], help="Start HowlPlane working on this repository (or continue after a stop)")
    p_start_top.add_argument("objective_text", nargs="?", metavar="OBJECTIVE",
                             help="Optional goal, for example: \"continue building the search feature\"")
    p_start_top.add_argument("--retry", action="store_true",
                             help="Continue after a failure you have looked at")
    _add_start_arguments(p_start_top, preflight=False)

    p_logs_top = subparsers.add_parser(
        "logs", parents=[common_parser], help="Show what HowlPlane has been doing",
        epilog="examples:\n  howlplane logs --follow\n  howlplane logs --errors --since 1h\n  howlplane logs --work-item WI-042",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_logs_arguments(p_logs_top)

    p_stop_top = subparsers.add_parser(
        "stop", parents=[common_parser], help="Pause HowlPlane safely; start again any time with: howlplane start")
    _add_stop_arguments(p_stop_top)

    # init-task
    p_init = subparsers.add_parser("init-task", parents=[common_parser], help="Initialize a new task specification")
    p_init.add_argument("--task-id", required=True, help="Task ID (e.g. TASK-101)")
    p_init.add_argument("--objective", required=True, help="Task objective description")
    p_init.add_argument("--criteria", nargs="*", help="Acceptance criteria")
    p_init.add_argument("--constraints", nargs="*", help="Constraints")
    p_init.add_argument("--task-class", default="feature", help="Task class / category")
    p_init.add_argument("--risk", default="medium", choices=["low", "medium", "high", "critical"])
    p_init.add_argument("--skills", nargs="*", help="Required skills")
    p_init.add_argument("--tier", default="tier_2", choices=["tier_1", "tier_2", "tier_3"])
    p_init.add_argument("--preferred-agent", help="Preferred agent ID override")
    p_init.add_argument("--output", "-o", help="Output file path (YAML or JSON)")

    # route-task
    p_route_task = subparsers.add_parser("route-task", parents=[common_parser], help="Route task to appropriate agent and reviewers")
    p_route_task.add_argument("--task-file", required=True, help="Path to task spec file")

    # briefs
    p_briefs = subparsers.add_parser("briefs", parents=[common_parser], help="Generate independent reviewer briefs")
    p_briefs.add_argument("--task-file", required=True, help="Path to task spec file")
    p_briefs.add_argument("--diff-file", help="Path to diff file")
    p_briefs.add_argument("--roles", nargs="*", help="Specific reviewer roles to generate")
    p_briefs.add_argument("--output-dir", help="Output directory for briefs")

    # prepare-run
    p_prep = subparsers.add_parser("prepare-run", parents=[common_parser], help="Prepare cross-agent dogfood task run artifacts")
    p_prep.add_argument("--task-file", required=True, help="Path to task spec file")
    p_prep.add_argument("--diff-file", help="Path to diff file")
    p_prep.add_argument("--roles", nargs="*", help="Specific reviewer roles to generate")
    p_prep.add_argument("--run-dir", help="Task run directory path")

    # reconcile
    p_rec = subparsers.add_parser("reconcile", parents=[common_parser], help="Reconcile multi-agent review findings")
    p_rec.add_argument("--findings-file", required=True, help="YAML/JSON findings file")
    p_rec.add_argument("--output", "-o", help="Save markdown report to path")
    p_rec.add_argument("--run-dir", help="Task run directory path")

    # verify
    p_ver = subparsers.add_parser("verify", parents=[common_parser], help="Execute project verification plan")
    p_ver.add_argument("--project-dir", help="Target project root directory")
    p_ver.add_argument("--task-id", help="Optional task ID")
    p_ver.add_argument("--output", "-o", help="Save verification JSON output to path")
    p_ver.add_argument("--run-dir", help="Task run directory path to store verification.json")

    # record
    p_rec_ev = subparsers.add_parser("record", parents=[common_parser], help="Record evidence entry to ledger")
    p_rec_ev.add_argument("--task-id", required=True, help="Task ID")
    p_rec_ev.add_argument("--agent-id", required=True, help="Agent ID")
    p_rec_ev.add_argument("--action", required=True, help="Action performed")
    p_rec_ev.add_argument("--command", help="Command executed")
    p_rec_ev.add_argument("--result", help="Command result")
    p_rec_ev.add_argument("--artifact", help="Artifact produced")
    p_rec_ev.add_argument("--task-class", help="Task class")
    p_rec_ev.add_argument("--risk-level", help="Risk level")
    p_rec_ev.add_argument("--reasoning-tier", help="Reasoning tier")
    p_rec_ev.add_argument("--implementing-agent", help="Implementing agent ID")
    p_rec_ev.add_argument("--recommended-agent", help="Recommended agent ID")
    p_rec_ev.add_argument("--actual-agent", help="Actual implementing agent ID")
    p_rec_ev.add_argument("--is-override", action="store_true", help="Flag if routing was overridden")
    p_rec_ev.add_argument("--override-reason", help="Reason for human routing override")
    p_rec_ev.add_argument("--defect-type", choices=["review_caught_defect", "verification_caught_defect", "boundary_caught_risk"], help="Defect type caught by control plane")
    p_rec_ev.add_argument("--orchestration-action", help="Orchestration action for tracking human friction")
    p_rec_ev.add_argument("--repository", help="Repository name or path")
    p_rec_ev.add_argument("--reviewer-role", help="Reviewer role for caught defect provenance")
    p_rec_ev.add_argument("--reviewing-agents", nargs="*", help="Reviewing agent IDs")
    p_rec_ev.add_argument("--remediation-cycles", type=int, help="Remediation cycle count")
    p_rec_ev.add_argument("--defect-caught", action="store_true", help="Flag if control plane caught a defect")
    p_rec_ev.add_argument("--findings-json", help="JSON summary of findings")
    p_rec_ev.add_argument("--failure-mode", help="Failure mode string")
    p_rec_ev.add_argument("--ledger-file", help="Ledger file path")

    # metrics / report
    p_met = subparsers.add_parser("metrics", parents=[common_parser], help="Calculate agent performance metrics")
    p_met.add_argument("--ledger-file", help="Ledger file path")
    p_met.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")

    p_rep = subparsers.add_parser("report", parents=[common_parser], help="Display full operational summary report")
    p_rep.add_argument("--ledger-file", help="Ledger file path")
    p_rep.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")

    # boundary
    p_bound = subparsers.add_parser("check-boundary", parents=[common_parser], help="Evaluate human authority boundary")
    p_bound.add_argument("--task-file", required=True, help="Path to task spec file")
    p_bound.add_argument("--actions", nargs="*", help="Planned actions or commands")
    p_bound.add_argument("--output", "-o", help="Save decision packet markdown to path")
    p_bound.add_argument("--run-dir", help="Task run directory path")

    # howlframe-audit
    p_ha = subparsers.add_parser("howlframe-audit", parents=[common_parser], help="Run HowlFrame project context audit")
    p_ha.add_argument("--project-dir", help="Target project root directory")
    p_ha.add_argument("--max-instructions", type=int, default=DEFAULT_INSTRUCTION_BUDGET, help="Instruction budget limit")
    p_ha.add_argument("--task-id", help="Optional task ID for evidence ledger")
    p_ha.add_argument("--ledger-file", help="Ledger file path")
    p_ha.add_argument("--json", action="store_true", help="Output JSON result")

    # approve
    p_appr = subparsers.add_parser("approve", parents=[common_parser], help="Approve an awaiting_human task, a parked work item, or a repository proposal")
    p_appr.add_argument("task_id", nargs="?", help="Task ID to approve")
    p_appr.add_argument("--work-item", help="Parked Factory work item to approve (instead of a task ID)")
    p_appr.add_argument("--proposal", help="Repository proposal awaiting authority to approve (instead of a task ID)")
    p_appr.add_argument("--state-dir", help="Factory state directory holding the work item or proposal")
    p_appr.add_argument("--reason", help="Optional human reason for approval")
    p_appr.add_argument("--ledger-file", help="Ledger file path")
    p_appr.add_argument("--json", action="store_true", help="Output JSON result")

    # reject
    p_rej = subparsers.add_parser("reject", parents=[common_parser], help="Reject an awaiting_human task, a parked work item, or a repository proposal")
    p_rej.add_argument("task_id", nargs="?", help="Task ID to reject")
    p_rej.add_argument("--work-item", help="Parked Factory work item to reject (instead of a task ID)")
    p_rej.add_argument("--proposal", help="Repository proposal awaiting authority to reject (instead of a task ID)")
    p_rej.add_argument("--state-dir", help="Factory state directory holding the work item or proposal")
    p_rej.add_argument("--reason", help="Optional human reason for rejection")
    p_rej.add_argument("--ledger-file", help="Ledger file path")
    p_rej.add_argument("--json", action="store_true", help="Output JSON result")

    # resume
    p_res = subparsers.add_parser("resume", parents=[common_parser], help="Resume an approved task")
    p_res.add_argument("task_id", help="Task ID to resume")
    p_res.add_argument("--ledger-file", help="Ledger file path")
    p_res.add_argument("--json", action="store_true", help="Output JSON result")

    # cancel
    p_can = subparsers.add_parser("cancel", parents=[common_parser], help="Cancel an active or interrupted task run")
    p_can.add_argument("task_id", help="Task ID to cancel")
    p_can.add_argument("--reason", help="Optional reason for cancellation")
    p_can.add_argument("--ledger-file", help="Ledger file path")
    p_can.add_argument("--json", action="store_true", help="Output JSON result")

    # unlock
    p_unlock = subparsers.add_parser(
        "unlock",
        parents=[common_parser],
        help="Reclaim a task-run lock whose owner is gone or unverifiable",
    )
    p_unlock.add_argument("task_id", help="Task ID whose lock should be reclaimed")
    p_unlock.add_argument("--json", action="store_true", help="Output JSON result")

    register_exploration_subparsers(subparsers, parents=[common_parser])
    register_factory_subparsers(subparsers, parents=[common_parser])
    register_synthesis_subparsers(subparsers, parents=[common_parser])

    return parser


def register_exploration_subparsers(subparsers: Any, parents: Optional[List[Any]] = None) -> None:
    kwargs = {"parents": parents} if parents else {}
    # explore
    p_exp = subparsers.add_parser("explore", help="Run governed HowlDream exploration", **kwargs)
    p_exp.add_argument("objective", help="Exploration objective")
    p_exp.add_argument(
        "--policy",
        choices=["NEVER", "MANUAL", "SUGGEST", "ALLOWED", "AUTOMATIC_WITH_BUDGET"],
        default="MANUAL",
        help="Exploration governance policy",
    )
    p_exp.add_argument("--budget-candidates", type=int, default=5, help="Candidate limit")
    p_exp.add_argument("--budget-trials", type=int, default=3, help="Trial limit")
    p_exp.add_argument("--budget-tokens", type=int, default=10000, help="Token budget")
    p_exp.add_argument("--repo-dir", default=".", help="Target repository directory")
    p_exp.add_argument("--json", action="store_true", help="Output JSON result")

    # trace
    p_trc = subparsers.add_parser("trace", help="Trace lineage DAG for a candidate or node", **kwargs)
    p_trc.add_argument("trace_id", help="Node, candidate, or dream ID to trace")
    p_trc.add_argument("--repo-dir", default=".", help="Target repository directory")
    p_trc.add_argument("--json", action="store_true", help="Output JSON result")


def _positive_int(value: str) -> int:
    try:
        val = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if val <= 0:
        raise argparse.ArgumentTypeError(f"{value} must be greater than 0")
    return val


def register_factory_subparsers(subparsers: Any, parents: Optional[List[Any]] = None) -> None:
    kwargs = {"parents": parents} if parents else {}
    p_factory = subparsers.add_parser(
        "factory", help="Persistent factory supervisor", **kwargs,
        formatter_class=_grouped_help_formatter(_FACTORY_HELP_GROUPS, "Advanced and debugging"),
        epilog="Normal workflow: start -> status -> logs -> stop.",
    )
    p_factory._positionals.title = "Actions"
    factory_sub = p_factory.add_subparsers(dest="factory_action", required=True, metavar="<action>")

    p_run_once = factory_sub.add_parser(
        "run-once", help="Execute a single factory supervisor tick", **kwargs
    )
    p_run_once.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_run_once.add_argument("--target-repo", help="Repository to discover work from (advanced)")
    p_run_once.add_argument("--product-repo", help="Product repository slug to weight portfolio shares toward")
    p_run_once.add_argument(
        "--target",
        choices=["repo", "self", "ecosystem"],
        default="repo",
        help="What the factory is improving: repo (default), self, or ecosystem.",
    )
    p_run_once.add_argument("--authority", choices=["safe", "standard", "autonomous"],
                            help="Named first-run authority choice")
    p_run_once.add_argument(
        "--objective",
        default=None,
        help="Persistent campaign objective. Becomes durable supervisor state.",
    )
    p_run_once.add_argument(
        "--workspace",
        default=None,
        help="Workspace YAML for ecosystem mode.",
    )
    p_run_once.add_argument(
        "--authority-profile",
        choices=["strict", "overnight-safe", "howlframe-overnight"],
        default=None,
        help="Bind delegated campaign authority for autonomous git/GitHub actions. "
             "Without this, authority-required work is parked rather than executed.",
    )
    p_run_once.add_argument("--json", action="store_true", help="Output JSON result")
    _add_workspace_trust_argument(p_run_once)

    p_run = factory_sub.add_parser(
        "run", help="Run the factory supervisor loop until stopped", **kwargs
    )
    p_run.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_run.add_argument("--target-repo", help="Repository to discover work from (advanced)")
    p_run.add_argument("--product-repo", help="Product repository slug to weight portfolio shares toward")
    p_run.add_argument(
        "--target",
        choices=["repo", "self", "ecosystem"],
        default="repo",
        help="What the factory is improving: repo (default), self, or ecosystem.",
    )
    p_run.add_argument(
        "--objective",
        default=None,
        help="Persistent campaign objective. Becomes durable supervisor state.",
    )
    p_run.add_argument(
        "--workspace",
        default=None,
        help="Workspace YAML for ecosystem mode.",
    )
    p_run.add_argument("--until", type=float, help="Run for at most N seconds")
    p_run.add_argument("--max-work-items", type=int, default=None,
                       help="Stop after dispatching this many distinct work items (bounded run). "
                            "Without this, the Factory runs continuously.")
    p_run.add_argument("--resume-stopped", action="store_true",
                       help="Resume saved stopped state after acquiring the supervisor lock")
    p_run.add_argument(
        "--authority",
        choices=["safe", "standard", "autonomous"],
        default=None,
        help="Campaign authority preset",
    )
    p_run.add_argument(
        "--authority-profile",
        choices=["strict", "overnight-safe", "howlframe-overnight"],
        default=None,
        help="Bind delegated campaign authority for autonomous git/GitHub actions. "
             "Without this, authority-required work is parked rather than executed.",
    )
    p_run.add_argument("--json", action="store_true", help="Output JSON result")
    _add_preflight_argument(p_run)
    _add_workspace_trust_argument(p_run)

    p_status = factory_sub.add_parser(
        "status", help="Show factory supervisor state", **kwargs
    )
    p_status.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_status.add_argument("--target-repo", help="Repository to resolve (advanced)")
    p_status.add_argument("--json", action="store_true", help="Output JSON result")
    p_status.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show full supervisor details (IDs, ticks, history counts)",
    )
    p_status.add_argument(
        "--publish",
        action="store_true",
        help="Write a redacted status snapshot to factory/status/remote-snapshot.json. "
             "Does not start a campaign.",
    )
    p_status.add_argument(
        "--publish-path",
        help="Override the redacted snapshot path used by --publish",
    )
    p_status.add_argument(
        "--arm-periodic",
        action="store_true",
        help="Also record the snapshot path so a restarted supervisor refreshes it "
             "each tick. Does not start a campaign.",
    )

    p_snapshot = factory_sub.add_parser(
        "snapshot", help="Read a published remote status snapshot (freshness, tip SHA)", **kwargs
    )
    p_snapshot.add_argument("--path", help="Snapshot file (default: factory/status/remote-snapshot.json)")
    p_snapshot.add_argument("--fresh-seconds", type=int, default=None,
                            help="Snapshots older than this are STALE (default 1800)")
    p_snapshot.add_argument("--json", action="store_true", help="Output JSON result")

    p_pending = factory_sub.add_parser(
        "pending", help="List ranked Pending backlog rows, or validate one (read-only)", **kwargs
    )
    p_pending.add_argument("--validate", metavar="ROW_ID",
                           help="Emit validation evidence for one row id. Does not admit or claim it.")
    p_pending.add_argument("--recorded-by", metavar="NAME",
                           help="Who ran the validation; recorded in the evidence")
    p_pending.add_argument("--json", action="store_true", help="Output JSON result")

    p_stop = factory_sub.add_parser(
        "stop", help="Stop the factory supervisor loop", **kwargs
    )
    _add_stop_arguments(p_stop)

    p_resume = factory_sub.add_parser(
        "resume", help="Resume a stopped factory supervisor", **kwargs
    )
    p_resume.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_resume.add_argument("--target-repo", help="Repository to resolve (advanced)")
    p_resume.add_argument("--json", action="store_true", help="Output JSON result")

    p_start = factory_sub.add_parser("start", help="Start a persistent Factory campaign for this repository", **kwargs)
    _add_start_arguments(p_start)

    p_logs = factory_sub.add_parser(
        "logs", help="Show recent Factory logs for this repository",
        epilog="examples:\n  howlplane factory logs --errors --since 1h\n  howlplane factory logs --work-item WI-042\n"
               "  howlplane factory logs --follow",
        formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs)
    _add_logs_arguments(p_logs)

    p_factory_doctor = factory_sub.add_parser("doctor", help="Check whether Factory can start safely", **kwargs)
    p_factory_doctor.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_factory_doctor.add_argument("--target-repo", help="Repository to resolve (advanced)")
    _add_readiness_arguments(p_factory_doctor)
    _add_workspace_trust_argument(p_factory_doctor)

    p_bootstrap = factory_sub.add_parser(
        "bootstrap", help="Bootstrap a local repository from an owner-accepted proposal (fingerprint-bound)", **kwargs
    )
    p_bootstrap.add_argument("--proposal", required=True, help="Accepted repository proposal id")
    p_bootstrap.add_argument("--state-dir", help="Factory state directory")
    p_bootstrap.add_argument("--target-root", help="Directory that will hold the new repository "
                                                   "(default: <state-dir>/bootstrapped_repositories)")
    p_bootstrap.add_argument("--retry", action="store_true", help="Retry a bootstrap that previously failed")
    p_bootstrap.add_argument("--ledger-file", help="Ledger file path")
    p_bootstrap.add_argument("--json", action="store_true", help="Output JSON result")

    p_prepare = factory_sub.add_parser(
        "prepare", help="Authorize a repository for unattended Factory use and prepare agent workspace trust", **kwargs
    )
    p_prepare.add_argument("--agent", action="append", choices=list(agent_readiness.AGENT_ORDER),
                           help="Limit preparation to one agent (repeatable)")
    p_prepare.add_argument("--yes", action="store_true", help="Confirm the printed authorization scope non-interactively")
    p_prepare.add_argument("--revoke", action="store_true",
                           help="Remove HowlPlane's authorization for this repository (vendor trust is left untouched)")
    _add_readiness_arguments(p_prepare)
    _add_workspace_trust_argument(p_prepare)

    p_canary = factory_sub.add_parser(
        "canary", help="Run a single-item bounded canary (sugar for 'run --max-work-items 1')",
        **kwargs,
    )
    p_canary.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_canary.add_argument("--target-repo", help="Repository to discover work from (advanced)")
    p_canary.add_argument("--product-repo", help="Product repository slug to weight portfolio shares toward")
    p_canary.add_argument(
        "--target",
        choices=["repo", "self", "ecosystem"],
        default="repo",
        help="What the factory is improving.",
    )
    p_canary.add_argument("--objective", default=None, help="Campaign objective.")
    p_canary.add_argument("--workspace", default=None, help="Workspace YAML for ecosystem mode.")
    p_canary.add_argument("--until", type=float, help="Run for at most N seconds")
    p_canary.add_argument("--resume-stopped", action="store_true",
                         help="Resume saved stopped state after acquiring the supervisor lock")
    p_canary.add_argument(
        "--authority",
        choices=["safe", "standard", "autonomous"],
        default="safe",
        help="Campaign authority preset (default: safe)",
    )
    p_canary.add_argument(
        "--authority-profile",
        choices=["strict", "overnight-safe", "howlframe-overnight"],
        default=None,
        help="Bind delegated campaign authority for autonomous git/GitHub actions.",
    )
    p_canary.add_argument("--json", action="store_true", help="Output JSON result")
    _add_workspace_trust_argument(p_canary)

    p_queue = factory_sub.add_parser(
        "queue", help="Run finite factory over an explicit task queue", **kwargs
    )
    p_queue.add_argument("queue_file", help="Path to task queue JSON file")
    p_queue.add_argument("--dry-run", action="store_true", help="Report planned execution without launching")
    p_queue.add_argument("--ledger", help="Explicit path to factory queue ledger file")
    p_queue.add_argument("--retry", action="append", default=[], help="Retry a previously failed or interrupted task ID (repeatable)")
    p_queue.add_argument("--max-tasks", type=_positive_int, default=None, help="Stop after dispatching this many tasks")
    p_queue.add_argument("--stop-on-failure", action="store_true", help="Stop after any task failure")
    p_queue.add_argument("--quiet", action="store_true", help="Suppress progress output")
    p_queue.add_argument("--no-progress", action="store_true", help="Disable live progress display")
    p_queue.add_argument("--heartbeat", type=float, default=30.0, help="Heartbeat interval in seconds")
    p_queue.add_argument("--json", action="store_true", help="Output JSON result")


def register_synthesis_subparsers(subparsers: Any, parents: Optional[List[Any]] = None) -> None:
    kwargs = {"parents": parents} if parents else {}
    # create (Prompt-to-Product Synthesis)
    p_create = subparsers.add_parser("create", help="Create a runnable software product from natural language", **kwargs)
    p_create.add_argument("prompt", help="Natural language description of desired software outcome")
    p_create.add_argument("--output-dir", "-o", help="Target output product directory")
    p_create.add_argument("--avoid-provider", help="Avoid using specified provider if alternatives exist")
    p_create.add_argument("--agent", help="Preferred agent override")
    p_create.add_argument("--port", type=int, default=8088, help="Default HTTP server port")
    p_create.add_argument("--ledger-file", help="Ledger file path")
    p_create.add_argument("--json", action="store_true", help="Output JSON result")

    # run (Run verified product bundle)
    p_run = subparsers.add_parser("run", help="Run a verified product bundle", **kwargs)
    p_run.add_argument("product_dir", help="Path to product bundle directory")
    p_run.add_argument("--port", type=int, help="Override HTTP server port")

    # dogfood (Marathon dogfooding loop)
    p_dogfood = subparsers.add_parser("dogfood", help="Run automated marathon dogfooding benchmarks", **kwargs)
    p_dogfood.add_argument("--benchmarks", help="Comma-separated list of benchmarks to run (notes,todo,status_api,inventory,json_transform)")
    p_dogfood.add_argument("--max-iterations", type=int, default=5, help="Maximum benchmark iterations")
    p_dogfood.add_argument("--until-providers-exhausted", action="store_true", help="Run until all external providers are exhausted")
    p_dogfood.add_argument("--avoid-provider", help="Avoid using specified provider if alternatives exist")
    p_dogfood.add_argument("--resume", help="Resume an existing dogfood campaign by campaign ID, preserving its persisted benchmark scope")
    p_dogfood.add_argument(
        "--status", metavar="CAMPAIGN_ID",
        help="Read-only: report a campaign's durable state and exit. Never invokes a provider, "
             "never synthesizes products, never changes campaign scope, consumes zero quota.",
    )
    p_dogfood.add_argument("--campaign-dir", default=".dogfood_runs", help="Base directory for durable campaign state")
    p_dogfood.add_argument("--output-dir", default="output", help="Base output directory for generated products")
    p_dogfood.add_argument("--ledger-file", help="Ledger file path")
    p_dogfood.add_argument("--json", action="store_true", help="Output JSON result")
    p_dogfood.add_argument(
        "--authority-profile", choices=["strict", "overnight-safe"], default=None,
        help=(
            "Binds delegated campaign authority for real git/GitHub integration (#59). "
            "Required for any autonomous merge; without it, the campaign proposes and "
            "implements but parks anything requiring authority. Selecting a profile is "
            "explicit operator authorization for exactly the actions it encodes -- the "
            "campaign cannot select or expand its own profile. On --resume, an unexpired "
            "existing envelope is reused automatically; passing this flag again on resume "
            "creates a fresh explicit reauthorization (required once the prior one expires)."
        ),
    )
    p_dogfood.add_argument("--target-repo", default=".", help="Repository root for real git/GitHub integration")
    p_dogfood.add_argument("--repo-slug", help="owner/repo for GitHub integration (auto-detected from origin remote if omitted)")

    # acceptance (live governed-integration acceptance canary, #59.2 Phases 15-18)
    p_accept = subparsers.add_parser("acceptance", help="Live autonomous acceptance canary", **kwargs)
    accept_sub = p_accept.add_subparsers(dest="acceptance_action", required=True)
    p_overnight = accept_sub.add_parser(
        "overnight-integration",
        help="Run the one-shot live governed branch/commit/PR/CI/merge lifecycle canary",
    )
    p_overnight.add_argument(
        "--authority-profile", choices=["strict", "overnight-safe"], required=True,
        help="Delegated campaign authority the canary runs under. Required -- there is no "
             "default; an operator must explicitly authorize exactly the actions it encodes.",
    )
    p_overnight.add_argument("--target-repo", default=".", help="Repository root for real git/GitHub integration")
    p_overnight.add_argument("--repo-slug", help="owner/repo (auto-detected from origin remote if omitted)")
    p_overnight.add_argument("--campaign-dir", default=".dogfood_runs", help="Base directory for durable campaign state")
    p_overnight.add_argument("--ledger-file", help="Ledger file path")
    p_overnight.add_argument("--json", action="store_true", help="Output JSON result")

    # marathon (bounded backlog-driven engineering marathon)
    p_marathon = subparsers.add_parser(
        "marathon", help="Work a bounded sequence of a repository's ranked backlog", **kwargs
    )
    p_marathon.add_argument(
        "--authority-profile", required=True,
        choices=["strict", "overnight-safe", "howlframe-overnight"],
        help="Delegated campaign authority. Required -- there is no default; an "
             "operator must explicitly authorize exactly the actions it encodes.",
    )
    p_marathon.add_argument("--target-repo", default=".", help="Repository whose backlog is worked")
    p_marathon.add_argument("--repo-slug", help="owner/repo (auto-detected from origin remote if omitted)")
    p_marathon.add_argument("--max-tasks", type=int, default=3, help="Maximum backlog items to attempt")
    p_marathon.add_argument("--max-runtime-hours", type=float, default=8.0, help="Wall-clock ceiling")
    p_marathon.add_argument("--roi-floor", type=float, default=0.5, help="Minimum backlog score to work")
    p_marathon.add_argument("--campaign-dir", default=".dogfood_runs", help="Durable campaign state directory")
    p_marathon.add_argument("--resume", help="Resume an existing campaign by id")
    p_marathon.add_argument("--ledger-file", help="Ledger file path")
    p_marathon.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be worked and stop. Invokes no provider and writes nothing.",
    )
    p_marathon.add_argument("--json", action="store_true", help="Output JSON result")

    # authority (read-only authority profile inspection, #59 Phase 26)
    #
    # The choices come from the canonical registry rather than a second
    # hard-coded list. The duplicate went stale the moment a third profile was
    # added: `howlframe-overnight` was a valid marathon authority profile that
    # could not be inspected, which is the wrong way round -- inspection is
    # read-only and is exactly what an operator does *before* granting it.
    # This does not widen any grant; `--authority-profile` choices on dogfood
    # and acceptance are deliberately left alone.
    from howlplane.control_plane.authority_profile import CANONICAL_PROFILES

    p_authority = subparsers.add_parser("authority", help="Inspect delegated authority profiles (read-only)", **kwargs)
    authority_sub = p_authority.add_subparsers(dest="authority_action", required=True)
    p_authority_show = authority_sub.add_parser("show", help="Show a canonical authority profile's exact permissions")
    p_authority_show.add_argument("profile_id", choices=sorted(CANONICAL_PROFILES))
    p_authority_show.add_argument("--json", action="store_true", help="Output JSON result")

    p_setup = subparsers.add_parser(
        "setup", help="Check this repository and prepare it for Factory use", **kwargs)
    p_setup.add_argument("--yes", action="store_true",
                         help="Confirm the printed preparation scope non-interactively")
    p_setup.add_argument("--json", action="store_true", help="Output JSON result (never prompts)")
    _add_workspace_trust_argument(p_setup)

    p_config = subparsers.add_parser(
        "config", help="Show, validate and explain effective configuration", **kwargs)
    config_sub = p_config.add_subparsers(dest="config_action", required=True, metavar="<action>")
    for name, text in (("show", "Show effective settings and where each value came from"),
                       ("validate", "Check that the configuration is valid"),
                       ("explain", "Explain one setting: value, source, default and type")):
        p_cfg = config_sub.add_parser(name, help=text)
        if name == "explain":
            p_cfg.add_argument("key", help="Dotted setting name, for example server.port")
        p_cfg.add_argument("--json", action="store_true", help="Output JSON result")

    p_tia = subparsers.add_parser(
        "tia", help="Record and check the Test Impact Assessment required before a code change ships", **kwargs)
    tia_sub = p_tia.add_subparsers(dest="tia_action", required=True, metavar="<action>")
    p_tia_record = tia_sub.add_parser("record", help="Validate a Test Impact Assessment and append it to the evidence ledger")
    p_tia_record.add_argument("--task-id", required=True)
    p_tia_record.add_argument("--file", required=True, help="JSON file ('-' for stdin) matching schemas/test-impact-assessment.schema.json")
    p_tia_record.add_argument("--agent-id", default="agent")
    p_tia_check = tia_sub.add_parser("check", help="Fail unless the task has a valid recorded Test Impact Assessment")
    p_tia_check.add_argument("task_id")
    p_tia_check.add_argument("--no-code-change", action="store_true",
                             help="Declare that the task changed no code or tests (nothing to assess)")
    for p_t in (p_tia_record, p_tia_check):
        p_t.add_argument("--ledger-file", help="Ledger file path")
        p_t.add_argument("--json", action="store_true", help="Output JSON result")

    # local (local Ollama model setup/health check, #58 Phase 3)
    p_local = subparsers.add_parser("local", help="Local (Ollama) model utilities", **kwargs)
    p_local.add_argument("local_action", choices=["setup"], help="Action to perform")
    p_local.add_argument("--model", default="qwen2.5-coder:7b-instruct", help="Model to pull/verify")
    p_local.add_argument("--json", action="store_true", help="Output JSON result")


def cmd_marathon(args: argparse.Namespace) -> int:
    """Works a bounded sequence of the target repository's ranked backlog."""
    import json as _json
    import sys as _sys
    from howlplane.control_plane.backlog_source import BacklogSource
    from howlplane.control_plane.synthesis import MarathonDogfoodEngine
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

    target_repo = getattr(args, "target_repo", None) or "."

    if getattr(args, "dry_run", False):
        # Read-only: shows exactly which items are eligible and why the
        # near-misses are not, without binding an authority envelope or
        # invoking any provider.
        selection = BacklogSource(
            target_repo, roi_floor=getattr(args, "roi_floor", 0.5)
        ).select()
        payload = {
            "target_repo": target_repo,
            "files_read": selection.files_read,
            "eligible": [item.to_dict() for item in selection.eligible],
            "excluded": selection.excluded,
            "would_work": [
                item.task_id for item in selection.eligible[: args.max_tasks]
            ],
        }
        if getattr(args, "json", False):
            print(_json.dumps(payload, indent=2))
        else:
            print("=" * 60)
            print(f"HOWLPLANE — MARATHON DRY RUN: {target_repo}")
            print("=" * 60)
            print(f"Backlogs read:      {', '.join(selection.files_read) or 'none'}")
            print(f"Eligible items:     {len(selection.eligible)}")
            print(f"Would work (max {args.max_tasks}):")
            for item in selection.eligible[: args.max_tasks]:
                print(f"  - {item.task_id}  score={item.score}  {item.title}")
            if selection.excluded:
                print("\nPending but NOT eligible:")
                for excluded in selection.excluded:
                    print(f"  - #{excluded['item_id']} ({excluded['source_file']}): {excluded['reason']}")
            print("\nNo provider was invoked and nothing was written.")
        return 0

    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    engine = MarathonDogfoodEngine(
        provider_pool=ProviderPoolManager.from_config(),
        ledger=ledger,
        campaign_dir=getattr(args, "campaign_dir", None),
        target_repo=target_repo,
        repo_slug=getattr(args, "repo_slug", None),
    )
    report = engine.run_backlog_marathon(
        authority_profile_id=args.authority_profile,
        max_tasks=args.max_tasks,
        max_runtime_hours=args.max_runtime_hours,
        resume_campaign_id=getattr(args, "resume", None),
        roi_floor=getattr(args, "roi_floor", 0.5),
    )

    if report.get("refused"):
        # Fail-closed preflight: the campaign never started. Reported on stderr
        # with a non-zero exit so an unattended wrapper cannot read it as a run
        # that simply found nothing to do.
        if getattr(args, "json", False):
            print(_json.dumps(report, indent=2))
        else:
            print("=" * 60, file=_sys.stderr)
            print("HOWLPLANE — MARATHON REFUSED", file=_sys.stderr)
            print("=" * 60, file=_sys.stderr)
            print(report["refusal_reason"], file=_sys.stderr)
            print("\nWorking tree changes found:", file=_sys.stderr)
            for path in report["dirty_files"]:
                print(f"  {path}", file=_sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(_json.dumps(report, indent=2))
    else:
        print("=" * 60)
        print(f"HOWLPLANE — BACKLOG MARATHON: {report['campaign_id']}")
        print("=" * 60)
        print(f"Repository:     {report['target_repo']} ({report['repo_slug'] or 'no slug'})")
        print(f"Authority:      {report['authority_profile']}")
        print(f"Stop reason:    {report['stop_reason']}")
        print(f"Attempted:      {', '.join(report['tasks_attempted']) or 'none'}")
        print(f"Completed:      {', '.join(report['tasks_completed']) or 'none'}")
        print(f"Failed:         {', '.join(report['tasks_failed']) or 'none'}")
        print(f"Parked:         {', '.join(report['tasks_parked']) or 'none'}")
        print(f"Duration:       {report['duration_seconds']}s")
        print(f"State:          {report['state_dir']}")
    # A parked task is not a failure: it is the control plane correctly
    # refusing to exceed its delegated authority, and the operator decides.
    return 0 if not report["tasks_failed"] else 1
































def _add_readiness_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--live", action="store_true",
                        help="Also send one tiny non-mutating prompt per eligible agent (consumes a little quota)")
    parser.add_argument("--json", action="store_true", help="Output JSON result")


def _add_workspace_trust_argument(parser: argparse.ArgumentParser) -> None:
    from howlplane.control_plane.orchestration import add_workspace_trust_argument
    add_workspace_trust_argument(parser)


def _add_start_arguments(p_start: argparse.ArgumentParser, preflight: bool = True) -> None:
    """Arguments shared by `howlplane start` and `howlplane factory start`."""
    p_start.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_start.add_argument("--target-repo", help="Factory target worktree (advanced)")
    if preflight:
        _add_preflight_argument(p_start)
    p_start.add_argument("--product-repo", help="Product repository slug to weight portfolio shares toward")
    p_start.add_argument("--target", choices=["repo", "self", "ecosystem"], default="repo")
    p_start.add_argument("--workspace", help="Workspace YAML for ecosystem mode")
    p_start.add_argument("--objective", help="Durable campaign objective")
    p_start.add_argument("--max-work-items", type=int, default=None,
                         help="Stop after dispatching this many distinct work items.")
    p_start.add_argument("--authority", choices=["safe", "standard", "autonomous"],
                         help="Named first-run authority choice")
    p_start.add_argument("--authority-profile", choices=["strict", "overnight-safe", "howlframe-overnight"],
                         help="Existing authority profile id (advanced)")
    p_start.add_argument("--json", action="store_true", help="Output JSON result")
    p_start.add_argument("--verbose", action="store_true", help="Also show backend and worktree path")
    _add_workspace_trust_argument(p_start)


def _add_logs_arguments(p_logs: argparse.ArgumentParser) -> None:
    """Arguments shared by `howlplane logs` and `howlplane factory logs`."""
    p_logs.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_logs.add_argument("--target-repo", help="Repository to resolve (advanced)")
    p_logs.add_argument("--follow", action="store_true", help="Stream new log output")
    p_logs.add_argument("--lines", "-n", type=int, default=80, help="Number of recent entries")
    p_logs.add_argument("--errors", action="store_true", help="Only errors (same as --level ERROR)")
    p_logs.add_argument("--level", type=str.upper, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Show this severity and above")
    p_logs.add_argument("--work-item", help="Only entries for this work item, for example WI-042")
    p_logs.add_argument("--provider", help="Only entries for this provider, for example codex")
    p_logs.add_argument("--since", help="Only entries newer than 30m, 1h, 2d or an ISO timestamp")
    p_logs.add_argument("--raw", action="store_true", help="Show raw process output instead of events")
    p_logs.add_argument("--json", action="store_true", help="One JSON object per line")
    p_logs.add_argument("--verbose", action="store_true", help="Include event codes and correlation ids")


def _add_stop_arguments(p_stop: argparse.ArgumentParser) -> None:
    p_stop.add_argument("--state-dir", help="Factory state directory (advanced)")
    p_stop.add_argument("--target-repo", help="Repository to resolve (advanced)")
    p_stop.add_argument("--json", action="store_true", help="Output JSON result")


def _add_preflight_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preflight", choices=["off", "warn", "require"], default="off",
        help="Check agent readiness before starting: warn, or refuse when no autonomous worker is usable",
    )










def cmd_create(args: argparse.Namespace) -> int:
    """Creates a runnable software product from natural language intent."""
    import re
    from howlplane.control_plane.synthesis import (
        NaturalLanguageSynthesizer,
        ProductSynthesizer,
    )
    prompt = args.prompt
    out_dir = args.output_dir or f"output/{re.sub(r'[^a-z0-9_-]', '-', prompt[:30].lower()).strip('-') or 'app'}"
    avoid = getattr(args, "avoid_provider", None)
    agent = getattr(args, "agent", None)
    port = getattr(args, "port", 8088)
    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None

    is_json = getattr(args, "json", False)
    if not is_json:
        print("=" * 60)
        print("HOWLPLANE PROMPT-TO-PRODUCT SYNTHESIS")
        print("=" * 60)
        print("Understanding product...")
    synthesizer = NaturalLanguageSynthesizer()
    spec = synthesizer.synthesize(prompt)

    if not is_json:
        print("")
        print("Product:")
        print(f"  {spec.title}")
        print("")
        print("Features:")
        if "browser_ui" in spec.interfaces:
            print("  - Browser UI")
        if "http_api" in spec.interfaces:
            print("  - JSON HTTP API")
        print("  - CRUD Operations")
        if spec.persistence.type == "local_store":
            print(f"  - Persistent Storage ({spec.persistence.storage_path})")
        print("")
        print("Synthesizing...")

    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

    engine = ProductSynthesizer(
        provider_pool=ProviderPoolManager.from_config(), ledger=ledger
    )
    res = engine.create_from_prompt(
        prompt=prompt,
        output_dir=out_dir,
        avoid_provider=avoid,
        preferred_agent=agent,
        port=port,
    )

    if getattr(args, "json", False):
        import json
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.success else 1

    if res.success and res.product_bundle and res.acceptance_report:
        print("Checking...")
        if res.repair_cycles > 0:
            print(f"Repairing ({res.repair_cycles} cycles)...")
        print("")
        print("Verification:")
        print(f"  {res.acceptance_report.passed_count}/{res.acceptance_report.total_count} acceptance checks passed")
        print("")
        print("PRODUCT READY")
        print(f"  Bundle: {res.product_bundle.directory}")
        print("")
        print("Run:")
        print(f"  howlplane run {res.product_bundle.directory}")
        print("=" * 60)
        return 0
    else:
        print("")
        print(f"SYNTHESIS FAILED: {res.status}")
        if res.error_message:
            print(f"Error: {res.error_message}")
        if res.framework_gaps:
            print("Framework Gaps:")
            for g in res.framework_gaps:
                print(f"  - {g.code}: {g.required_behavior} ({g.current_support})")
        print("=" * 60)
        return 1


def cmd_run_product(args: argparse.Namespace) -> int:
    """Executes a verified product bundle."""
    import subprocess
    product_dir = Path(args.product_dir).resolve()
    port = getattr(args, "port", None)
    run_script = product_dir / "scripts" / "run.sh"
    if not run_script.exists():
        print(f"ERROR: scripts/run.sh not found in {product_dir}", file=sys.stderr)
        return 1

    cmd = ["bash", str(run_script)]
    if port:
        cmd.append(str(port))

    print(f"Launching verified product in {product_dir}...")
    try:
        res = subprocess.run(cmd, cwd=str(product_dir))
        return res.returncode
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0


def cmd_dogfood_status(args: argparse.Namespace) -> int:
    """
    Read-only dogfood campaign inspection (#58 Phase 1). Loads durable campaign
    state directly from disk and reports it. Never constructs a provider pool,
    never probes agent binaries/services, never invokes a provider, never
    synthesizes a product, and never mutates the persisted campaign scope.
    """
    from howlplane.control_plane.synthesis.campaign_state import DurableCampaignState

    campaign_id = args.status
    campaign_dir = Path(getattr(args, "campaign_dir", ".dogfood_runs") or ".dogfood_runs") / campaign_id
    try:
        state = DurableCampaignState.load(campaign_dir)
    except FileNotFoundError:
        print(f"No durable campaign state found for '{campaign_id}' at {campaign_dir}.")
        return 1

    if getattr(args, "json", False):
        import json
        print(json.dumps(state.to_dict(), indent=2))
    else:
        print(state.render_markdown())
        pending = [
            b for b in state.requested_benchmarks
            if b not in {h.get("benchmark_id") for h in state.benchmark_history if h.get("success")}
        ]
        print("\n## Status Inspection (read-only; no provider invoked)\n")
        print(f"- Pending benchmarks in scope: {', '.join(pending) or 'none'}")
        print(f"- Next action: {state.next_action}")
    return 0


def cmd_dogfood(args: argparse.Namespace) -> int:
    """Executes marathon dogfooding loop across product benchmarks."""
    if getattr(args, "status", None):
        return cmd_dogfood_status(args)

    from howlplane.control_plane.synthesis import MarathonDogfoodEngine
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
    benchmarks = [b.strip() for b in args.benchmarks.split(",")] if getattr(args, "benchmarks", None) else None
    max_iters = getattr(args, "max_iterations", 5)
    avoid = getattr(args, "avoid_provider", None)
    out_base = getattr(args, "output_dir", "output")
    campaign_dir = getattr(args, "campaign_dir", None)
    resume_id = getattr(args, "resume", None)
    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None

    engine = MarathonDogfoodEngine(
        provider_pool=ProviderPoolManager.from_config(),
        base_output_dir=out_base, ledger=ledger, campaign_dir=campaign_dir,
        target_repo=getattr(args, "target_repo", None) or ".",
        repo_slug=getattr(args, "repo_slug", None),
    )
    report = engine.run_marathon(
        benchmarks=benchmarks,
        max_iterations=max_iters,
        until_providers_exhausted=getattr(args, "until_providers_exhausted", False),
        avoid_provider=avoid,
        resume_campaign_id=resume_id,
        authority_profile_id=getattr(args, "authority_profile", None),
    )

    if getattr(args, "json", False):
        import json
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render_markdown())

    return 0 if report.iterations_failed == 0 else 1


def cmd_acceptance(args: argparse.Namespace) -> int:
    """
    Live governed-integration acceptance canary (#59.2 Phases 15-18).
    `ai acceptance overnight-integration` runs exactly one bounded governed
    engineering task -- through the real GovernedTaskOrchestrator, real
    independent review, real deterministic verification, and real
    GitIntegrationExecutor branch/commit/push/PR/CI/merge/remote-verify/
    local-sync lifecycle -- to prove that lifecycle end-to-end with no
    mocked git/gh boundary. The task is mechanically scoped to touch only
    its designated evidence artifact under documentation/task_journals/.
    """
    if getattr(args, "acceptance_action", None) != "overnight-integration":
        print("Usage: howlplane acceptance overnight-integration --authority-profile <strict|overnight-safe>")
        return 1

    from howlplane.control_plane.synthesis import MarathonDogfoodEngine
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    engine = MarathonDogfoodEngine(
        provider_pool=ProviderPoolManager.from_config(),
        campaign_dir=getattr(args, "campaign_dir", None),
        target_repo=getattr(args, "target_repo", None) or ".",
        repo_slug=getattr(args, "repo_slug", None),
        ledger=ledger,
    )
    result = engine.run_acceptance_canary(authority_profile_id=args.authority_profile)
    git_rec = result.get("git_record") or {}
    fully_verified = bool(
        git_rec.get("merged") and git_rec.get("remote_main_contains_merge") and git_rec.get("local_main_synced")
    )

    if getattr(args, "json", False):
        import json
        print(json.dumps({**result, "fully_verified": fully_verified}, indent=2))
    else:
        print("=" * 60)
        print(f"HOWLPLANE — LIVE ACCEPTANCE CANARY: {result['campaign_id']}")
        print("=" * 60)
        print(f"Task ID:            {result['task_id']}")
        print(f"Journal path:       {result['journal_path']}")
        print(f"Task success:       {result['task_success']}")
        print(f"Branch:             {git_rec.get('branch')}")
        print(f"Commit SHA:         {git_rec.get('commit_sha')}")
        print(f"PR:                 #{git_rec.get('pr_number')} {git_rec.get('pr_url') or ''}")
        print(f"CI status:          {git_rec.get('ci_status')}")
        print(f"Merged:             {git_rec.get('merged')}")
        print(f"Merge SHA:          {git_rec.get('merge_sha')}")
        print(f"Remote verified:    {git_rec.get('remote_main_contains_merge')}")
        print(f"Local synced:       {git_rec.get('local_main_synced')}")
        if git_rec.get("failure_reason"):
            print(f"Failure reason:     {git_rec.get('failure_reason')}")
        print(f"Fully verified:     {fully_verified}")
        print("=" * 60)

    return 0 if fully_verified else 1


def cmd_authority(args: argparse.Namespace) -> int:
    """
    Read-only authority profile inspection (#59 Phase 26). Prints a
    canonical profile's exact permissions, TTL, budgets, and local-resource
    limits. Invokes no AI, constructs no provider pool or engine, and
    performs no writes -- mirrors the read-only contract of
    `ai dogfood --status`.
    """
    from howlplane.control_plane.authority_profile import get_profile

    if getattr(args, "authority_action", None) != "show":
        from howlplane.control_plane.authority_profile import CANONICAL_PROFILES
        print(f"Usage: howlplane authority show <{'|'.join(sorted(CANONICAL_PROFILES))}>")
        return 1

    profile = get_profile(args.profile_id)
    if getattr(args, "json", False):
        import json
        print(json.dumps(profile.to_dict(), indent=2))
        return 0

    print(f"# Authority Profile: `{profile.profile_id}` (v{profile.version})")
    print()
    print(f"- **TTL:** {profile.ttl_hours} hours")
    print(f"- **Max autonomous merges:** {profile.max_merges}")
    print(f"- **External spend budget:** ${profile.external_spend_usd_limit}")
    print(f"- **Authorized repositories:** {', '.join(profile.authorized_repositories) or 'none'}")
    print(f"- **Local RAM threshold:** {profile.local_ram_threshold_gib} GiB")
    print(f"- **Local keep_alive:** {profile.local_keep_alive}")
    print(f"- **Local-only iteration limit:** {profile.local_only_iteration_limit}")
    print()
    print("## Allowed action classes")
    for a in profile.allowed_action_classes or ["(none)"]:
        print(f"- {a}")
    print()
    print("## Denied action classes (never auto-authorized, envelope cannot override)")
    for a in profile.denied_action_classes or ["(none)"]:
        print(f"- {a}")
    return 0


def cmd_local(args: argparse.Namespace) -> int:
    """
    Local (Ollama) model utilities (#58 Phase 3). `ai local setup`:
      1. checks whether Ollama is installed
      2. installs it via the official installer if missing and the operator
         confirms (or if HOWLPLANE_LOCAL_AUTO_INSTALL=1 is set)
      3. pulls the canonical model
      4. verifies with `ollama list`
      5. performs a tiny real inference ("Respond only with LOCAL_OK")
      6. reports PASS/FAIL with duration and observed details

    Never pulls any model other than the one requested (default:
    qwen2.5-coder:7b-instruct), and never installs anything beyond the
    official Ollama runtime.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import time as _time

    from howlplane.control_plane.agent_execution import OllamaLocalBackend, diagnose_ollama
    from howlplane.control_plane.task_spec import TaskSpec

    model = getattr(args, "model", "qwen2.5-coder:7b-instruct")
    report: Dict[str, Any] = {"model": model, "steps": []}

    def _step(name: str, ok: bool, detail: str = "") -> None:
        report["steps"].append({"name": name, "ok": ok, "detail": detail})
        symbol = "OK" if ok else "FAIL"
        print(f"[{symbol}] {name}{': ' + detail if detail else ''}")

    if not _shutil.which("ollama"):
        auto_install = os.environ.get("HOWLPLANE_LOCAL_AUTO_INSTALL") == "1"
        if not auto_install:
            _step(
                "ollama_installed", False,
                "Ollama is not installed. Re-run with HOWLPLANE_LOCAL_AUTO_INSTALL=1 to "
                "install it via the official installer (curl -fsSL https://ollama.com/install.sh | sh), "
                "or install it yourself first.",
            )
            report["result"] = "FAIL"
            _print_local_report(args, report)
            return 1
        try:
            _subprocess.run(  # nosec B602 - fixed official install command, no shell interpolation of user input
                ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                check=True, timeout=600,
            )
            _step("ollama_installed", True, "Installed via official installer")
        except Exception as exc:
            _step("ollama_installed", False, f"Install failed: {exc}")
            report["result"] = "FAIL"
            _print_local_report(args, report)
            return 1
    else:
        _step("ollama_installed", True, "Already on PATH")

    diag = diagnose_ollama(model=model)
    if diag.reason == "OLLAMA_SERVICE_UNAVAILABLE":
        _step("ollama_service", False, diag.detail)
        report["result"] = "FAIL"
        _print_local_report(args, report)
        return 1
    _step("ollama_service", diag.reason != "OLLAMA_SERVICE_UNAVAILABLE", diag.detail)

    if diag.reason == "MODEL_NOT_INSTALLED":
        try:
            _subprocess.run(["ollama", "pull", model], check=True, timeout=3600)
            _step("model_pulled", True, f"Pulled {model}")
        except Exception as exc:
            _step("model_pulled", False, str(exc))
            report["result"] = "FAIL"
            _print_local_report(args, report)
            return 1
    else:
        _step("model_pulled", True, "Already installed")

    try:
        listing = _subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30)
        model_listed = model.split(":")[0] in listing.stdout
        _step("ollama_list", model_listed, listing.stdout.strip().splitlines()[-1] if listing.stdout.strip() else "")
    except Exception as exc:
        _step("ollama_list", False, str(exc))

    backend = OllamaLocalBackend(model=model)
    probe_task = TaskSpec(task_id="LOCAL-SETUP-PROBE", repository="howlplane", objective="local setup smoke test", risk_level="low", task_class="docs")
    t0 = _time.time()
    result = backend.execute(probe_task, cwd=".", prompt_override="Respond only with LOCAL_OK", timeout_seconds=120)
    elapsed = round(_time.time() - t0, 3)
    inference_ok = result.success and "LOCAL_OK" in result.stdout
    _step("inference_smoke_test", inference_ok, f"stdout={result.stdout.strip()!r} duration={elapsed}s")

    report["result"] = "PASS" if inference_ok else "FAIL"
    report["duration_seconds"] = elapsed
    _print_local_report(args, report)
    return 0 if inference_ok else 1


def _print_local_report(args: argparse.Namespace, report: Dict[str, Any]) -> None:
    if getattr(args, "json", False):
        import json
        print(json.dumps(report, indent=2))
    else:
        print(f"\nResult: {report.get('result')}")


def cmd_product_status(args: argparse.Namespace) -> int:
    from howlplane.control_plane import product_cli
    return product_cli.cmd_status(args)


def cmd_product_start(args: argparse.Namespace) -> int:
    from howlplane.control_plane import product_cli
    return product_cli.cmd_start(args)


def cmd_product_logs(args: argparse.Namespace) -> int:
    from howlplane.control_plane import product_cli
    return product_cli.cmd_logs(args)


def cmd_product_stop(args: argparse.Namespace) -> int:
    from howlplane.control_plane import product_cli
    return product_cli.cmd_stop(args)


# Every subcommand build_parser() registers must appear here; see the same note
HANDLERS = {
    "orchestrate": lambda args: __import__("howlplane.control_plane.orchestration", fromlist=["command"]).command(args),
    "work": cmd_work,
    "route": cmd_route,
    "providers": cmd_providers,
    "status": cmd_product_status,
    "start": cmd_product_start,
    "logs": cmd_product_logs,
    "stop": cmd_product_stop,
    "unlock": cmd_unlock,
    "init-task": cmd_init_task,
    "route-task": cmd_route_task,
    "briefs": cmd_briefs,
    "prepare-run": cmd_prepare_run,
    "reconcile": cmd_reconcile,
    "verify": cmd_verify,
    "record": cmd_record,
    "metrics": cmd_metrics,
    "report": cmd_metrics,
    "check-boundary": cmd_boundary,
    "doctor": cmd_doctor,
    "agents": cmd_agents,
    "howlframe-audit": cmd_howlframe_audit,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "resume": cmd_resume,
    "cancel": cmd_cancel,
    "create": cmd_create,
    "run": cmd_run_product,
    "dogfood": cmd_dogfood,
    "acceptance": cmd_acceptance,
    "marathon": cmd_marathon,
    "authority": cmd_authority,
    "tia": lambda args: __import__("howlplane.control_plane.tia_cli", fromlist=["cmd_tia"]).cmd_tia(args),
    "config": lambda args: __import__("howlplane.control_plane.config_cli", fromlist=["command"]).command(args),
    "setup": lambda args: __import__("howlplane.control_plane.setup_cli", fromlist=["command"]).command(args),
    "local": cmd_local,
    "factory": cmd_factory,
    "explore": cmd_explore,
    "trace": cmd_trace,
    "creative": lambda args: __import__("howlplane.control_plane.creative_pipeline", fromlist=["command"]).command(args),
}

ACTIONS = HANDLERS


def cmd_home(args: argparse.Namespace) -> int:
    from howlplane.control_plane import product_cli
    return product_cli.cmd_home(args)


def main(args: Optional[List[str]] = None, program_name: str = "howlplane") -> int:
    """Runs the canonical HowlPlane control plane launcher."""
    if args is None:
        args = sys.argv[1:]
    global _COLOR_MODE
    args, debug, _COLOR_MODE = _extract_global_flags(list(args))
    if _COLOR_MODE is not None and _COLOR_MODE not in ("auto", "always", "never"):
        print("--color must be one of: auto, always, never", file=sys.stderr)
        return 2
    parser = build_parser(program_name=program_name)
    parsed_args, unknown = parser.parse_known_args(args)
    if unknown:
        message = f"unrecognized arguments: {' '.join(unknown)}"
        if any(arg == "--verify" or arg.startswith("--verify=") for arg in args):
            # argparse reads a verification command's own flags as options (DOG-035).
            message += '; quote a verification command that has options, for example --verify "python3 -m pytest -q"'
        parser.error(message)

    if not parsed_args.subcommand:
        if program_name != "howlplane":
            # The deprecated `ai` entry point keeps its original behavior.
            parser.print_help()
            return 1
        handler = cmd_home
    else:
        handler = HANDLERS.get(parsed_args.subcommand)
    if not handler:
        parser.print_help()
        return 1

    try:
        # One resolution point: an explicit --workspace-trust overrides config for
        # this process and every agent, session, and service it launches.
        from howlplane.control_plane import workspace_trust
        workspace_trust.set_cli_policy(getattr(parsed_args, "workspace_trust", None))
        # Fail fast on an invalid environment or config value, before any work starts.
        # `config` explains invalid values itself, so it must be reachable when they are wrong.
        if parsed_args.subcommand != "config":
            workspace_trust.resolve_policy()
        return handler(parsed_args)
    except BrokenPipeError:
        # The reader went away (`howlplane logs | head`). That is not a HowlPlane
        # failure: stay quiet and keep the interpreter from re-raising on the
        # final stdout flush.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141
    except Exception as err:
        return _report_failure(err, parsed_args, debug)


def _report_failure(err: BaseException, parsed_args: argparse.Namespace, debug: bool) -> int:
    """The one canonical error boundary: howlplane.error/v1 for every failure."""
    from howlplane.control_plane.presentation import errors as presentation_errors
    from howlplane.control_plane.presentation.style import resolve_style
    as_json = getattr(parsed_args, "json", False)
    explained = None
    try:
        explained = presentation_errors.explain(err)
    except Exception:
        explained = None
    if explained is None:
        explained = presentation_errors.internal_error(err)
    if as_json:
        print(json.dumps(explained.to_dict()), file=sys.stderr)
    else:
        print(explained.render(resolve_style(sys.stderr, _COLOR_MODE)), file=sys.stderr)
    if debug:
        import traceback
        traceback.print_exception(type(err), err, err.__traceback__, file=sys.stderr)
    return 1


_COLOR_MODE: Optional[str] = None


def _extract_global_flags(args: List[str]) -> tuple:
    """Pull ``--debug`` and ``--color MODE`` out of argv wherever they appear."""
    debug = os.environ.get("HOWLPLANE_DEBUG") == "1"
    color = None
    rest: List[str] = []
    it = iter(args)
    for token in it:
        if token == "--":
            rest.append(token)
            rest.extend(it)
            break
        if token == "--debug":
            debug = True
        elif token == "--color":
            color = next(it, None)
        elif token.startswith("--color="):
            color = token.split("=", 1)[1]
        else:
            rest.append(token)
    return rest, debug, color


def legacy_main(args: Optional[List[str]] = None) -> int:
    """Runs the deprecated ai compatibility entry point without altering stdout."""
    print(
        "'ai' is deprecated. Use the Howl ecosystem CLI when available, or "
        "'howlplane' for direct HowlPlane access.",
        file=sys.stderr,
    )
    launcher_mod = sys.modules.get("howlplane.control_plane.launcher")
    if launcher_mod and hasattr(launcher_mod, "main") and launcher_mod.main is not main:
        return launcher_mod.main(args, program_name="ai")
    return main(args, program_name="ai")


if __name__ == "__main__":
    if os.environ.get("HOWLPLANE_LEGACY_AI") == "1":
        sys.exit(legacy_main())
    sys.exit(main())
