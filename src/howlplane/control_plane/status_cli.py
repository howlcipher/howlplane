"""Inspection command handlers (status, doctor, agents).

Moved out of ``cli.py`` (backlog #78). ``cli.py`` re-exports every name and these
handlers look shared helpers up on the ``cli`` module at call time, so existing
imports and test monkeypatches of ``cli.<name>`` keep working unchanged.
"""

import argparse
import json
import sys


def _cli():
    from howlplane.control_plane import cli
    return cli


def cmd_status(args: argparse.Namespace) -> int:
    """Displays project status, active task runs, lock status, and crash recovery diagnostics."""
    target_repo = _cli()._resolve_repo(args)
    cp_root = _cli().find_control_plane_root(getattr(args, "control_plane_dir", None))
    ctx = _cli().ProjectAdapter.discover(target_repo)

    print("=" * 60)
    print(f"AI CONTROL PLANE — PROJECT STATUS: {ctx.name}")
    print("=" * 60)
    print(f"Repository Path:    {target_repo}")
    print(f"Control Plane:      {cp_root}")
    print(f"Project Stack:      {', '.join(ctx.project_types) or 'generic'}")
    print(f"Project AGENTS.md:  {'Present' if ctx.has_agents_md else 'Not found'}")
    print(f"Hygiene Status:     {ctx.hygiene_status}")

    # Inspect Repository Lock
    repo_lock_file = _cli().get_repo_lock_path(target_repo)
    if repo_lock_file.exists():
        try:
            l_data = json.loads(repo_lock_file.read_text(encoding="utf-8"))
            alive, _ = _cli().is_process_alive(l_data.get("pid", 0), l_data.get("hostname", ""))
            status_str = "ACTIVE" if alive else "STALE (Reclaimable)"
            print(f"Repository Lock:    {status_str} — Task: {l_data.get('task_id')}, PID: {l_data.get('pid')}, Command: '{l_data.get('command')}'")
        except Exception:
            print("Repository Lock:    Present (Unparseable)")
    else:
        print("Repository Lock:    Unlocked (Available)")

    print("-" * 60)
    print("VERIFICATION COMMANDS DISCOVERED:")
    plan = _cli().ProjectAdapter.create_verification_plan(ctx, task_id="STATUS-CHECK")
    if plan.steps:
        for idx, s in enumerate(plan.steps, 1):
            cmd_display = ' '.join(s.command) if isinstance(s.command, list) else s.command
            print(f"  {idx}. [{s.category}] {cmd_display}")
    else:
        print("  (No automatic test/build commands detected)")

    df_mode = _cli().get_dogfood_mode()
    h_bin = _cli().find_howlframe_binary()
    h_ver = _cli().get_howlframe_version(h_bin) if h_bin else None
    print("-" * 60)
    print("HOWLFRAME DOGFOOD STATUS:")
    print(f"  Mode:               {df_mode}")
    if h_bin:
        print(f"  Binary:             {h_bin} ({h_ver or '0.1.0'})")
        if df_mode == "shadow":
            audit_res = _cli().HowlFrameAuditRunner.run_audit(ctx, record_evidence=False, dogfood_mode="shadow")
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
                rec_diag = _cli().CrashRecoveryEngine.inspect_task(target_repo, r)
                t_spec = _cli().TaskSpec.load_from_file(str(t_file))
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
                        prog_data = _cli().safe_load_json(prog_file)
                    except Exception:
                        prog_data = None

                if t_spec.current_state == "awaiting_human":
                    dec_record = _cli().HumanLifecycleManager.load_decision(t_dir)
                    current_fp = _cli().compute_repository_fingerprint(target_repo, t_dir)
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
                        print(f"    howlplane approve {t_spec.task_id}")
                        print(f"    howlplane reject {t_spec.task_id}")
                    elif dec_record.decision == "approved":
                        has_drift, drift_reason = (
                            _cli().check_repository_drift(dec_record.repository_state, current_fp)
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
                            print(f"    howlplane approve {t_spec.task_id} --reason \"re-approved after drift\"")
                        else:
                            print(f"    howlplane resume {t_spec.task_id}")
                    elif dec_record.decision == "rejected":
                        print(f"  Decision:           REJECTED")
                        if dec_record.reason:
                            print(f"  Reason:             {dec_record.reason}")
                        print("  Terminal state:     FAILED (Rejected)")
                    print("-" * 40)
                elif (
                    prog_data
                    and prog_data.get("state") == "RUNNING"
                    and t_spec.current_state not in _cli().TERMINAL_TASK_STATES
                ):
                    p_phase = prog_data.get("phase", t_spec.current_state.upper())
                    p_resource = prog_data.get("resource_id") or t_spec.actual_agent or t_spec.recommended_agent or "N/A"
                    p_elapsed = _cli().format_elapsed(prog_data.get("elapsed_seconds", 0))
                    p_heartbeat = _cli().format_last_heartbeat(prog_data.get("updated_at"))
                    p_pid = prog_data.get("pid")

                    is_proc_alive = False
                    if rec_diag.get("is_process_running"):
                        is_proc_alive = True
                    elif p_pid:
                        import socket
                        from howlplane.control_plane.locking import is_process_alive as check_pid_alive
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
                    for role, disposition in (rec_diag.get("reviewer_dispositions") or {}).items():
                        if disposition not in ("completed_clean", "completed_with_findings"):
                            print(f"      - {role}: {disposition.upper()}")
                    if not is_proc_alive:
                        rec_action = rec_diag.get('recommendation') or f"howlplane resume {t_spec.task_id}"
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """`howlplane doctor`: the front door. Selectors narrow it; the engine checks are unchanged."""
    from howlplane.control_plane import doctor_report
    return doctor_report.command(args)


def cmd_system_doctor(args: argparse.Namespace) -> int:
    """Executes workspace health diagnostics (the system checks only)."""
    from howlplane.control_plane.doctor import run_diagnostics
    target_repo = _cli()._resolve_repo(args)
    results = run_diagnostics(repo_root=target_repo)
    if getattr(args, "json", False):
        print(json.dumps({"schema": "howlplane.doctor.system/v1", "checks": [
            {"name": r.name, "status": r.status, "message": r.message, "details": r.details}
            for r in results]}, indent=2))
        return 1 if any(res.status not in ("ok", "warning") for res in results) else 0

    from howlplane.control_plane.presentation.doctor import render_checks_table
    from howlplane.control_plane.presentation.style import resolve_style
    style = resolve_style(sys.stdout, _cli()._COLOR_MODE)
    has_error = any(res.status not in ("ok", "warning") for res in results)
    warnings = sum(1 for res in results if res.status == "warning")
    lines = style.header("HowlPlane Doctor", "DEGRADED" if has_error else "HEALTHY",
                         "error" if has_error else "ok")
    lines += render_checks_table(results, style)
    ok = sum(1 for res in results if res.status == "ok")
    errors = sum(1 for res in results if res.status not in ("ok", "warning"))
    lines += ["", f"{ok} checks passed, {warnings} warning(s), {errors} failed."]
    print("\n".join(lines))
    return 1 if has_error else 0


def cmd_agents(args: argparse.Namespace) -> int:
    if getattr(args, "agents_action", None) != "doctor":
        print("Usage: howlplane agents doctor [--repo PATH] [--live] [--json]")
        return 1
    return _cli().agent_readiness.command(args)
