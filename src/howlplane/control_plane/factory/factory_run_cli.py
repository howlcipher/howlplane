"""Factory run handlers and the shared ``_factory_*`` helpers.

Moved out of ``cli.py`` (backlog #78). ``cli.py`` re-exports every name and these
handlers look shared helpers up on the ``cli`` module at call time, so existing
imports and test monkeypatches of ``cli.<name>`` keep working unchanged.
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _cli():
    from howlplane.control_plane import cli
    return cli


def _resolve_factory_campaign(
    args: argparse.Namespace, *, prepare: bool = False, force_resolve: bool = False
):
    """Resolve the zero-configuration campaign and attach its paths to args.

    When no explicit state directory is given, prefer any currently-running
    campaign for this repository over a stopped historical one. Explicit
    --state-dir always wins and is surfaced directly in args.state_dir.
    """
    from howlplane.control_plane.factory.campaign import prepare_campaign, resolve_campaign

    # Preserve the established explicit interface exactly. It can point at a
    # deliberately prepared target unrelated to the caller's current checkout.
    if (not force_resolve and getattr(args, "state_dir", None)
            and getattr(args, "target_repo", None)):
        return None
    if not force_resolve and getattr(args, "state_dir", None):
        # Historical commands accepted only --state-dir and used the caller's
        # checkout. Keep that advanced/debug contract intact.
        args.target_repo = "."
        return None

    is_bounded = (getattr(args, "max_work_items", None) is not None and args.max_work_items > 0) or getattr(args, "factory_action", None) == "canary"
    campaign = resolve_campaign(
        getattr(args, "repo", None),
        state_dir=getattr(args, "state_dir", None),
        target_repo=getattr(args, "target_repo", None),
        prefer_active=True,
        bounded=is_bounded if prepare else False,
    )
    if prepare:
        campaign = prepare_campaign(campaign)
    args.state_dir = str(campaign.state_dir)
    # The supervisor must always operate on the isolated target for convenience
    # commands. Explicit target-repo remains an advanced override, validated by
    # prepare_campaign against the discovered source repository.
    args.target_repo = str(campaign.target_dir)
    return campaign


def _select_factory_authority(args: argparse.Namespace, campaign: Any) -> Optional[str]:
    """Obtain explicit first-run authority without inventing a new policy model."""
    from howlplane.control_plane.authority_envelope import ENVELOPE_FILENAME, load_envelope
    from howlplane.control_plane.authority_profile import CANONICAL_PROFILES

    envelope_dir = campaign.state_dir / "campaign"
    existing_profile = None
    if (envelope_dir / ENVELOPE_FILENAME).is_file():
        existing_profile = load_envelope(envelope_dir).profile_id

    requested = getattr(args, "authority_profile", None)
    choice = getattr(args, "authority", None)
    if choice:
        remote = campaign.repository.remote
        # Resolve only profiles already approved for this exact repository.
        # `strict` is intentionally universal but loses to any repository grant.
        compatible = [
            profile for profile in CANONICAL_PROFILES.values()
            if not profile.authorized_repositories
            or any(remote.endswith(repository) for repository in profile.authorized_repositories)
        ]
        delegated = [profile for profile in compatible if profile.authorized_repositories]
        # Explicit, auditable ordering of already-granted authority. This does
        # not construct or broaden a profile.
        strongest = max(
            delegated or compatible,
            key=lambda profile: (
                len(profile.allowed_action_classes), profile.max_merges,
                profile.ttl_hours, profile.profile_id,
            ),
            default=None,
        )
        mappings = {
            "safe": "strict",
            "standard": strongest.profile_id if strongest else None,
            "autonomous": strongest.profile_id if strongest else None,
        }
        requested = mappings[choice]
        if choice != "safe" and not delegated:
            raise ValueError(
                "No existing delegated authority profile is compatible with this repository. "
                "Use --authority safe, or configure an approved --authority-profile."
            )

    if existing_profile is not None:
        if requested is not None and requested != existing_profile:
            display_choice = choice or getattr(args, "authority_profile", None)
            raise ValueError(
                f"Requested authority '{display_choice}' ({requested}) conflicts with existing "
                f"campaign authority envelope '{existing_profile}'. Startup refused to prevent "
                "silent authority override."
            )
        args.authority_profile = existing_profile
        return existing_profile

    if requested:
        args.authority_profile = requested
        return requested

    if not sys.stdin.isatty():
        raise ValueError(
            "Factory needs an explicit authority choice in a non-interactive environment. "
            "Use --authority safe or --authority-profile strict."
        )
    print("Choose Factory authority:\n  1. Safe: no consequential Git or GitHub actions.\n"
          "  2. Standard: use an existing repository-compatible delegated profile.\n"
          "  3. Autonomous: use the most permissive existing compatible profile.\n")
    answer = input("Choice [1]: ").strip() or "1"
    choices = {"1": "safe", "2": "standard", "3": "autonomous"}
    if answer not in choices:
        raise ValueError("Authority choice must be 1, 2, or 3")
    args.authority = choices[answer]
    return _select_factory_authority(args, campaign)


def _build_factory_supervisor(args: argparse.Namespace, sleep: Any = None):
    from datetime import datetime, timezone
    from pathlib import Path
    import getpass
    import socket
    import time

    from howlplane.control_plane.authority_envelope import (
        ENVELOPE_FILENAME,
        create_envelope,
        load_envelope,
        save_envelope,
    )
    from howlplane.control_plane.authority_profile import get_profile
    from howlplane.control_plane.backlog_source import BacklogSource, source_file_rank
    from howlplane.control_plane.factory.dispatcher import MarathonDispatcherAdapter
    from howlplane.control_plane.factory.repo_proposal import CapabilityStore, RepoProposalStore
    from howlplane.control_plane.factory.supervisor import FactorySupervisor
    from howlplane.control_plane.factory.supervisor_state import SupervisorStateRecord, SupervisorStateStore
    from howlplane.control_plane.factory.target import FactoryTarget, FactoryTargetMode, Workspace
    from howlplane.control_plane.factory.work_item import WorkItemStore
    from howlplane.control_plane.git_integration import detect_repo_slug
    from howlplane.control_plane.synthesis import MarathonDogfoodEngine
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

    state_dir = Path(args.state_dir).resolve()
    state_store = SupervisorStateStore(state_dir / "supervisor")
    work_item_store = WorkItemStore(state_dir / "work_items")
    repo_proposal_store = RepoProposalStore(state_dir / "repo_proposals")
    capability_store = CapabilityStore(state_dir / "capabilities")
    target_repo = Path(getattr(args, "target_repo", None) or ".").resolve()
    target_mode = getattr(args, "target", "repo")
    workspace_path = getattr(args, "workspace", None)
    objective = getattr(args, "objective", None)

    target = FactoryTarget(
        mode=FactoryTargetMode(target_mode),
        target_repo=target_repo,
        workspace=Workspace.from_file(workspace_path) if workspace_path else None,
        controller_checkout=Path.cwd().resolve(),
    )
    if target.mode == FactoryTargetMode.SELF:
        target.ensure_isolated_self_target()

    # Persist campaign objective and target metadata so they survive restart.
    state_record = state_store.load()
    if objective is not None:
        state_record.objective = objective
    state_record.target_mode = target_mode
    state_record.target_repository = str(target_repo)
    state_record.workspace_file = str(Path(workspace_path).resolve()) if workspace_path else None
    state_store.save(state_record)

    def _backlog_rank(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _discover_repo(repo_path: Path, repo_name: str):
        try:
            source = BacklogSource(repo_path)
            selection = source.select()
        except Exception:
            return []
        return [
            {
                "origin": "existing_backlog",
                "repository": repo_name,
                "title": item.title,
                "description": source.item_detail(item),
                "identity_keys": [item.source_file, item.item_id],
                "evidence_refs": [f"{item.source_file}#{item.item_id}"],
                "evidence_fingerprints": [f"backlog:{item.source_file}:{item.item_id}"],
                "source_file_rank": source_file_rank(item),
                "source_rank": _backlog_rank(item.item_id),
                "kind": item.kind,
            }
            for item in selection.eligible
        ]

    def _discovery():
        if target.mode == FactoryTargetMode.ECOSYSTEM:
            if target.workspace is None:
                return []
            evidence: List[Dict[str, Any]] = []
            for repo in target.workspace.repositories:
                repo_name = repo.repository or detect_repo_slug(repo.path) or str(repo.path)
                evidence.extend(_discover_repo(repo.path, repo_name))
            return evidence

        repo = detect_repo_slug(target_repo) or str(target_repo)
        return _discover_repo(target_repo, repo)

    provider_pool = ProviderPoolManager.from_config(probe_on_start=False)

    # Bind operator-selected delegated authority once.  Without an authority
    # profile the engine truthfully parks anything requiring authority rather
    # than silently renewing or expanding its own grant per tick.
    authority_profile_id = getattr(args, "authority_profile", None)
    envelope = None
    campaign_dir = state_dir / "campaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    envelope_path = campaign_dir / ENVELOPE_FILENAME
    if envelope_path.is_file():
        envelope = load_envelope(campaign_dir)
        if authority_profile_id and envelope.profile_id != authority_profile_id:
            raise ValueError(
                "Requested authority profile does not match the saved "
                f"envelope: {authority_profile_id!r} != {envelope.profile_id!r}"
            )
    elif authority_profile_id:
        operator_origin = f"cli:{getpass.getuser()}@{socket.gethostname()}"
        envelope = create_envelope(
            get_profile(authority_profile_id), "FACTORY-CAMPAIGN", operator_origin
        )
        save_envelope(envelope, campaign_dir)

    engine = MarathonDogfoodEngine(
        provider_pool=provider_pool,
        target_repo=target_repo,
        repo_slug=detect_repo_slug(target_repo) or "",
    )
    engine.authority_envelope = envelope
    if envelope is not None:
        engine.git_executor = engine._git_executor_factory(
            envelope, state_store.load().merges_count
        )

    dispatcher = MarathonDispatcherAdapter(engine_factory=lambda: engine)

    return FactorySupervisor(
        state_store=state_store,
        work_item_store=work_item_store,
        repo_proposal_store=repo_proposal_store,
        capability_store=capability_store,
        dispatcher=dispatcher,
        discovery=_discovery,
        provider_pool=provider_pool,
        policy=None,
        product_repo=getattr(args, "product_repo", None),
        clock=lambda: datetime.now(timezone.utc),
        sleep=sleep or time.sleep,
        state_dir=state_dir,
        lock=None,
        max_work_items=getattr(args, "max_work_items", None),
    )


def cmd_factory_run_once(args: argparse.Namespace) -> int:
    campaign = _cli()._resolve_factory_campaign(args, prepare=True)
    if campaign is not None:
        _cli()._select_factory_authority(args, campaign)
    supervisor = _cli()._build_factory_supervisor(args)
    result = supervisor.run_once()
    status = supervisor.status()
    if getattr(args, "json", False):
        import json
        print(json.dumps({"tick": result.__dict__, "status": status}, indent=2, default=str))
    else:
        print(f"State: {status['state']}")
        print(f"Next wake: {status['next_wake_at']}")
        print(f"Reason: {result.reason}")
        print(f"Observations consumed: {status['observations_consumed']}")
        if result.selected_work_item_id:
            print(f"Selected: {result.selected_work_item_id}")
    return 0


def cmd_factory_run(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone
    import signal
    import threading

    refused = _cli()._factory_preflight(args)
    if refused is not None:
        return refused
    campaign = _cli()._resolve_factory_campaign(args, prepare=True)
    if campaign is not None:
        _cli()._select_factory_authority(args, campaign)
    wake = threading.Event()
    supervisor = _cli()._build_factory_supervisor(args, sleep=wake.wait)

    def request_stop(signum, _frame):
        supervisor.request_stop(f"signal_{signal.Signals(signum).name.lower()}")
        wake.set()

    until = None
    if getattr(args, "until", None):
        until = datetime.now(timezone.utc) + timedelta(seconds=args.until)
    previous = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, request_stop)
        if getattr(args, "resume_stopped", False):
            supervisor.run(until=until, resume_stopped=True)
        else:
            supervisor.run(until=until)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    status = supervisor.status()
    if getattr(args, "json", False):
        import json
        print(json.dumps({"status": status}, indent=2, default=str))
    else:
        print(f"Factory stopped. State: {status['state']}")
        print(f"Run mode: {status.get('run_mode', 'continuous')}")
        if status.get('max_work_items') is not None:
            print(f"Work item limit: {status['max_work_items']}")
            print(f"Work items dispatched: {status.get('work_items_dispatched', 0)}")
            remaining = max(0, (status['max_work_items'] or 0) - (status.get('work_items_dispatched') or 0))
            print(f"Remaining: {remaining}")
        print(f"Stopped reason: {status['stopped_reason']}")
        print(f"Dispatches: {status['dispatch_history_count']}")
        print(f"Completed: {len(status['recent_completed'])}")
        print(f"Failed: {len(status['recent_failed'])}")
    return 0


def _factory_state_store(args: argparse.Namespace):
    from pathlib import Path
    from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
    return SupervisorStateStore(Path(args.state_dir).resolve() / "supervisor")


def _factory_workspace(args: argparse.Namespace) -> Optional[str]:
    """The directory Factory workers will run in, without creating anything."""
    from howlplane.control_plane.factory.campaign import CampaignError, resolve_campaign
    if getattr(args, "state_dir", None) and getattr(args, "target_repo", None):
        return str(Path(args.target_repo).expanduser().resolve())
    try:
        bounded = (getattr(args, "max_work_items", None) or 0) > 0 or getattr(args, "factory_action", None) == "canary"
        return str(resolve_campaign(state_dir=getattr(args, "state_dir", None), target_repo=getattr(args, "target_repo", None),
                                    prefer_active=True, bounded=bounded).target_dir)
    except (CampaignError, OSError):
        return None


def _factory_readiness(live: bool = False, workspace: Optional[str] = None) -> dict:
    """Agent readiness for Factory; with a workspace, trust there decides who can work."""
    from howlplane.control_plane.orchestration import default_execution_budget
    summaries = _cli().agent_readiness.evaluate(live=live, workspace=workspace if live and workspace and Path(workspace).is_dir() else None)
    report = _cli().agent_readiness.workspace_report(workspace) if workspace else None
    return {"readiness": _cli().agent_readiness.factory_readiness(summaries, report),
            "execution_budget": default_execution_budget(), "agents": summaries, "workspace": report}


def _factory_preflight(args: argparse.Namespace) -> Optional[int]:
    """Level-1 agent and workspace readiness before a campaign.

    No worker is dispatched until trust for the Factory workspace is known.
    `require` refuses to start when no implementation worker can run there
    unattended; agents that would meet a trust prompt are excluded, not fatal.
    """
    mode = getattr(args, "preflight", "off")
    if mode == "off":
        return None
    report = _cli()._factory_readiness(workspace=_cli()._factory_workspace(args))
    readiness = report["readiness"]
    if readiness["status"] != "READY":
        print(_cli().agent_readiness.render_factory(readiness, report["execution_budget"]), end="", file=sys.stderr)
    if mode == "require" and readiness["status"] == "BLOCKED":
        print("Factory preflight: no autonomous implementation worker is usable; not starting.", file=sys.stderr)
        return 1
    return None
