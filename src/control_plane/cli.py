#!/usr/bin/env python3
"""
cli.py

Deterministic command-line interface for the multi-agent engineering control plane.
"""

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

from src.control_plane.agent_registry import AgentRegistry
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.human_boundary import HumanBoundaryGate, HumanLifecycleManager
from src.control_plane.howlframe_runner import HowlFrameAuditRunner, DEFAULT_INSTRUCTION_BUDGET
from src.control_plane.metrics import MetricsCalculator
from src.control_plane.project_adapter import ProjectAdapter
from src.control_plane.reconciliation import ReviewFinding, ReviewReconciler
from src.control_plane.reviewers import list_reviewer_roles, get_reviewer_role
from src.control_plane.router import TaskRouter
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan


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
    project_dir = args.project_dir or "."
    context = ProjectAdapter.discover(project_dir)
    task_id = args.task_id or "VERIFY-RUN"

    plan = ProjectAdapter.create_verification_plan(context, task_id=task_id)
    print(f"Running verification plan for project '{context.name}' ({len(plan.steps)} steps)...")
    status = plan.execute_all(cwd=project_dir)

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
    ledger.append_entry(entry)
    print(f"Recorded evidence entry '{entry.entry_id}' for task '{entry.task_id}'.")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Calculates and displays engineering history metrics."""
    ledger = EvidenceLedger(args.ledger_file)
    entries = ledger.list_all_entries()
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Executes workspace health diagnostics."""
    from src.infrastructure.doctor import run_diagnostics
    repo_dir = Path(args.repo_dir) if getattr(args, "repo_dir", None) else None
    results = run_diagnostics(repo_root=repo_dir)

    print("=" * 60)
    print("WORKSPACE HEALTH DIAGNOSTICS (DOCTOR)")
    print("=" * 60)
    has_error = False
    for res in results:
        if res.status == "ok":
            mark = "✓"
        elif res.status == "warning":
            mark = "!"
        else:
            mark = "✗"
            has_error = True
        print(f"[{mark}] {res.name}: {res.message}")
        if res.details and isinstance(res.details, dict) and "action" in res.details:
            print(f"    Action: {res.details['action']}")
    print("=" * 60)
    if not has_error:
        print("Status: HEALTHY (All critical checks passed)")
        return 0
    else:
        print("Status: DEGRADED (One or more critical checks failed)")
