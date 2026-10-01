"""`howlplane doctor`: one front door over the existing diagnostics.

No new probing lives here. System checks come from ``doctor.run_diagnostics``;
repository, workers, workspace preparation and Factory readiness come from
``setup_cli.gather`` (which wraps ``agent_readiness`` and workspace trust). The
detailed worker and Factory views remain ``agents doctor`` / ``factory doctor`` and
are reached with ``--agents`` / ``--factory``.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

DOCTOR_SCHEMA = "howlplane.doctor/v1"

_SECTION_OF = {"git": "System", "howlplane": "System", "config": "System",
               "repository": "Repository", "workspace_trust": "Repository", "factory": "Factory"}
_STATUS_OF = {"ready": "ok", "attention": "attention", "blocked": "error",
              "ok": "ok", "warning": "attention", "error": "error"}


def _entry(section: str, label: str, status: str, detail: str = "", fix: str = "") -> Dict[str, str]:
    return {"section": section, "label": label, "status": _STATUS_OF.get(status, status),
            "detail": detail, "fix": fix}


def build_report(repo: Path, live: bool = False) -> Dict[str, Any]:
    from howlplane.control_plane import setup_cli
    from howlplane.control_plane.doctor import run_diagnostics
    from howlplane.control_plane.launcher import find_git_repo_root

    try:
        diag_root = find_git_repo_root(repo)
    except Exception:
        diag_root = repo
    entries: List[Dict[str, str]] = []
    for check in run_diagnostics(repo_root=diag_root):
        if check.name.startswith("AI Resource:"):
            continue  # per-worker rows are the Workers section; `doctor --system` still lists them
        entries.append(_entry("System", check.name, check.status, check.message,
                              (check.details or {}).get("action", "")))
    gathered = setup_cli.gather(repo, live=live)
    for check in gathered["checks"]:
        section = _SECTION_OF.get(check["id"], "Workers" if check["id"].startswith("agent.") else "System")
        entries.append(_entry(section, check["label"], check["status"], check["detail"], check["fix"]))

    issues: List[Dict[str, str]] = []
    for item in entries:
        if item["status"] == "error":
            fix = item["fix"] or ("howlplane doctor --agents" if item["label"] == "Factory" else "")
            issues.append({"problem": f"{item['label']}: {item['detail']}".rstrip(": "), "fix": fix})
    if gathered["needs_preparation"] and not any(i["fix"] == "howlplane setup" for i in issues):
        issues.append({"problem": "This repository is not prepared for autonomous work.", "fix": "howlplane setup"})
    repository_blocked = any(c["id"] == "repository" and c["status"] == setup_cli.BLOCKED for c in gathered["checks"])
    if gathered["factory"] == "blocked" and not repository_blocked and not any(
            i["problem"].startswith("Factory") for i in issues):
        issues.append({"problem": "No AI worker is ready to work.", "fix": "howlplane doctor --agents"})
    blocked_setup = any(c["status"] == setup_cli.BLOCKED for c in gathered["checks"])
    ready = not blocked_setup and not gathered["needs_preparation"] and gathered["factory"] != "blocked"
    has_error = any(e["status"] == "error" for e in entries)
    overall = "READY" if ready and not has_error else "NOT READY"
    return {"schema": DOCTOR_SCHEMA, "overall": overall, "ready": ready, "has_error": has_error,
            "repo": gathered["repo"], "entries": entries, "issues": issues, "factory": gathered["factory"]}


def render(report: Dict[str, Any], style: Any) -> List[str]:
    from howlplane.control_plane.presentation.doctor import check_mark
    severity = "ok" if report["overall"] == "READY" else "error"
    lines = style.header("HowlPlane Doctor", report["overall"], severity)
    for section in ("System", "Workers", "Repository", "Factory"):
        rows = [e for e in report["entries"] if e["section"] == section]
        if not rows:
            continue
        lines.append(style.section(section))
        width = max(len(e["label"]) for e in rows)
        for e in rows:
            mark = check_mark(style, e["status"])
            lines.append(f"  {mark} {e['label']:<{width}}  {style.muted(e['detail'])}".rstrip())
        lines.append("")
    lines.append(style.section("Overall"))
    lines.append(f"  {report['overall']}")
    if report["issues"]:
        lines += ["", style.section("Fix")]
        for index, issue in enumerate(report["issues"], 1):
            lines.append(f"  {index}. {issue['problem']}")
            if issue["fix"]:
                lines.append("     " + style.command(issue["fix"]))
    return lines


def _selector(args: argparse.Namespace) -> str:
    for name in ("system", "agents", "factory", "ready"):
        if getattr(args, name, False):
            return name
    return "all"


def command(args: argparse.Namespace) -> int:
    from howlplane.control_plane import cli as cli_module
    selector = _selector(args)
    live = bool(getattr(args, "live", False))
    as_json = bool(getattr(args, "json", False))
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    if selector == "agents":
        from howlplane.control_plane import agent_readiness
        return agent_readiness.command(argparse.Namespace(
            repo=getattr(args, "repo", None), live=live, json=as_json, agent=None, refresh=False,
            smoke_timeout=agent_readiness.DEFAULT_SMOKE_TIMEOUT_SECONDS,
            workspace_trust=getattr(args, "workspace_trust", None)))
    if selector == "factory":
        return cli_module.cmd_factory_doctor(argparse.Namespace(
            repo=getattr(args, "repo", None), live=live, json=as_json, state_dir=None, target_repo=None,
            workspace_trust=getattr(args, "workspace_trust", None)))
    if selector == "system":
        return cli_module.cmd_system_doctor(args)
    report = build_report(repo, live=live)
    if as_json:
        print(json.dumps(report, indent=2))
    elif selector == "ready":
        if report["ready"]:
            print("READY")
        else:
            print("NOT READY")
            for index, issue in enumerate(report["issues"], 1):
                print(f"{index}. {issue['problem']}")
                if issue["fix"]:
                    print(f"   Fix: {issue['fix']}")
    else:
        from howlplane.control_plane.presentation.style import resolve_style
        print("\n".join(render(report, resolve_style(sys.stdout, cli_module._COLOR_MODE))))
    if selector == "ready":
        return 0 if report["ready"] else 1
    return 1 if report["has_error"] or not report["ready"] else 0
