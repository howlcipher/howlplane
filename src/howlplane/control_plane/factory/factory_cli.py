"""Factory command handlers (status, start, stop, resume, logs, doctor).

Moved out of ``cli.py`` (backlog #75). ``cli.py`` re-exports every name and these
handlers look shared helpers up on the ``cli`` module at call time, so existing
imports and test monkeypatches of ``cli.<name>`` keep working unchanged.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from howlplane.control_plane import agent_readiness
from howlplane.control_plane.task_spec import TaskSpec


def _cli():
    from howlplane.control_plane import cli
    return cli


def _worker_display(resource_id: Optional[str]) -> Optional[str]:
    from howlplane.control_plane.presentation.operator import worker_display
    return worker_display(resource_id)


def _owner_decisions(work_store: Any, target_dir: Any, state_dir: Any = None) -> List[Dict[str, str]]:
    """Exact approve/reject commands for every work item parked for the owner.

    A governed task that really is ``awaiting_human`` gets the task command, so its
    drift and verification checks apply. Any other ``awaiting_owner`` item gets the
    work-item command (``factory/work_item_decision.py``) when ``state_dir`` is known.
    """
    import shlex
    from howlplane.control_plane.factory.work_item import WorkItemState
    from howlplane.control_plane.factory.work_item_decision import work_item_commands
    decisions: List[Dict[str, str]] = []
    for item in work_store.list_all():
        if item.state != WorkItemState.AWAITING_OWNER:
            continue
        entry: Optional[Dict[str, str]] = None
        for task_id in item.task_ids:
            task_file = Path(target_dir) / ".task_runs" / task_id / "task.yaml"
            try:
                if TaskSpec.load_from_file(str(task_file)).current_state != "awaiting_human":
                    continue
            except Exception:
                continue
            repo = shlex.quote(str(target_dir))
            entry = {
                "work_item_id": item.work_item_id, "task_id": task_id, "kind": "task", "title": getattr(item, "title", None),
                "approve": f"howlplane approve {task_id} --repo {repo}",
                "reject": f"howlplane reject {task_id} --repo {repo}",
            }
            break
        if entry is None and state_dir is not None:
            entry = {"work_item_id": item.work_item_id, "kind": "work_item", "title": getattr(item, "title", None),
                     **work_item_commands(item.work_item_id, str(state_dir), str(target_dir))}
        if entry is not None:
            decisions.append(entry)
    return decisions


def _proposal_decisions(proposal_store: Any, state_dir: Any) -> List[Dict[str, str]]:
    """Exact approve/reject commands for every repository proposal awaiting authority."""
    from howlplane.control_plane.factory.proposal_decision import proposal_commands
    if state_dir is None:
        return []
    return [
        {"proposal_id": p.proposal_id, "repository_name": p.repository_name, "kind": "proposal",
         **proposal_commands(p.proposal_id, str(state_dir))}
        for p in proposal_store.list_awaiting_authority()
    ]


def _bootstrap_ready(proposal_store: Any, state_dir: Any) -> List[Dict[str, str]]:
    """Accepted proposals that have not been bootstrapped yet, with the one command that consumes them."""
    from howlplane.control_plane.factory.bootstrap import BootstrapRunStore, COMPLETED, bootstrap_command
    from howlplane.control_plane.factory.repo_proposal import ProposalState
    if state_dir is None:
        return []
    runs = BootstrapRunStore(Path(state_dir).resolve() / "bootstrap_runs")
    ready = []
    for p in proposal_store.list_all():
        if p.state != ProposalState.ACCEPTED.value:
            continue
        run = runs.load(p.proposal_id) if runs.exists(p.proposal_id) else None
        if run is not None and run.state == COMPLETED:
            continue
        ready.append({"proposal_id": p.proposal_id, "repository_name": p.repository_name,
                      "bootstrap_state": run.state if run else "accepted",
                      "command": bootstrap_command(p.proposal_id, str(state_dir))})
    return ready


def cmd_factory_bootstrap(args: argparse.Namespace) -> int:
    """The single governed consumer of an accepted repository proposal (#82)."""
    from howlplane.control_plane.factory import bootstrap
    from howlplane.control_plane.factory.owner_decision import require_state_dir
    require_state_dir(args, "factory bootstrap", "accepted proposal")
    ledger_file = _cli()._resolve_ledger_file(args)
    return bootstrap.run_cli(args, _cli().EvidenceLedger(ledger_file) if ledger_file else None)


def collect_factory_status(args: argparse.Namespace):
    """The one Factory status projection: ``(status, operator, campaign, record, published)``.

    Shared by ``factory status`` and the everyday ``howlplane status``/home screen so
    every surface derives state, health, reason and next action from the same data.
    Read-only apart from the optional redacted snapshot publish.
    """
    from pathlib import Path
    from howlplane.control_plane.factory.campaign import campaign_from_state_dir
    from howlplane.control_plane.factory.repo_proposal import RepoProposalStore
    from howlplane.control_plane.factory.work_item import WorkItemState, WorkItemStore
    # An explicit --state-dir may point anywhere, so only repo-discovered status
    # recommends the short `howlplane approve ID` form that relies on discovery.
    discovered = not getattr(args, "state_dir", None)
    campaign = _cli()._resolve_factory_campaign(args)
    if campaign is None:
        campaign = campaign_from_state_dir(args.state_dir)
    store = _cli()._factory_state_store(args)
    record = store.load(reconcile_restart=False)
    work_store = WorkItemStore(Path(args.state_dir).resolve() / "work_items")
    proposal_store = RepoProposalStore(Path(args.state_dir).resolve() / "repo_proposals")
    parked = [
        {"work_item_id": i.work_item_id, "state": i.state, "blocker": i.admission_blocked_reason, "title": i.title}
        for i in work_store.list_all()
        if i.state in (WorkItemState.AWAITING_OWNER, WorkItemState.BLOCKED, WorkItemState.DEFERRED)
    ]
    proposals = [
        {"proposal_id": p.proposal_id, "repository_name": p.repository_name, "disposition": p.disposition}
        for p in proposal_store.list_awaiting_authority()
    ]
    status = {
        "supervisor_id": record.supervisor_id,
        "state": record.state,
        "objective": record.objective,
        "target_mode": record.target_mode,
        "target_repository": record.target_repository,
        "workspace_file": record.workspace_file,
        "created_at": record.created_at,
        "last_tick_at": record.last_tick_at,
        "last_successful_tick_at": record.last_successful_tick_at,
        "next_wake_at": record.next_wake_at,
        "backoff_reason": getattr(record, "backoff_reason", None),
        "backoff_attempt": getattr(record, "backoff_attempt", None),
        "backoff_delay_seconds": getattr(record, "backoff_delay_seconds", None),
        "current_work_item_id": record.current_work_item_id,
        "current_task_id": record.current_task_id,
        "current_dispatch_id": record.current_dispatch_id,
        "observations_consumed": record.observations_consumed,
        "failure_count": record.failure_count,
        "last_error": record.last_error,
        "stopped_reason": record.stopped_reason,
        "provider_wake_conditions": record.provider_wake_conditions,
        "recent_completed": record.recent_completed,
        "recent_failed": record.recent_failed,
        "dispatch_history_count": len(record.dispatch_history),
        "transition_history_count": len(record.transition_history),
        "run_mode": record.run_mode if hasattr(record, "run_mode") else "continuous",
        "max_work_items": getattr(record, "max_work_items", None),
        "work_items_dispatched": getattr(record, "work_items_dispatched", 0),
        "bounded_run_started_at": getattr(record, "bounded_run_started_at", None),
        "bounded_run_completed_at": getattr(record, "bounded_run_completed_at", None),
        "bounded_run_stop_reason": getattr(record, "bounded_run_stop_reason", None),
        "bounded_dispatched_ids": list(getattr(record, "bounded_dispatched_ids", [])),
        "parked_items": parked,
        "proposals_awaiting_authority": proposals,
        "consecutive_capped_ticks": getattr(record, "consecutive_capped_ticks", 0),
        "alerts": list(getattr(record, "alerts", [])),
        # Additive: the worker comes from the dispatch record, never from logs.
        "worker_resource_id": getattr(record, "current_provider", None),
        "worker": _cli()._worker_display(getattr(record, "current_provider", None)),
        "dispatch_started_at": getattr(record, "current_dispatch_started_at", None),
    }
    if record.current_work_item_id:
        try:
            current_item = work_store.load(record.current_work_item_id)
            status["current_work_title"] = current_item.title
            status["current_attempt"] = current_item.attempts
        except Exception:
            pass
    if campaign is not None:
        from howlplane.control_plane.factory.service import process_status
        from howlplane.control_plane.authority_envelope import ENVELOPE_FILENAME, load_envelope
        profile = None
        envelope_dir = campaign.state_dir / "campaign"
        if (envelope_dir / ENVELOPE_FILENAME).is_file():
            profile = load_envelope(envelope_dir).profile_id
        display_authority = "safe" if profile == "strict" else profile
        status.update({
            "campaign_id": campaign.repository.campaign_id,
            "project": campaign.repository.remote or campaign.repository.root.name,
            "worktree": str(campaign.target_dir),
            "authority": display_authority or "not configured",
            "process": process_status(campaign),
        })
    from howlplane.control_plane.factory.status_publish import publish_cli_status
    published = publish_cli_status(status, args)
    # Local commands name local paths, so they are added after the redacted publish.
    status["decisions"] = (
        (_cli()._owner_decisions(work_store, campaign.target_dir, args.state_dir) if campaign is not None else [])
        + _proposal_decisions(proposal_store, args.state_dir))
    if discovered:
        for decision in status["decisions"]:
            item = decision.get("task_id") or decision.get("work_item_id") or decision.get("proposal_id")
            if item:
                decision["short_approve"] = f"howlplane approve {item}"
                decision["short_reject"] = f"howlplane reject {item}"
    status["bootstrap_ready"] = _bootstrap_ready(proposal_store, args.state_dir)
    from howlplane.control_plane.presentation.operator import derive_operator_status
    operator = derive_operator_status(status)
    return status, operator, campaign, record, published


def cmd_factory_status(args: argparse.Namespace) -> int:
    from howlplane.control_plane.presentation.operator import render_operator_text
    status, operator, campaign, record, published = _cli().collect_factory_status(args)
    parked = status["parked_items"]
    proposals = status["proposals_awaiting_authority"]
    if getattr(args, "json", False):
        import json
        print(json.dumps({**status, "operator": operator.to_dict()}, indent=2, default=str))
    elif not getattr(args, "verbose", False):
        from howlplane.control_plane.presentation.style import resolve_style
        print("\n".join(render_operator_text(operator, status, resolve_style(sys.stdout, _cli()._COLOR_MODE))))
    else:
        print("HowlPlane Factory\n")
        if campaign is not None:
            print(f"Project: {status['project']}")
            print(f"Process: {status['process']}")
            print(f"Target: isolated worktree ({status['worktree']})")
            print(f"Authority: {status['authority']}")
        print(f"State: {status['state']}")
        run_mode = status.get("run_mode", "continuous")
        print(f"Run mode: {run_mode}")
        if status.get("max_work_items") is not None:
            print(f"Work item limit: {status['max_work_items']}")
            print(f"Work items dispatched: {status.get('work_items_dispatched', 0)}")
            remaining = max(0, (status["max_work_items"] or 0) - (status.get("work_items_dispatched") or 0))
            print(f"Remaining: {remaining}")
        if status.get("bounded_run_stop_reason"):
            print(f"Stopped reason: {status['bounded_run_stop_reason']}")
        if record.objective:
            print(f"Objective: {record.objective}")
        if record.target_mode:
            print(f"Target mode: {record.target_mode}")
        if record.target_repository:
            print(f"Target repository: {record.target_repository}")
        print(f"Created: {status['created_at']}")
        print(f"Last tick: {status['last_tick_at']}")
        print(f"Last successful tick: {status['last_successful_tick_at']}")
        print(f"Next wake: {status['next_wake_at']}")
        print(f"Current item: {status['current_work_item_id']}")
        print(f"Current task: {status['current_task_id']}")
        print(f"Current dispatch: {status['current_dispatch_id']}")
        print(f"Observations: {status['observations_consumed']}")
        print(f"Failures: {status['failure_count']}")
        print(f"Last error: {status['last_error']}")
        print(f"Provider wake: {status['provider_wake_conditions']}")
        print(f"Recent completed: {len(status['recent_completed'])}")
        print(f"Recent failed: {len(status['recent_failed'])}")
        if parked:
            print("Parked items:")
            for p in parked:
                print(f"  - {p['work_item_id']}: {p['state']} ({p['blocker'] or 'no blocker'})")
        if proposals:
            print("Proposals awaiting authority:")
            for p in proposals:
                print(f"  - {p['proposal_id']}: {p['repository_name']} ({p['disposition']})")
        alerts = status.get("alerts", [])
        if alerts:
            print("Active alerts:")
            for a in alerts[-5:]:
                print(f"  - [{a.get('type')}] {a.get('message')}")
    if published is not None:
        stream = sys.stderr if getattr(args, "json", False) else sys.stdout
        print(f"Published redacted status: {published}", file=stream)
    return 0


def cmd_factory_stop(args: argparse.Namespace) -> int:
    from howlplane.control_plane.factory.campaign import campaign_from_state_dir
    from howlplane.control_plane.factory.supervisor_state import SupervisorState
    campaign = _cli()._resolve_factory_campaign(args)
    if campaign is None:
        campaign = campaign_from_state_dir(args.state_dir)
    if campaign is not None:
        from howlplane.control_plane.factory.service import stop_process
        # Persisting STOPPED first makes a crash during backend shutdown fail
        # closed. The run loop receives SIGTERM and reconciles its active tick.
        store = _cli()._factory_state_store(args)
        record = store.load(reconcile_restart=False)
        if record.state != SupervisorState.STOPPED:
            record.transition_to(SupervisorState.STOPPED, reason="operator_stop")
            record.stopped_reason = "operator_stop"
            store.save(record)
        everyday = bool(getattr(args, "everyday", False))
        _cli()._print_factory_result(
            "STOPPED", "ok", stop_process(campaign),
            [("Project", campaign.repository.remote or campaign.repository.root.name)],
            "Work and evidence are preserved. Continue when ready:" if everyday
            else "Campaign state and evidence are preserved. Resume when ready:",
            ["howlplane start", "howlplane status"] if everyday
            else ["howlplane factory resume", "howlplane factory status"],
            title="HowlPlane" if everyday else "HowlPlane Factory")
        return 0
    store = _cli()._factory_state_store(args)
    record = store.load()
    everyday = bool(getattr(args, "everyday", False))
    resume_cmd = "howlplane start" if everyday else "howlplane factory resume"
    title = "HowlPlane" if everyday else "HowlPlane Factory"
    if record.state == SupervisorState.STOPPED:
        _cli()._print_factory_result("STOPPED", "ok", "The Factory was already stopped.", [],
                              "Resume when ready:", [resume_cmd], title=title)
        return 0
    record.transition_to(SupervisorState.STOPPED, reason="operator_stop")
    record.stopped_reason = "operator_stop"
    store.save(record)
    _cli()._print_factory_result("STOPPED", "ok", "Factory supervisor stopped. Campaign state is preserved.", [],
                          "Resume when ready:", [resume_cmd], title=title)
    return 0


def cmd_factory_resume(args: argparse.Namespace) -> int:
    from howlplane.control_plane.factory.supervisor_state import SupervisorState
    # Discover the repository's campaign like every other Factory command; without
    # this, resume failed with a TypeError unless --state-dir was given.
    _cli()._resolve_factory_campaign(args)
    store = _cli()._factory_state_store(args)
    record = store.load()
    if record.state != SupervisorState.STOPPED:
        from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
        raise OperatorFailure(OperatorError(
            "FACTORY_NOT_STOPPED", "The Factory is not stopped, so there is nothing to resume.",
            f"Its current state is {record.state}.", "Check what it is doing.", "howlplane factory status"))
    record.transition_to(SupervisorState.IDLE, reason="operator_resume")
    record.stopped_reason = None
    store.save(record)
    running = False
    try:
        campaign = _cli()._resolve_factory_campaign(args)
        if campaign is not None:
            from howlplane.control_plane.factory.service import process_status
            running = process_status(campaign) == "running"
    except Exception:
        running = False
    next_commands = ["howlplane factory status"] if running else ["howlplane factory start"]
    note = ("Campaign state restored to IDLE; the running Factory continues."
            if running else "Campaign state restored to IDLE. The Factory process is not running; start it:")
    _cli()._print_factory_result("RESUMED", "ok", "Campaign resumed.", [], note, next_commands)
    return 0


def cmd_factory_start(args: argparse.Namespace) -> int:
    """Normal persistent Factory entrypoint over the existing run loop."""
    from howlplane.control_plane.factory.service import start_process
    from howlplane.control_plane.presentation.style import phase
    quiet = bool(getattr(args, "json", False))

    phase("Checking providers", enabled=not quiet, mode=_cli()._COLOR_MODE)
    refused = _cli()._factory_preflight(args)
    if refused is not None:
        return refused
    phase("Preparing isolated worktree", enabled=not quiet, mode=_cli()._COLOR_MODE)
    campaign = _cli()._resolve_factory_campaign(args, prepare=True, force_resolve=True)
    profile = _cli()._select_factory_authority(args, campaign)
    # Bind the selected existing envelope before detaching. This keeps the
    # operator choice durable even if the new backend exits before its first
    # tick, while still using the same supervisor builder and authority path.
    _cli()._build_factory_supervisor(args)
    start_kwargs = {}
    if getattr(args, "max_work_items", None) is not None:
        start_kwargs["max_work_items"] = args.max_work_items
    phase("Starting Factory", enabled=not quiet, mode=_cli()._COLOR_MODE)
    started, record = start_process(
        campaign, profile, getattr(args, "objective", None), **start_kwargs
    )
    display_authority = "safe" if profile == "strict" else profile
    if getattr(args, "json", False):
        import json
        print(json.dumps({"started": started, "campaign_id": campaign.repository.campaign_id,
                          "state_dir": str(campaign.state_dir), "target_repo": str(campaign.target_dir),
                          "backend": record.backend, "authority": display_authority}, indent=2))
        return 0
    project = campaign.repository.remote or campaign.repository.root.name
    rows = [("Project", project), ("Authority", display_authority)]
    if getattr(args, "verbose", False):
        rows += [("Backend", record.backend), ("Target", str(campaign.target_dir))]
    everyday = bool(getattr(args, "everyday", False))
    title = "HowlPlane" if everyday else "HowlPlane Factory"
    if not started:
        _cli()._print_factory_result("RUNNING", "ok", "HowlPlane was already running. Nothing changed." if everyday
                              else "The Factory was already running. Nothing changed.", rows,
                              "See what it is doing:", ["howlplane status" if everyday else "howlplane factory status"],
                              title=title)
        return 0
    if campaign.repository.dirty:
        rows.append(("Checkout", "has local changes; it will not be modified"))
    commands = (["howlplane status", "howlplane logs --follow", "howlplane stop"] if everyday else
                ["howlplane factory status", "howlplane factory logs --follow", "howlplane factory stop"])
    _cli()._print_factory_result("STARTED", "ok", "HowlPlane started working in an isolated worktree." if everyday
                          else "Factory started in an isolated worktree.", rows, "Next:", commands, title=title)
    return 0


def _print_factory_result(badge: str, severity: str, message: str, rows: List[tuple], next_note: str,
                          commands: List[str], title: str = "HowlPlane Factory") -> None:
    """Shared confirmation block for mutating Factory commands."""
    from howlplane.control_plane.presentation.style import resolve_style
    style = resolve_style(sys.stdout, _cli()._COLOR_MODE)
    lines = style.header(title, badge, severity)
    lines.append(message)
    kv = style.kv(rows)
    if kv:
        lines += [""] + kv
    lines += ["", style.section(next_note.rstrip(":") if not commands else next_note)]
    lines += [style.command_block(c) for c in commands]
    print("\n".join(lines))


def cmd_factory_logs(args: argparse.Namespace) -> int:
    from howlplane.control_plane.factory.campaign import campaign_from_state_dir
    from howlplane.control_plane.factory import logs_cli
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    campaign = _cli()._resolve_factory_campaign(args)
    if campaign is None:
        campaign = campaign_from_state_dir(args.state_dir)
    if campaign is None:
        raise OperatorFailure(OperatorError(
            "FACTORY_NO_CAMPAIGN", "Factory logs need a repository to look up.",
            "No repository or state directory was given and the current directory is not a Git repository.",
            "Run it inside the repository, or pass --target-repo PATH."))
    return logs_cli.command(campaign, args)


def _print_worker_table(report: Dict[str, Any]) -> None:
    from howlplane.control_plane.presentation.doctor import render_worker_summary
    from howlplane.control_plane.presentation.style import resolve_style
    agents = report.get("agents")
    if agents:
        print("\n".join(render_worker_summary(agents, report.get("workspace"), resolve_style(sys.stdout, _cli()._COLOR_MODE))))


def cmd_factory_doctor(args: argparse.Namespace) -> int:
    from howlplane.control_plane.factory.campaign import CampaignError
    from howlplane.control_plane.factory.service import _systemd_available, process_status
    report = _cli()._factory_readiness(live=getattr(args, "live", False), workspace=_cli()._factory_workspace(args))
    blocked = 1 if report["readiness"]["status"] == "BLOCKED" else 0
    if getattr(args, "json", False):
        print(json.dumps({"schema": agent_readiness.SCHEMA, **report}, indent=2))
        return blocked
    try:
        campaign = _cli()._resolve_factory_campaign(args)
        if campaign is None:
            print("Factory doctor: explicit state and target configuration accepted.\n")
            _cli()._print_worker_table(report)
            print(agent_readiness.render_factory(report["readiness"], report["execution_budget"]), end="")
            return blocked
        target_state = "missing"
        if campaign.target_dir.exists():
            try:
                from howlplane.control_plane.factory.campaign import _validate_target
                _validate_target(campaign)
                target_state = "healthy"
            except CampaignError as exc:
                target_state = f"unhealthy: {exc}"
        print("HowlPlane Factory doctor")
        print(f"Repository: healthy ({campaign.repository.root})")
        print(f"Campaign: {campaign.repository.campaign_id}")
        print(f"Worktree: {target_state}")
        print(f"State directory: {campaign.state_dir}")
        print(f"Process: {process_status(campaign)}")
        print(f"Backend: {'systemd user service' if _systemd_available() else 'portable detached process'}")
        print()
        _cli()._print_worker_table(report)
        print(agent_readiness.render_factory(report["readiness"], report["execution_budget"]), end="")
        return blocked
    except CampaignError as exc:
        from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
        raise OperatorFailure(OperatorError(
            "CAMPAIGN_ERROR", f"Factory doctor: cannot start safely: {exc}",
            "The Factory cannot safely use this repository as configured.",
            "Fix the problem above, then run the check again.", "howlplane factory doctor")) from exc


def cmd_factory(args: argparse.Namespace) -> int:
    try:
        action = getattr(args, "factory_action", None)
        if action == "run-once":
            return _cli().cmd_factory_run_once(args)
        if action == "run":
            return _cli().cmd_factory_run(args)
        if action == "status":
            return _cli().cmd_factory_status(args)
        if action == "pending":
            from howlplane.control_plane.factory import remote_cli
            return remote_cli.cmd_factory_pending(args)
        if action == "snapshot":
            from howlplane.control_plane.factory import remote_cli
            return remote_cli.cmd_factory_snapshot(args)
        if action == "stop":
            return _cli().cmd_factory_stop(args)
        if action == "resume":
            return _cli().cmd_factory_resume(args)
        if action == "start":
            return _cli().cmd_factory_start(args)
        if action == "logs":
            return _cli().cmd_factory_logs(args)
        if action == "prepare":
            from howlplane.control_plane.factory import prepare
            return prepare.command(args)
        if action == "doctor":
            return _cli().cmd_factory_doctor(args)
        if action == "bootstrap":
            return _cli().cmd_factory_bootstrap(args)
        if action == "canary":
            args.max_work_items = 1
            if not getattr(args, "until", None):
                args.until = None
            return _cli().cmd_factory_run(args)
        if action == "queue":
            from howlplane.control_plane.factory import task_queue
            return task_queue.command(args)
        print("Unknown factory action.")
        return 1
    except (OSError, ValueError) as exc:
        from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
        raise OperatorFailure(OperatorError(
            "FACTORY_INPUT_ERROR", f"Factory: {exc}",
            "The Factory command could not use the repository, state directory or arguments given.",
            "Check the arguments and the repository, then run the readiness check.",
            "howlplane factory doctor")) from exc