def cmd_howlframe_audit(args: argparse.Namespace) -> int:
    """Executes HowlFrame project context audit on target repository."""
    import json
    project_dir = getattr(args, "project_dir", None) or "."
    context = ProjectAdapter.discover(project_dir)
    max_instructions = getattr(args, "max_instructions", DEFAULT_INSTRUCTION_BUDGET)
    task_id = getattr(args, "task_id", None)
    ledger_file = getattr(args, "ledger_file", None)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control_plane",
        description="Deterministic Multi-Agent Engineering Control Plane CLI",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Command to execute")

    # init-task
    p_init = subparsers.add_parser("init-task", help="Initialize a new task specification")
    p_init.add_argument("--task-id", required=True, help="Task ID (e.g. TASK-101)")
    p_init.add_argument("--repo", default=".", help="Repository path or name")
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
    p_route = subparsers.add_parser("route-task", help="Route task to appropriate agent and reviewers")
    p_route.add_argument("--task-file", required=True, help="Path to task spec file")

    # briefs
    p_briefs = subparsers.add_parser("briefs", help="Generate independent reviewer briefs")
    p_briefs.add_argument("--task-file", required=True, help="Path to task spec file")
    p_briefs.add_argument("--diff-file", help="Path to diff file")
    p_briefs.add_argument("--roles", nargs="*", help="Specific reviewer roles to generate")
    p_briefs.add_argument("--output-dir", help="Output directory for briefs")

    # prepare-run
    p_prep = subparsers.add_parser("prepare-run", help="Prepare cross-agent dogfood task run artifacts")
    p_prep.add_argument("--task-file", required=True, help="Path to task spec file")
    p_prep.add_argument("--diff-file", help="Path to diff file")
    p_prep.add_argument("--roles", nargs="*", help="Specific reviewer roles to generate")
    p_prep.add_argument("--run-dir", help="Task run directory path")

    # reconcile
    p_rec = subparsers.add_parser("reconcile", help="Reconcile multi-agent review findings")
    p_rec.add_argument("--findings-file", required=True, help="YAML/JSON findings file")
    p_rec.add_argument("--output", "-o", help="Save markdown report to path")
    p_rec.add_argument("--run-dir", help="Task run directory path")

    # verify
    p_ver = subparsers.add_parser("verify", help="Execute project verification plan")
    p_ver.add_argument("--project-dir", default=".", help="Target project root directory")
    p_ver.add_argument("--task-id", help="Optional task ID")
    p_ver.add_argument("--output", "-o", help="Save verification JSON output to path")
    p_ver.add_argument("--run-dir", help="Task run directory path to store verification.json")

    # record
    p_rec_ev = subparsers.add_parser("record", help="Record evidence entry to ledger")
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
    p_met = subparsers.add_parser("metrics", help="Calculate agent performance metrics")
    p_met.add_argument("--ledger-file", help="Ledger file path")
    p_met.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")

    p_rep = subparsers.add_parser("report", help="Display full operational summary report")
    p_rep.add_argument("--ledger-file", help="Ledger file path")
    p_rep.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")

    # boundary
    p_bound = subparsers.add_parser("check-boundary", help="Evaluate human authority boundary")
    p_bound.add_argument("--task-file", required=True, help="Path to task spec file")
    p_bound.add_argument("--actions", nargs="*", help="Planned actions or commands")
    p_bound.add_argument("--output", "-o", help="Save decision packet markdown to path")
    p_bound.add_argument("--run-dir", help="Task run directory path")

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Run deterministic workspace health diagnostics")
    p_doc.add_argument("--repo-dir", help="Target repository directory (defaults to current)")

    # howlframe-audit
    p_ha = subparsers.add_parser("howlframe-audit", help="Run HowlFrame project context audit")
    p_ha.add_argument("--project-dir", default=".", help="Target project root directory")
    p_ha.add_argument("--max-instructions", type=int, default=DEFAULT_INSTRUCTION_BUDGET, help="Instruction budget limit")
    p_ha.add_argument("--task-id", help="Optional task ID for evidence ledger")
    p_ha.add_argument("--ledger-file", help="Ledger file path")
    p_ha.add_argument("--json", action="store_true", help="Output JSON result")

    # approve
    p_appr = subparsers.add_parser("approve", help="Approve an awaiting_human task")
    p_appr.add_argument("task_id", help="Task ID to approve")
    p_appr.add_argument("--repo-dir", default=".", help="Target project root directory")
    p_appr.add_argument("--reason", help="Optional human reason for approval")
    p_appr.add_argument("--ledger-file", help="Ledger file path")
    p_appr.add_argument("--json", action="store_true", help="Output JSON result")

    # reject
    p_rej = subparsers.add_parser("reject", help="Reject an awaiting_human task")
    p_rej.add_argument("task_id", help="Task ID to reject")
    p_rej.add_argument("--repo-dir", default=".", help="Target project root directory")
    p_rej.add_argument("--reason", help="Optional human reason for rejection")
    p_rej.add_argument("--ledger-file", help="Ledger file path")
    p_rej.add_argument("--json", action="store_true", help="Output JSON result")

    # resume
    p_res = subparsers.add_parser("resume", help="Resume an approved task")
    p_res.add_argument("task_id", help="Task ID to resume")
    p_res.add_argument("--repo-dir", default=".", help="Target project root directory")
    p_res.add_argument("--ledger-file", help="Ledger file path")
    p_res.add_argument("--json", action="store_true", help="Output JSON result")

    # cancel
    p_can = subparsers.add_parser("cancel", help="Cancel an active or interrupted task run")
    p_can.add_argument("task_id", help="Task ID to cancel")
    p_can.add_argument("--repo-dir", default=".", help="Target project root directory")
    p_can.add_argument("--reason", help="Optional reason for cancellation")
    p_can.add_argument("--ledger-file", help="Ledger file path")
    p_can.add_argument("--json", action="store_true", help="Output JSON result")

    register_synthesis_subparsers(subparsers)

    return parser


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

    # authority (read-only authority profile inspection, #59 Phase 26)
    p_authority = subparsers.add_parser("authority", help="Inspect delegated authority profiles (read-only)", **kwargs)
    authority_sub = p_authority.add_subparsers(dest="authority_action", required=True)
    p_authority_show = authority_sub.add_parser("show", help="Show a canonical authority profile's exact permissions")
    p_authority_show.add_argument("profile_id", choices=["strict", "overnight-safe"])
    p_authority_show.add_argument("--json", action="store_true", help="Output JSON result")

    # local (local Ollama model setup/health check, #58 Phase 3)
    p_local = subparsers.add_parser("local", help="Local (Ollama) model utilities", **kwargs)
    p_local.add_argument("local_action", choices=["setup"], help="Action to perform")
    p_local.add_argument("--model", default="qwen2.5-coder:7b-instruct", help="Model to pull/verify")
    p_local.add_argument("--json", action="store_true", help="Output JSON result")


