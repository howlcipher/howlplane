#!/usr/bin/env python3
"""
launcher.py

Thin global entrypoint into the Multi-Agent Engineering Control Plane.
Operates across any target Git repository, discovering project-local truth
while enforcing global control plane policies, deterministic routing,
independent reviewer selection, verification planning, and fail-closed
human authority boundaries.
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
from typing import List, Optional, Tuple, Union

_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from src.control_plane.cli import (
    cmd_doctor as cp_cmd_doctor,
    cmd_verify as cp_cmd_verify,
    cmd_howlframe_audit as cp_cmd_howlframe_audit,
    cmd_approve as cp_cmd_approve,
    cmd_reject as cp_cmd_reject,
    cmd_resume as cp_cmd_resume,
    cmd_cancel as cp_cmd_cancel,
    cmd_unlock as cp_cmd_unlock,
    cmd_create as cp_cmd_create,
    cmd_run_product as cp_cmd_run_product,
    cmd_dogfood as cp_cmd_dogfood,
    cmd_acceptance as cp_cmd_acceptance,
    cmd_authority as cp_cmd_authority,
    cmd_local as cp_cmd_local,
    register_synthesis_subparsers,
)
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.git_env import run_git_in_repo
from src.control_plane.locking import get_repo_lock_path, is_process_alive
from src.control_plane.recovery import CrashRecoveryEngine
from src.control_plane.howlframe_runner import (
    HowlFrameAuditRunner,
    find_howlframe_binary,
    get_howlframe_version,
    get_dogfood_mode,
    DEFAULT_INSTRUCTION_BUDGET,
)
from src.control_plane.human_boundary import (
    HumanBoundaryGate,
    HumanLifecycleManager,
    HumanDecisionRecord,
    compute_repository_fingerprint,
    check_repository_drift,
)
from src.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig, OrchestrationResult
from src.control_plane.project_adapter import ProjectAdapter, ProjectContext
from src.control_plane.reconciliation import ReconciliationResult
from src.control_plane.reviewers import get_reviewer_role
from src.control_plane.router import TaskRouter, RoutingDecision
from src.control_plane.atomic_io import safe_load_json
from src.control_plane.progress import format_elapsed, format_last_heartbeat
from src.control_plane.resource_cli import inventory_document, render_inventory, render_route
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from src.control_plane.task_spec import TaskSpec


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
        f"ERROR: no target Git repository found in '{target}'. 'ai' must be executed inside a Git repository."
    )


def find_control_plane_root(override_path: Optional[str] = None) -> Path:
    """Discovers the HowlPlane repository path using the 5-step precedence."""
    if override_path:
        p = Path(override_path).expanduser().resolve()
        if (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
            return p
        raise ControlPlaneNotFoundError(
            f"ERROR: specified HowlPlane control plane path does not exist: {override_path}"
        )

    # 1. Primary canonical environment variable: HOWLPLANE_HOME / HOWLPLANE_DIR
    env_path = os.environ.get("HOWLPLANE_HOME") or os.environ.get("HOWLPLANE_DIR")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
            return p
        raise ControlPlaneNotFoundError(
            f"ERROR: HOWLPLANE_HOME environment variable points to invalid path: {env_path}"
        )

    # 2. Deprecated legacy environment variable: AI_KNOWLEDGE_LIBRARY
    legacy_env = os.environ.get("AI_KNOWLEDGE_LIBRARY")
    if legacy_env:
        p = Path(legacy_env).expanduser().resolve()
        if (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
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
                    if (p / "src" / "control_plane").is_dir() or (p / "AGENTS.md").is_file():
                        return p
                    raise ControlPlaneNotFoundError(f"ERROR: configured path in '{cfg}' is invalid: {match.group(1)}")
            except ControlPlaneNotFoundError:
                raise
            except Exception:
                pass

    # 4. Self repository root detection
    self_root = Path(__file__).resolve().parent.parent.parent
    if (self_root / "src" / "control_plane").is_dir() and (self_root / "AGENTS.md").is_file():
        return self_root

    raise ControlPlaneNotFoundError(
        "ERROR: configured HowlPlane control plane not found.\n"
        "Please set the HOWLPLANE_HOME environment variable or configure ~/.config/howlplane/config.toml:\n\n"
        "  [control_plane]\n"
        "  path = \"/path/to/howlplane\"\n"
    )


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
        task_class = "test"
    elif any(k in lowered for k in ["doc", "readme", "comment", "guide"]):
        task_class = "documentation"
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
    elif risk_level == "low" and task_class == "documentation":
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
        for role_id in decision.recommended_reviewers:
            role_res = last_cycle.reviewer_results.get(role_id)
            if role_res:
                if role_res.findings:
                    sev_summary = f"{role_res.findings[0].severity.upper()}"
                    status_text = f"{sev_summary} → REMEDIATED" if res.final_state == "complete" and res.remediation_cycles_count > 0 else f"{sev_summary} ({len(role_res.findings)} findings)"
                else:
                    status_text = "PASS"
                role_label = role_id.replace("-reviewer", "").capitalize()
                print(f"  {role_label:<17} {status_text}")
            else:
                role_label = role_id.replace("-reviewer", "").capitalize()
                print(f"  {role_label:<17} PASS")
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


def cmd_work(args: argparse.Namespace) -> int:
    """Executes the governed work command from any target repository."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)

    if not getattr(args, "skip_doctor", False):
        from src.infrastructure.doctor import check_dependencies, check_git_status
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
    target_repo = find_git_repo_root(args.repo)
    ctx = ProjectAdapter.discover(target_repo)
    pool = ProviderPoolManager.from_config(read_only=True, probe_on_start=False)
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
            print("ERROR: ai providers reset requires a resource ID", file=sys.stderr)
            return 1
        pool = ProviderPoolManager.from_config(probe_on_start=False)
        state = pool.reset_resource(resource_id, reprobe=True)
        EvidenceLedger().append_entry(EvidenceEntry(
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
        print(json.dumps(state.to_dict(), indent=2) if args.json else (
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Executes workspace health diagnostics by delegating to control plane doctor."""
    target_repo = find_git_repo_root(args.repo)
    args.repo_dir = str(target_repo)
    return cp_cmd_doctor(args)


def cmd_verify(args: argparse.Namespace) -> int:
    """Executes deterministic project verification by delegating to control plane verify."""
    target_repo = find_git_repo_root(args.repo)
    args.project_dir = str(target_repo)
    args.output = None
    args.run_dir = None
    return cp_cmd_verify(args)


def cmd_howlframe_audit(args: argparse.Namespace) -> int:
    """Executes HowlFrame project context audit by delegating to control plane cli."""
    target_repo = find_git_repo_root(args.repo)
    args.project_dir = str(target_repo)
    return cp_cmd_howlframe_audit(args)


# Lifecycle states from which a run never continues. `progress.json` is a
# heartbeat written by a live process and is not cleared when a task reaches
# one of these, so a cancelled run kept rendering as "STALE / PREPARING" from
# stale heartbeat metadata while its own recommendation correctly said it was
# cancelled. Terminal lifecycle state outranks process-progress presentation.
# `rejected` is deliberately absent: it is a human decision value, and
# HumanLifecycleManager.reject transitions the task to `failed`.
TERMINAL_TASK_STATES = frozenset({"complete", "cancelled", "failed"})


def cmd_status(args: argparse.Namespace) -> int:
    """Displays project status, active task runs, lock status, and crash recovery diagnostics."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    ctx = ProjectAdapter.discover(target_repo)

    print("=" * 60)
    print(f"AI CONTROL PLANE — PROJECT STATUS: {ctx.name}")
    print("=" * 60)
    print(f"Repository Path:    {target_repo}")
    print(f"Control Plane:      {cp_root}")
    print(f"Project Stack:      {', '.join(ctx.project_types) or 'generic'}")
    print(f"Project AGENTS.md:  {'Present' if ctx.has_agents_md else 'Not found'}")
    print(f"Hygiene Status:     {ctx.hygiene_status}")

    # Inspect Repository Lock
    repo_lock_file = get_repo_lock_path(target_repo)
    if repo_lock_file.exists():
        try:
            l_data = json.loads(repo_lock_file.read_text(encoding="utf-8"))
            alive, _ = is_process_alive(l_data.get("pid", 0), l_data.get("hostname", ""))
            status_str = "ACTIVE" if alive else "STALE (Reclaimable)"
            print(f"Repository Lock:    {status_str} — Task: {l_data.get('task_id')}, PID: {l_data.get('pid')}, Command: '{l_data.get('command')}'")
        except Exception:
            print("Repository Lock:    Present (Unparseable)")
    else:
        print("Repository Lock:    Unlocked (Available)")

    print("-" * 60)
    print("VERIFICATION COMMANDS DISCOVERED:")
    plan = ProjectAdapter.create_verification_plan(ctx, task_id="STATUS-CHECK")
    if plan.steps:
        for idx, s in enumerate(plan.steps, 1):
            cmd_display = ' '.join(s.command) if isinstance(s.command, list) else s.command
            print(f"  {idx}. [{s.category}] {cmd_display}")
    else:
        print("  (No automatic test/build commands detected)")

    df_mode = get_dogfood_mode()
    h_bin = find_howlframe_binary()
    h_ver = get_howlframe_version(h_bin) if h_bin else None
    print("-" * 60)
    print("HOWLFRAME DOGFOOD STATUS:")
    print(f"  Mode:               {df_mode}")
    if h_bin:
        print(f"  Binary:             {h_bin} ({h_ver or '0.1.0'})")
        if df_mode == "shadow":
            audit_res = HowlFrameAuditRunner.run_audit(ctx, record_evidence=False, dogfood_mode="shadow")
            print(f"  Audit Result:       {audit_res.audit_status or 'N/A'} ({audit_res.status}) [{audit_res.duration_seconds}s]")
            if audit_res.findings:
                print(f"  Findings:           {', '.join(audit_res.findings)}")
            if audit_res.comparison_notes:
                print(f"  Disagreements:      {', '.join(audit_res.comparison_notes)}")
    else:
        print("  Binary:             Not available (PATH)")

    task_runs_dir = target_repo / ".task_runs"
    runs = []
    if task_runs_dir.is_dir():
        runs = [d.name for d in task_runs_dir.iterdir() if d.is_dir() and (d / "task.yaml").exists()]

    print("-" * 60)
    print(f"ACTIVE TASK RUNS ({len(runs)}):")
    if runs:
        for r in sorted(runs):
            t_dir = task_runs_dir / r
            t_file = t_dir / "task.yaml"
            try:
                rec_diag = CrashRecoveryEngine.inspect_task(target_repo, r)
                t_spec = TaskSpec.load_from_file(str(t_file))
                rec_file = t_dir / "reconciliation.json"
                blockers = 0
                highs = 0
                if rec_file.exists():
                    try:
                        rec_data = json.loads(rec_file.read_text(encoding="utf-8"))
                        blockers = rec_data.get("summary", {}).get("unresolved_blockers", 0)
                        highs = rec_data.get("summary", {}).get("unresolved_highs", 0)
                    except Exception:
                        pass
                ver_file = t_dir / "verification_result.json"
                ver_status = "unverified"
                if ver_file.exists():
                    try:
                        ver_data = json.loads(ver_file.read_text(encoding="utf-8"))
                        ver_status = ver_data.get("overall_status", "unverified")
                    except Exception:
                        pass

                dp_file = t_dir / "decision_packet.md"
                dp_rel = (
                    str(dp_file.relative_to(target_repo))
                    if dp_file.is_file() and dp_file.is_relative_to(target_repo)
                    else str(dp_file)
                )

                prog_file = t_dir / "progress.json"
                prog_data = None
                if prog_file.is_file():
                    try:
                        prog_data = safe_load_json(prog_file)
                    except Exception:
                        prog_data = None

                if t_spec.current_state == "awaiting_human":
                    dec_record = HumanLifecycleManager.load_decision(t_dir)
                    current_fp = compute_repository_fingerprint(target_repo, t_dir)
                    boundaries_list = t_spec.human_approval_requirements or ["human_authority_boundary"]
                    boundaries_str = ", ".join(boundaries_list)
                    receipt_file = t_dir / "execution_receipt.json"

                    print(f"  Task:               {t_spec.task_id}")
                    print(f"  State:              AWAITING_HUMAN (Stage: {rec_diag.get('last_stage', 'awaiting_human')})")
                    print(f"  Verification:       {ver_status.upper()}")
                    if dec_record and dec_record.changeops_decision_id:
                        print(f"  ChangeOps Decision: {dec_record.changeops_decision_id}")
                    if receipt_file.is_file():
                        try:
                            rc_data = json.loads(receipt_file.read_text(encoding="utf-8"))
                            print(f"  Execution Receipt:  {rc_data.get('status', 'unknown').upper()} (Verification: {rc_data.get('verification_status', 'PASS')})")
                        except Exception:
                            pass
                    elif rec_diag.get("native_receipt_found"):
                        print("  Execution Receipt:  PENDING_RECONCILIATION (Found in HowlChangeOps native receipts)")

                    if not dec_record:
                        print(f"  Repository State:   CURRENT")
                        print(f"  Boundary:           {boundaries_str}")
                        print(f"  Decision:           pending")
                        if dp_file.is_file():
                            print(f"  Decision Packet:    {dp_rel}")
                        print("")
                        print("  Next Action:")
                        print(f"    ai approve {t_spec.task_id}")
                        print(f"    ai reject {t_spec.task_id}")
                    elif dec_record.decision == "approved":
                        has_drift, drift_reason = (
                            check_repository_drift(dec_record.repository_state, current_fp)
                            if dec_record.repository_state
                            else (False, None)
                        )
                        appr_state = f"STALE ({drift_reason})" if has_drift else "CURRENT"
                        print(f"  Boundary:           {boundaries_str}")
                        print(f"  Decision:           APPROVED")
                        print(f"  Approval State:     {appr_state}")
                        if dp_file.is_file():
                            print(f"  Decision Packet:    {dp_rel}")
                        print("")
                        print("  Next Action:")
                        if has_drift:
                            print(f"    ai approve {t_spec.task_id} --reason \"re-approved after drift\"")
                        else:
                            print(f"    ai resume {t_spec.task_id}")
                    elif dec_record.decision == "rejected":
                        print(f"  Decision:           REJECTED")
                        if dec_record.reason:
                            print(f"  Reason:             {dec_record.reason}")
                        print("  Terminal state:     FAILED (Rejected)")
                    print("-" * 40)
                elif (
                    prog_data
                    and prog_data.get("state") == "RUNNING"
                    and t_spec.current_state not in TERMINAL_TASK_STATES
                ):
                    p_phase = prog_data.get("phase", t_spec.current_state.upper())
                    p_resource = prog_data.get("resource_id") or t_spec.actual_agent or t_spec.recommended_agent or "N/A"
                    p_elapsed = format_elapsed(prog_data.get("elapsed_seconds", 0))
                    p_heartbeat = format_last_heartbeat(prog_data.get("updated_at"))
                    p_pid = prog_data.get("pid")

                    is_proc_alive = False
                    if rec_diag.get("is_process_running"):
                        is_proc_alive = True
                    elif p_pid:
                        import socket
                        from src.control_plane.locking import is_process_alive as check_pid_alive
                        is_proc_alive, _ = check_pid_alive(p_pid, socket.gethostname())

                    state_label = "RUNNING" if is_proc_alive else "STALE (Process not running)"

                    print(f"  {t_spec.task_id}")
                    print(f"    State:          {state_label}")
                    print(f"    Phase:          {p_phase}")
                    print(f"    Resource:       {p_resource}")
                    print(f"    Elapsed:        {p_elapsed}")
                    print(f"    Last heartbeat: {p_heartbeat}")
                    c_revs = rec_diag.get("completed_reviewers", [])
                    if c_revs:
                        print(f"    Completed Reviews: {', '.join(c_revs)}")
                    # This is the branch an interrupted run actually lands in,
                    # so it has to say why a review is not complete rather than
                    # simply omitting it from the completed list.
                    for role, disposition in (rec_diag.get("reviewer_dispositions") or {}).items():
                        if disposition not in ("completed_clean", "completed_with_findings"):
                            print(f"      - {role}: {disposition.upper()}")
                    if not is_proc_alive:
                        rec_action = rec_diag.get('recommendation') or f"ai resume {t_spec.task_id}"
                        print(f"    Recommendation:    {rec_action}")
                    print("-" * 40)
                elif t_spec.current_state in ("interrupted", "cancelled", "implementing", "reviewing", "remediating", "verifying"):
                    print(f"  Task:               {t_spec.task_id}")
                    print(f"  State:              {t_spec.current_state.upper()} (Last Stage: {rec_diag.get('last_stage')})")
                    print(f"  Classification:     {rec_diag.get('classification', 'RECONCILE_FIRST')}")
                    if rec_diag.get("is_process_running"):
                        p_info = rec_diag.get("process_info") or {}
                        print(f"  Process:            RUNNING (PID: {p_info.get('pid')}, Backend: {p_info.get('backend')})")
                    if rec_diag.get("completed_reviewers"):
                        print(f"  Completed Reviews:  {', '.join(rec_diag.get('completed_reviewers'))}")
                    if rec_diag.get("incomplete_reviewers"):
                        print(f"  Pending Reviews:    {', '.join(rec_diag.get('incomplete_reviewers'))}")
                    # Name why a review is not complete, so an invalid or failed
                    # reviewer is visibly distinct from one that never ran.
                    for role, disposition in (rec_diag.get("reviewer_dispositions") or {}).items():
                        if disposition not in ("completed_clean", "completed_with_findings"):
                            print(f"    - {role}: {disposition.upper()}")
                    print(f"  Recommendation:     {rec_diag.get('recommendation')}")
                    print("-" * 40)
                else:
                    print(f"  - {r}: [{t_spec.current_state.upper()}] {t_spec.objective} (Risk: {t_spec.risk_level.upper()}, Agent: {t_spec.actual_agent or t_spec.recommended_agent or 'N/A'}, Blockers: {blockers}, Highs: {highs}, Verification: {ver_status})")
            except Exception:
                print(f"  - {r}")
    else:
        print("  (No task runs in .task_runs/)")

    journal_dir = target_repo / "documentation" / "task_journals"
    journals = []
    if journal_dir.is_dir():
        journals = [f.name for f in journal_dir.glob("*.md") if f.name != "TEMPLATE.md"]

    if journals:
        print("-" * 60)
        print(f"TASK JOURNALS ({len(journals)}):")
        for j in sorted(journals):
            print(f"  - {j}")

    print("=" * 60)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Explicitly approves a task awaiting human authorization."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    args.repo_dir = str(target_repo)
    args.ledger_file = str(cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
    return cp_cmd_approve(args)


def cmd_reject(args: argparse.Namespace) -> int:
    """Explicitly rejects a task awaiting human authorization."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    args.repo_dir = str(target_repo)
    args.ledger_file = str(cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
    return cp_cmd_reject(args)


def cmd_resume(args: argparse.Namespace) -> int:
    """Resumes task execution following human authorization or crash recovery."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    args.repo_dir = str(target_repo)
    args.ledger_file = str(cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
    return cp_cmd_resume(args)


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancels an active or interrupted task run safely."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    args.repo_dir = str(target_repo)
    args.ledger_file = str(cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
    return cp_cmd_cancel(args)


def cmd_unlock(args: argparse.Namespace) -> int:
    """Reclaims a stale or unverifiable task-run lock by explicit human action."""
    target_repo = find_git_repo_root(args.repo)
    cp_root = find_control_plane_root(args.control_plane_dir)
    args.repo_dir = str(target_repo)
    args.ledger_file = str(cp_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
    return cp_cmd_unlock(args)


def build_parser() -> argparse.ArgumentParser:
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

    parser = argparse.ArgumentParser(
        prog="ai",
        description="Thin Global Entrypoint into the Multi-Agent Engineering Control Plane",
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Command to execute")

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

    p_providers = subparsers.add_parser(
        "providers", parents=[common_parser], help="Show the configured AI resource pool"
    )
    p_providers.add_argument("provider_action", nargs="?", choices=["reset"])
    p_providers.add_argument("resource_id", nargs="?")
    p_providers.add_argument("--json", action="store_true", help="Output versioned JSON")

    subparsers.add_parser("doctor", parents=[common_parser], help="Run workspace health diagnostics")
    subparsers.add_parser("status", parents=[common_parser], help="Show project status and verification plan")

    # approve
    p_appr = subparsers.add_parser("approve", parents=[common_parser], help="Approve an awaiting_human task")
    p_appr.add_argument("task_id", help="Task ID to approve")
    p_appr.add_argument("--reason", help="Optional human reason for approval")
    p_appr.add_argument("--json", action="store_true", help="Output JSON result")

    # reject
    p_rej = subparsers.add_parser("reject", parents=[common_parser], help="Reject an awaiting_human task")
    p_rej.add_argument("task_id", help="Task ID to reject")
    p_rej.add_argument("--reason", help="Optional human reason for rejection")
    p_rej.add_argument("--json", action="store_true", help="Output JSON result")

    # resume
    p_res = subparsers.add_parser("resume", parents=[common_parser], help="Resume a task after human approval or interruption")
    p_res.add_argument("task_id", help="Task ID to resume")
    p_res.add_argument("--json", action="store_true", help="Output JSON result")

    # cancel
    p_can = subparsers.add_parser("cancel", parents=[common_parser], help="Cancel an active or interrupted task run")
    p_can.add_argument("task_id", help="Task ID to cancel")
    p_can.add_argument("--reason", help="Optional reason for cancellation")
    p_can.add_argument("--json", action="store_true", help="Output JSON result")

    p_unlock = subparsers.add_parser(
        "unlock",
        parents=[common_parser],
        help="Reclaim a task-run lock whose owner is gone or unverifiable",
    )
    p_unlock.add_argument("task_id", help="Task ID whose lock should be reclaimed")
    p_unlock.add_argument("--json", action="store_true", help="Output JSON result")

    p_ver = subparsers.add_parser("verify", parents=[common_parser], help="Execute deterministic verification plan")
    p_ver.add_argument("--task-id", help="Task ID")

    p_ha = subparsers.add_parser("howlframe-audit", parents=[common_parser], help="Run HowlFrame project context audit")
    p_ha.add_argument("--max-instructions", type=int, default=DEFAULT_INSTRUCTION_BUDGET, help="Instruction budget limit")
    p_ha.add_argument("--task-id", help="Task ID")
    p_ha.add_argument("--json", action="store_true", help="Output JSON result")

    # create, run, dogfood (Prompt-to-Product Synthesis)
    register_synthesis_subparsers(subparsers, parents=[common_parser])

    return parser


def main(args: Optional[List[str]] = None) -> int:
    p = build_parser()
    opts = p.parse_args(args if args is not None else sys.argv[1:])
    if not opts.subcommand:
        p.print_help()
        return 1

    actions = {
        "work": cmd_work,
        "route": cmd_route,
        "providers": cmd_providers,
        "doctor": cmd_doctor,
        "status": cmd_status,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "resume": cmd_resume,
        "cancel": cmd_cancel,
        "unlock": cmd_unlock,
        "verify": cmd_verify,
        "howlframe-audit": cmd_howlframe_audit,
        "create": cp_cmd_create,
        "run": cp_cmd_run_product,
        "dogfood": cp_cmd_dogfood,
        "acceptance": cp_cmd_acceptance,
        "authority": cp_cmd_authority,
        "local": cp_cmd_local,
    }
    fn = actions.get(opts.subcommand)
    if not fn:
        p.print_help()
        return 1

    try:
        return fn(opts)
    except ControlPlaneError as err:
        print(str(err), file=sys.stderr)
        return 1
    except Exception as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
