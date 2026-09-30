"""`howlplane setup`: first-run readiness, composed from existing mechanisms.

This owns no state. It reads repository discovery, agent readiness and workspace
trust, renders them as one checklist, and delegates preparation to
`howlplane factory prepare`, which keeps its own authorization prompt.
"""

import argparse
import json
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


def _agent_check(summary: Dict[str, Any], trust: Dict[str, Any]) -> Dict[str, str]:
    name, agent = summary["name"], summary["agent"]
    cid = f"agent.{agent}"
    if not summary["installed"]:
        return _check(cid, name, ATTENTION, "not installed")
    if summary["authenticated"] is False:
        return _check(cid, name, ATTENTION, "sign-in required (AUTHENTICATION_REQUIRED)",
                      f"sign in to the {name} CLI, then run `howlplane agents doctor`")
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


def gather(repo: Path) -> Dict[str, Any]:
    """Run the read-only checks. Never writes, never prepares."""
    from howlplane.control_plane import agent_readiness
    from howlplane.control_plane.factory.campaign import CampaignError, discover_repository

    checks: List[Dict[str, str]] = []
    try:
        repository = discover_repository(repo)
    except CampaignError as exc:
        checks.append(_check("repository", "Repository", BLOCKED, str(exc),
                             "run `git init` (or cd into a Git repository) and retry"))
        return {"checks": checks, "repo": str(repo), "needs_preparation": False,
                "factory": "blocked"}
    checks.append(_check("repository", "Repository", READY, str(repository.root)))
    checks.append(_check("git", "Git", READY if shutil.which("git") else BLOCKED,
                         "" if shutil.which("git") else "git not found on PATH",
                         "" if shutil.which("git") else "install Git"))
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
        checks.append(_check("config", "Configuration", BLOCKED, str(exc).splitlines()[0],
                             "fix the configuration file or environment variable named above"))

    workspace = str(repository.root)
    summaries = agent_readiness.evaluate()
    trust_report = agent_readiness.workspace_report(workspace)
    per_agent = trust_report.get("agents", {})
    for summary in summaries:
        checks.append(_agent_check(summary, per_agent.get(summary["agent"], {})))
    readiness = agent_readiness.factory_readiness(summaries, trust_report)
    gated = readiness["workspace_trust_required"]
    if gated:
        checks.append(_check("workspace_trust", "Workspace trust", ATTENTION,
                             f"needs preparation ({', '.join(gated)})",
                             "howlplane factory prepare"))
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
            return {"message": check["fix"], "command": ""}
    if report["needs_preparation"]:
        return {"message": "Prepare this repository for unattended Factory use.",
                "command": "howlplane factory prepare"}
    if report["factory"] == "blocked":
        return {"message": "Get at least one AI worker signed in and installed.",
                "command": "howlplane agents doctor"}
    return {"message": "Start the Factory.", "command": "howlplane factory start"}


def render(report: Dict[str, Any]) -> str:
    lines = ["HowlPlane Setup", ""]
    for check in report["checks"]:
        dots = "." * max(2, 18 - len(check["label"]))
        lines.append(f"{check['label']} {dots} {check['detail'] or check['status']}")
    return "\n".join(lines)


def command(args: argparse.Namespace) -> int:
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    as_json = getattr(args, "json", False)
    report = gather(repo)
    interactive = sys.stdin.isatty() and not as_json
    prepared = None
    if report["needs_preparation"] and (getattr(args, "yes", False) or interactive):
        if not as_json:
            print(render(report) + "\n")
        from howlplane.control_plane.factory import prepare
        prepared = prepare.command(argparse.Namespace(
            repo=str(repo), yes=getattr(args, "yes", False), agent=None, live=False,
            json=False, revoke=False, workspace_trust=getattr(args, "workspace_trust", None)))
        report = gather(repo)
    nxt = _next_step(report)
    blocked = any(c["status"] == BLOCKED for c in report["checks"])
    if as_json:
        print(json.dumps({"schema": SETUP_SCHEMA, **report, "next_action": nxt,
                          "ready": not blocked and not report["needs_preparation"]}, indent=2))
    else:
        print(render(report))
        print("")
        print("Ready." if not blocked and not report["needs_preparation"] else "Not ready yet.")
        print(f"\nNext: {nxt['message']}")
        if nxt["command"]:
            print(f"  {nxt['command']}")
    return 1 if blocked or prepared not in (None, 0) else 0