def _handle_decision(args: argparse.Namespace, decision: str) -> int:
    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    fn = HumanLifecycleManager.approve if decision == "approved" else HumanLifecycleManager.reject
    record = fn(
        target_repo=args.repo_dir,
        task_id=args.task_id,
        reason=getattr(args, "reason", None),
        operator_source="cli",
        ledger=ledger,
    )
    if getattr(args, "json", False):
        print(record.to_json())
    else:
        title = "TASK AUTHORIZED" if decision == "approved" else "TASK REJECTED"
        print("=" * 60)
        print(f"HOWLPLANE — {title}: {record.task_id}")
        print("=" * 60)
        print(f"Task:               {record.task_id}")
        print(f"Decision:           {decision.upper()}")
        print(f"Timestamp:          {record.timestamp}")
        if record.reason:
            print(f"Reason:             {record.reason}")
        if decision == "approved":
            print("")
            print("Next Action:")
            print(f"  ai resume {record.task_id}")
        else:
            print("Terminal state:     FAILED (Rejected)")
        print("=" * 60)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _handle_decision(args, "approved")


def cmd_reject(args: argparse.Namespace) -> int:
    return _handle_decision(args, "rejected")


def cmd_resume(args: argparse.Namespace) -> int:
    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    res = HumanLifecycleManager.resume(
        target_repo=args.repo_dir,
        task_id=args.task_id,
        ledger=ledger,
    )
    if getattr(args, "json", False):
        import json
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(f"Task '{res.task_id}' RESUMED. Final state: {res.final_state.upper()} (Exit {res.exit_code}).")
    return res.exit_code


def cmd_cancel(args: argparse.Namespace) -> int:
    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    res = HumanLifecycleManager.cancel(
        target_repo=args.repo_dir,
        task_id=args.task_id,
        reason=getattr(args, "reason", None),
        ledger=ledger,
    )
    if getattr(args, "json", False):
        import json
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(f"Task '{res.task_id}' CANCELLED safely. Code changes preserved in working tree.")
    return res.exit_code


def _lock_candidates(repo_dir, task_id):
    """Every lock that could be holding this task back, in reclaim order.

    `ai status` reports the repository lock, so `ai unlock` has to be able to
    act on it. Reclaiming only `.task.lock` meant the command truthfully said
    "nothing to reclaim" while `.git/howlplane.lock` kept the task unrecoverable
    (HOWLFRAM-SLOPFIX-06).
    """
    from src.control_plane.locking import get_repo_lock_path, get_task_lock_path

    return [
        ("task run", get_task_lock_path(repo_dir, task_id)),
        ("repository", get_repo_lock_path(repo_dir)),
    ]


def _lock_relevance(owner: dict, task_id: str):
    """Reports whether a lock belongs to this task, and why not when it does not.

    Deliberately narrow: this is a reclaim path, not a general lock remover. A
    lock written for another task, or for an operation that is not a task's
    repository mutation, is never this command's business.
    """
    if owner.get("task_id") != task_id:
        return False, (
            f"held for task '{owner.get('task_id')}', not '{task_id}'"
        )
    if owner.get("lock_type") not in ("task_run", "repository_mutation"):
        return False, (
            f"lock type '{owner.get('lock_type')}' is not a task-owned lock"
        )
    return True, ""


