"""`howlplane setup`: first-run readiness, composed from existing mechanisms.

This owns no state. It reads repository discovery, agent readiness and workspace
trust, renders them as one checklist, and delegates preparation to
`howlplane factory prepare`, which keeps its own authorization prompt.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

SETUP_SCHEMA = "howlplane.setup/v1"

READY = "ready"
ATTENTION = "attention"
BLOCKED = "blocked"


def _check(check_id: str, label: str, status: str, detail: str = "", fix: str = "") -> Dict[str, str]:
    return {"id": check_id, "label": label, "status": status, "detail": detail, "fix": fix}


def _plain(text: str) -> str:
    """Drop the raw 'ERROR:' prefix that low-level errors carry."""
    return re.sub(r"^\s*ERROR:\s*", "", str(text)).strip()


def _install_git_fix() -> str:
    if sys.platform == "darwin":
        return "brew install git"
    if sys.platform.startswith("linux") and shutil.which("apt-get"):
        return "sudo apt-get install -y git"
    return "install Git from https://git-scm.com/downloads"


def _agent_check(summary: Dict[str, Any], trust: Dict[str, Any]) -> Dict[str, str]:
    name, agent = summary["name"], summary["agent"]
    cid = f"agent.{agent}"
    if not summary["installed"]:
        return _check(cid, name, ATTENTION, "not installed")
    if summary["authenticated"] is False:
        return _check(cid, name, ATTENTION, "sign-in required (AUTHENTICATION_REQUIRED)",
                      f"sign in to the {name} CLI, then run `howlplane doctor --agents`")
    state = summary["capacity"]["state"]
    if state in ("SESSION_EXHAUSTED", "QUOTA_EXHAUSTED", "RATE_LIMITED"):
        label = {"SESSION_EXHAUSTED": "session limit", "QUOTA_EXHAUSTED": "quota exhausted",
                 "RATE_LIMITED": "rate limited"}[state]
        return _check(cid, name, ATTENTION, label, "wait for the provider limit to reset")
    if summary["unattended_execution"] is False:
        return _check(cid, name, ATTENTION, "interactive only")
    if trust.get("effective_state") == "TRUST_REQUIRED":
        return _check(cid, name, ATTENTION, "workspace trust needed")
    return _check(cid, name, READY, "ready")


def gather(repo: Path, live: bool = False) -> Dict[str, Any]:
    """Run the read-only checks. Never writes, never prepares."""
    from howlplane.control_plane import agent_readiness
    from howlplane.control_plane.factory.campaign import CampaignError, discover_repository

    checks: List[Dict[str, str]] = []
    if not shutil.which("git"):
        checks.append(_check("git", "Git", BLOCKED, "Git is not installed (no 'git' on PATH)",
                             _install_git_fix()))
        return {"checks": checks, "repo": str(repo), "needs_preparation": False,
                "factory": "blocked"}
    try:
        repository = discover_repository(repo)
    except CampaignError as exc:
        checks.append(_check("repository", "Repository", BLOCKED, _plain(exc), "git init"))
        return {"checks": checks, "repo": str(repo), "needs_preparation": False,
                "factory": "blocked"}
    checks.append(_check("repository", "Repository", READY, str(repository.root)))
    checks.append(_check("git", "Git", READY, "installed"))
    try:
        from importlib.metadata import version
        checks.append(_check("howlplane", "HowlPlane", READY, version("howlplane")))
    except Exception:
        checks.append(_check("howlplane", "HowlPlane", READY, "running from source"))
    try:
        from howlplane.control_plane.config_loader import load_config
        load_config()
        checks.append(_check("config", "Configuration", READY, "valid"))
    except Exception as exc:
        checks.append(_check("config", "Configuration", BLOCKED, _plain(str(exc).splitlines()[0]),
                             "fix the configuration file or environment variable named above"))

    workspace = str(repository.root)
    summaries = agent_readiness.evaluate(live=live, workspace=workspace if live else None)
    trust_report = agent_readiness.workspace_report(workspace)
    per_agent = trust_report.get("agents", {})
    for summary in summaries:
        checks.append(_agent_check(summary, per_agent.get(summary["agent"], {})))
    readiness = agent_readiness.factory_readiness(summaries, trust_report)
    gated = readiness["workspace_trust_required"]
    if gated:
        checks.append(_check("workspace_trust", "Workspace trust", ATTENTION,
                             f"needs preparation ({', '.join(gated)})",
                             "howlplane setup"))
    else:
        checks.append(_check("workspace_trust", "Workspace trust", READY, "prepared"))
    factory_status = readiness["status"]
    checks.append(_check(
        "factory", "Factory",
        {"READY": READY, "DEGRADED": ATTENTION, "BLOCKED": BLOCKED}[factory_status],
        {"READY": "ready", "DEGRADED": "usable, with gaps above",
         "BLOCKED": "no autonomous worker is usable"}[factory_status]))
    return {"checks": checks, "repo": workspace, "needs_preparation": bool(gated),
            "factory": factory_status.lower()}


def _next_step(report: Dict[str, Any]) -> Dict[str, str]:
    for check in report["checks"]:
        if check["status"] == BLOCKED and check["fix"]:
            if check["id"] in ("repository", "git") and not check["fix"].startswith("install "):
                return {"message": f"{check['label']}: {check['detail']}", "command": check["fix"]}
            return {"message": check["fix"], "command": ""}
    if report["needs_preparation"]:
        return {"message": "Prepare this repository for autonomous work. The exact scope is shown first.",
                "command": "howlplane setup --yes"}
    if report["factory"] == "blocked":
        return {"message": "Get at least one AI worker signed in and installed.",
                "command": "howlplane doctor --agents"}
    return {"message": "Start working.", "command": "howlplane start"}


_SECTION_OF = {"git": "System", "howlplane": "System", "config": "System",
               "repository": "Repository", "workspace_trust": "Repository", "factory": "Repository"}
_SEVERITY_OF = {READY: "ok", ATTENTION: "attention", BLOCKED: "error"}


def render(report: Dict[str, Any], style: Any = None) -> str:
    """The checklist, grouped as System / AI workers / Repository. Meaning is carried in words."""
    from howlplane.control_plane.presentation.doctor import check_mark
    from howlplane.control_plane.presentation.style import Style
    style = style or Style()
    lines = [style.paint("title", "HowlPlane Setup"), ""]
    for title in ("System", "AI workers", "Repository"):
        rows = [c for c in report["checks"]
                if (_SECTION_OF.get(c["id"], "AI workers" if c["id"].startswith("agent.") else "System")) == title]
        if not rows:
            continue
        lines.append(style.section(title))
        width = max(len(c["label"]) for c in rows)
        for c in rows:
            detail = c["detail"] or c["status"]
            detail = detail[:1].upper() + detail[1:]
            mark = check_mark(style, _SEVERITY_OF[c["status"]])
            lines.append(f"  {mark} {c['label']:<{width}}  {style.muted(detail)}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


def command(args: argparse.Namespace) -> int:
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    as_json = getattr(args, "json", False)
    from howlplane.control_plane.presentation.style import resolve_style
    from howlplane.control_plane import cli as cli_module
    style = resolve_style(sys.stdout, cli_module._COLOR_MODE)
    report = gather(repo)
    interactive = sys.stdin.isatty() and not as_json
    prepared = None
    if report["needs_preparation"] and (getattr(args, "yes", False) or interactive):
        if not as_json:
            print(render(report, style) + "\n")
        from howlplane.control_plane.factory import prepare
        prepared = prepare.command(argparse.Namespace(
            repo=str(repo), yes=getattr(args, "yes", False), agent=None, live=False,
            json=False, revoke=False, workspace_trust=getattr(args, "workspace_trust", None)))
        report = gather(repo)
        if prepared == 0 and not as_json:
            print("Repository prepared.\n")
    nxt = _next_step(report)
    blocked = any(c["status"] == BLOCKED for c in report["checks"])
    if as_json:
        print(json.dumps({"schema": SETUP_SCHEMA, **report, "next_action": nxt,
                          "ready": not blocked and not report["needs_preparation"]}, indent=2))
    else:
        print(render(report, style))
        print("")
        if not blocked and not report["needs_preparation"]:
            print("You're ready.")
            if report["factory"] == "degraded":
                print("Some workers need attention; HowlPlane will use the ones that are ready.")
            print("\nStart:")
        else:
            print("Not ready yet.")
            print(f"\nNext: {nxt['message']}")
        if nxt["command"]:
            print(f"  {style.command(nxt['command'])}")
    return 1 if blocked or prepared not in (None, 0) else 0
