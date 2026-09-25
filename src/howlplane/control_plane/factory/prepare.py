"""`howlplane factory prepare`: explicit operator authorization plus one-time workspace trust.

Running this command is the operator's authorization. It names the exact scope
first (the repository checkout and its Factory workspace root), asks for
confirmation, records HowlPlane's own authorization, creates the stable Factory
worktree, and then prepares vendor trust only through supported mechanisms:

* Cursor: the documented `--trust` flag, stopped at a sentinel model so no
  inference runs.
* Devin: its own interactive trust prompt, opened on the operator's terminal and
  answered by the operator. HowlPlane sends no input.
* Codex, Claude Code, AGY: nothing to prepare; their noninteractive modes never
  prompt for trust.

Trust is then re-verified. `--revoke` removes only HowlPlane's authorization;
vendor trust stays where the vendor keeps it, and the command says where.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from howlplane.control_plane import agent_readiness, workspace_trust
from howlplane.control_plane.factory.campaign import (
    CampaignError,
    discover_repository,
    factory_workspace_root,
    prepare_campaign,
    resolve_campaign,
)

VENDOR_STORES = {
    "cursor": lambda: str(workspace_trust.cursor_projects_dir() / "<slug>" / ".workspace-trusted"),
    "devin_cli": lambda: str(workspace_trust.devin_trust_file()),
}


def _installed(agent: str) -> bool:
    return shutil.which(agent_readiness.SPECS[agent].binary) is not None


def scope_plan(repo: Path, agents: list[str]) -> dict[str, Any]:
    """What preparing `repo` would authorize, before anything is written."""
    repository = discover_repository(repo)
    campaign = resolve_campaign(repository.root, prefer_active=True)
    root = factory_workspace_root(repository)
    workspaces = [repository.root, root]
    target = campaign.target_dir
    if root not in target.parents:
        # An advanced --target-repo outside the root is authorized exactly.
        workspaces.append(target)
    plan = []
    for agent in agents:
        spec = workspace_trust.adapter(agent)
        installed = _installed(agent)
        if spec is None:
            continue
        needs = spec.scope != "not_enforced"
        plan.append({
            "agent": agent, "name": agent_readiness.SPECS[agent].name, "installed": installed,
            "method": spec.method, "scope": spec.scope,
            "covers_new_worktrees": spec.scope in ("ancestor", "not_enforced"),
            "action": ("none needed" if not needs else "skipped: CLI not installed" if not installed
                       else agent_readiness.PREPARE_TEXT[spec.method]),
        })
    return {"repository": repository, "campaign": campaign, "factory_root": root,
            "target": target, "workspaces": workspaces, "agents": plan}


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["HOWLPLANE FACTORY PREPARE", "",
             "This authorizes HowlPlane to prepare and use, unattended:",
             f"  Repository checkout:     {plan['repository'].root}",
             f"  Factory workspace root:  {plan['factory_root']}",
             f"  Factory worktree:        {plan['target']}", "",
             "Agents:"]
    for item in plan["agents"]:
        inherit = ("new Factory worktrees under the root need no further preparation" if item["covers_new_worktrees"]
                   else "each new directory must be prepared")
        lines.append(f"  {item['name']}: {item['action']}")
        if item["scope"] != "not_enforced" and item["installed"]:
            lines.append(f"    Trust applies to the directories above and their descendants; {inherit}.")
    lines += ["", "Nothing outside these paths is authorized. Vendor trust prompts are never answered by HowlPlane."]
    return "\n".join(lines) + "\n"


def _confirmed(args: argparse.Namespace) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input("Authorize this scope? Type 'yes' to continue: ").strip().lower() == "yes"
    except EOFError:
        return False


def _revoke(repo: Path, as_json: bool) -> int:
    root = discover_repository(repo).root
    removed = workspace_trust.revoke(root)
    stores = {agent: locate() for agent, locate in VENDOR_STORES.items()}
    if as_json:
        print(json.dumps({"revoked": removed is not None, "repo_root": str(root), "vendor_trust_stores": stores}, indent=2))
        return 0
    print(f"HowlPlane authorization for {root}: {'removed' if removed else 'none recorded'}.")
    print("Vendor trust is unchanged; HowlPlane never edits it. To withdraw it, use each CLI's own settings:")
    for agent, location in stores.items():
        print(f"  {agent_readiness.SPECS[agent].name}: {location}")
    return 0


def command(args: argparse.Namespace) -> int:
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    as_json = getattr(args, "json", False)
    if not repo.is_dir():
        print(f"Factory prepare: {repo} is not a directory.", file=sys.stderr)
        return 1
    try:
        if getattr(args, "revoke", False):
            return _revoke(repo, as_json)
        agents = list(getattr(args, "agent", None) or agent_readiness.AGENT_ORDER)
        plan = scope_plan(repo, agents)
    except CampaignError as exc:
        print(f"Factory prepare: {exc}", file=sys.stderr)
        return 1
    print(render_plan(plan), end="", file=sys.stderr if as_json else sys.stdout)
    if not _confirmed(args):
        print("Factory prepare: not authorized (pass --yes or confirm at a terminal). Nothing was changed.",
              file=sys.stderr)
        return 2
    repository = plan["repository"]
    workspace_trust.authorize(repository.root, repository.common_git_dir, plan["factory_root"], plan["workspaces"],
                              [item["agent"] for item in plan["agents"]], intent="howlplane factory prepare")
    try:
        prepare_campaign(plan["campaign"])
    except CampaignError as exc:
        print(f"Factory prepare: {exc}", file=sys.stderr)
        return 1
    hosted = agent_readiness.hosted_probes_allowed()
    results: dict[str, list[dict[str, Any]]] = {}
    for item in plan["agents"]:
        agent = item["agent"]
        if item["scope"] == "not_enforced" or not item["installed"]:
            continue
        if not hosted:
            results[agent] = [{**workspace_trust.check(agent, path), "detail": agent_readiness.LOCAL_ONLY_DETAIL}
                              for path in plan["workspaces"]]
            continue
        outcome = []
        for path in plan["workspaces"]:
            prepared = workspace_trust.prepare(agent, path)
            # Re-verify with the CLI itself; the store alone is our model of it.
            verified = workspace_trust.probe(agent, path) if prepared["state"] == workspace_trust.READY else prepared
            if verified["state"] == workspace_trust.READY:
                agent_readiness.clear_workspace_refusal(agent, verified["workspace"])
            outcome.append(verified)
        results[agent] = outcome
        workspace_trust.record_prepared(repository.root, agent, outcome)
    target = str(plan["target"])
    summaries = agent_readiness.evaluate(live=getattr(args, "live", False), workspace=target if getattr(
        args, "live", False) else None)
    report = agent_readiness.workspace_report(target)
    readiness = agent_readiness.factory_readiness(summaries, report)
    if as_json:
        print(json.dumps({"schema": workspace_trust.SCHEMA, "repo_root": str(repository.root),
                          "factory_root": str(plan["factory_root"]), "target": target,
                          "trust": results, "workspace": report, "readiness": readiness}, indent=2))
    else:
        print("\nTRUST PREPARATION")
        if not results:
            print("  Nothing to prepare.")
        for agent, outcome in results.items():
            for entry in outcome:
                print(f"  {agent_readiness.SPECS[agent].name}: {entry['state']} — {entry['workspace']} ({entry['detail']})")
        print()
        print("\n".join(agent_readiness.render_workspace(report)))
        print()
        from howlplane.control_plane.orchestration import default_execution_budget
        print(agent_readiness.render_factory(readiness, default_execution_budget()), end="")
    return 1 if readiness["status"] == "BLOCKED" else 0