def cmd_unlock(args: argparse.Namespace) -> int:
    """Reclaims the locks holding a task back, when their owners are gone.

    The one explicit, audited takeover path. `ai resume` deliberately keeps its
    fail-closed behavior, so nothing ever steals a lock implicitly; a person
    asks for this, and the reclamation is recorded (HOWLFRAM-SLOPFIX-05).
    Both the task-run lock and the repository lock are inspected, so what this
    command acts on matches what `ai status` reports (HOWLFRAM-SLOPFIX-06).
    """
    from src.control_plane.locking import (
        LockError,
        classify_lock_owner,
        reclaim_lock,
    )
    from src.control_plane.atomic_io import safe_load_json

    ledger = EvidenceLedger(args.ledger_file) if getattr(args, "ledger_file", None) else None
    as_json = bool(getattr(args, "json", False))

    def audit(action, result, artifact=None, metadata=None):
        if ledger is None:
            return
        ledger.append_entry(
            EvidenceEntry(
                task_id=args.task_id,
                agent_id="human_operator",
                action=action,
                command=f"ai unlock {args.task_id}",
                result=result,
                artifact=artifact,
                repository=str(args.repo_dir),
                metadata=metadata or {},
            )
        )

    audit("unlock_requested", "REQUESTED")

    inspected = []
    reclaimed = []
    refusals = []

    for label, lock_path in _lock_candidates(args.repo_dir, args.task_id):
        if not lock_path.exists():
            continue
        try:
            owner = safe_load_json(lock_path)
        except Exception as err:
            refusals.append(f"{label} lock at '{lock_path}' is unreadable: {err}")
            continue

        relevant, why_not = _lock_relevance(owner, args.task_id)
        state, reason = classify_lock_owner(
            owner.get("pid", -1),
            owner.get("hostname", ""),
            owner.get("process_create_time", 0.0),
        )
        inspected.append(
            {
                "scope": label,
                "path": str(lock_path),
                "owner_state": state.value,
                "reason": reason,
                "relevant": relevant,
                "pid": owner.get("pid"),
                "hostname": owner.get("hostname"),
                "operation": owner.get("operation"),
                "task_id": owner.get("task_id"),
            }
        )

        if not as_json:
            print(f"{label.capitalize()} lock: {lock_path}")
            print(
                f"  Owner: pid {owner.get('pid')} @ {owner.get('hostname')} "
                f"({owner.get('command')}) -- {state.value}"
            )
            print(f"  {reason}")

        if not relevant:
            msg = f"Refusing to reclaim {label} lock: {why_not}."
            refusals.append(msg)
            if not as_json:
                print(f"  {msg}")
            audit(
                "unlock_refused",
                "NOT_THIS_TASK",
                artifact=str(lock_path),
                metadata={"scope": label, "reason": why_not},
            )
            continue

        try:
            record = reclaim_lock(lock_path)
        except LockError as err:
            refusals.append(str(err))
            if not as_json:
                print(f"  ERROR: {err}")
            audit(
                "unlock_refused",
                state.value,
                artifact=str(lock_path),
                metadata={"scope": label, "reason": str(err)},
            )
            continue

        payload = record.to_dict()
        payload["scope"] = label
        reclaimed.append(payload)
        # Keeps the established ledger vocabulary rather than introducing a
        # second name for the same event.
        audit(
            "stale_lock_reclaimed",
            record.owner_state,
            artifact=record.lock_path,
            metadata=payload,
        )
        if not as_json:
            print(f"  Reclaimed {record.owner_state} {label} lock.")

    if as_json:
        import json
        print(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "inspected": inspected,
                    "reclaimed": reclaimed,
                    "refused": refusals,
                },
                indent=2,
            )
        )

    if not inspected:
        if not as_json:
            print(f"No locks held for '{args.task_id}'. Nothing to reclaim.")
        audit("unlock_requested", "NO_OP")
        return 0

    if reclaimed:
        if not as_json:
            print(f"You can now run: ai resume {args.task_id}")
        return 0

    return 1 if refusals else 0


def cmd_create(args: argparse.Namespace) -> int:
    """Creates a runnable software product from natural language intent."""
    import re
    from src.control_plane.synthesis import (
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

    from src.control_plane.synthesis.provider_pool import ProviderPoolManager

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
        print(f"  ai run {res.product_bundle.directory}")
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
    from src.control_plane.synthesis.campaign_state import DurableCampaignState

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

    from src.control_plane.synthesis import MarathonDogfoodEngine
    from src.control_plane.synthesis.provider_pool import ProviderPoolManager
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
        print("Usage: ai acceptance overnight-integration --authority-profile <strict|overnight-safe>")
        return 1

    from src.control_plane.synthesis import MarathonDogfoodEngine
    from src.control_plane.synthesis.provider_pool import ProviderPoolManager

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
    from src.control_plane.authority_profile import get_profile

    if getattr(args, "authority_action", None) != "show":
        print("Usage: ai authority show <strict|overnight-safe>")
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

    from src.control_plane.agent_execution import OllamaLocalBackend, diagnose_ollama
    from src.control_plane.task_spec import TaskSpec

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


def main(args: Optional[List[str]] = None) -> int:
    if args is None:
        args = sys.argv[1:]
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    if not parsed_args.subcommand:
        parser.print_help()
        return 1

    handlers = {
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
        "howlframe-audit": cmd_howlframe_audit,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "resume": cmd_resume,
        "cancel": cmd_cancel,
        "create": cmd_create,
        "run": cmd_run_product,
        "dogfood": cmd_dogfood,
        "authority": cmd_authority,
        "local": cmd_local,
    }

    handler = handlers.get(parsed_args.subcommand)
    if not handler:
        parser.print_help()
        return 1
    return handler(parsed_args)


if __name__ == "__main__":
    sys.exit(main())

