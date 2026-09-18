#!/usr/bin/env python3
"""Tests for the factory CLI subcommands."""

from datetime import datetime, timezone
import json
from pathlib import Path
import socket

import pytest

from src.control_plane.cli import main
from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore


@pytest.mark.parametrize("json_output", [False, True])
def test_status_observes_active_dispatch_without_restarting(tmp_path, capsys, json_output):
    store = SupervisorStateStore(tmp_path / "supervisor")
    record = store.load()
    record.transition_to(SupervisorState.DISPATCHING, reason="item_selected")
    record.record_dispatch("D-live", "WI-live", "TASK-live", record.created_at)
    record.failure_count = 4
    store.save(record)
    state_path = tmp_path / "supervisor" / "factory_supervisor.json"
    original = state_path.read_bytes()

    args = ["factory", "status", "--state-dir", str(tmp_path)]
    assert main(args + (["--json"] if json_output else [])) == 0
    output = capsys.readouterr().out
    if json_output:
        status = json.loads(output)
        assert status["state"] == "dispatching"
        assert status["failure_count"] == 4
        assert status["current_dispatch_id"] == "D-live"
        assert status["last_error"] is None
    else:
        assert "State: dispatching" in output
        assert "Failures: 4" in output
        assert "Current dispatch: D-live" in output
        assert "Restart during dispatch" not in output
    assert state_path.read_bytes() == original
    # A genuine startup still reconciles interrupted work, retaining identity.
    recovered = store.load()
    assert recovered.state == SupervisorState.BACKOFF_AFTER_FAILURE
    assert recovered.failure_count == 5
    assert recovered.current_dispatch_id == "D-live"


class FakeSupervisor:
    def __init__(self):
        self.state = SupervisorState.IDLE
        self.stopped = False
        self.resumed = False

    def tick(self):
        class Result:
            selected_work_item_id = None
            reason = "tick"
        self.state = SupervisorState.WAITING_FOR_WORK
        return Result()

    def run_once(self):
        return self.tick()

    def status(self):
        return {
            "state": self.state,
            "last_tick_at": None,
            "next_wake_at": datetime.now(timezone.utc).isoformat(),
            "current_work_item_id": None,
            "current_task_id": None,
            "current_dispatch_id": None,
            "observations_consumed": 0,
            "failure_count": 0,
            "stopped_reason": None,
            "dispatch_history_count": 0,
            "transition_history_count": 0,
            "admission_decisions_count": 0,
            "proposals_awaiting_authority": [],
            "recent_completed": [],
            "recent_failed": [],
            "provider_wake_conditions": {},
            "parked_items": [],
        }

    def stop(self, reason="operator_stop"):
        self.stopped = True
        self.state = SupervisorState.STOPPED

    def resume(self):
        self.resumed = True
        self.state = SupervisorState.IDLE

    def run(self, until=None):
        self.state = SupervisorState.STOPPED


def test_factory_status_creates_default_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: FakeSupervisor(),
    )
    code = main(["factory", "status", "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    assert code == 0
    assert "idle" in captured.out


def test_factory_stop_and_resume(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    code = main(["factory", "stop", "--state-dir", str(state_dir)])
    assert code == 0
    store = SupervisorStateStore(state_dir / "supervisor")
    assert store.load().state == SupervisorState.STOPPED

    code = main(["factory", "resume", "--state-dir", str(state_dir)])
    assert code == 0
    assert store.load().state == SupervisorState.IDLE


def test_factory_run_once_invokes_tick(tmp_path, monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: supervisor,
    )
    code = main(["factory", "run-once", "--state-dir", str(tmp_path / "state"), "--authority", "safe"])
    assert code == 0
    assert supervisor.state == SupervisorState.WAITING_FOR_WORK


def test_factory_run_invokes_loop(tmp_path, monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: supervisor,
    )
    code = main(["factory", "run", "--state-dir", str(tmp_path / "state"), "--until", "0.1"])
    assert code == 0
    assert supervisor.state == SupervisorState.STOPPED


def test_build_factory_supervisor_binds_authority_envelope_once(tmp_path):
    from types import SimpleNamespace

    from src.control_plane.cli import _build_factory_supervisor

    args = SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        target_repo=".",
        target="repo",
        objective=None,
        workspace=None,
        authority_profile="strict",
    )
    supervisor = _build_factory_supervisor(args)
    engine = supervisor.dispatcher._engine_factory()
    assert engine.authority_envelope is not None
    assert engine.authority_envelope.profile_id == "strict"
    # Authority is bound once on the same engine instance; repeated factory calls
    # reuse it rather than renewing.
    engine2 = supervisor.dispatcher._engine_factory()
    assert engine2 is engine
    assert engine2.authority_envelope is engine.authority_envelope


def test_build_factory_supervisor_persists_objective(tmp_path):
    from types import SimpleNamespace

    from src.control_plane.cli import _build_factory_supervisor
    from src.control_plane.factory.supervisor_state import SupervisorStateStore

    args = SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        target_repo=".",
        target="repo",
        objective="Improve reliability",
        workspace=None,
        authority_profile=None,
    )
    _build_factory_supervisor(args)
    record = SupervisorStateStore(tmp_path / "state" / "supervisor").load()
    assert record.objective == "Improve reliability"
    assert record.target_mode == "repo"


def test_build_factory_supervisor_self_target_rejects_controller_checkout(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from src.control_plane.cli import _build_factory_supervisor

    # The controller checkout is Path.cwd(); pointing target-repo at cwd must fail.
    args = SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        target_repo=str(Path.cwd()),
        target="self",
        objective=None,
        workspace=None,
        authority_profile=None,
    )
    with pytest.raises(ValueError) as exc:
        _build_factory_supervisor(args)
    assert "refuses to run against the controller checkout" in str(exc.value)


def test_factory_start_uses_zero_config_campaign_and_is_idempotent(tmp_path, monkeypatch, capsys):
    """The product command resolves state/worktree, then reuses an active process."""
    from types import SimpleNamespace

    from tests._factory_test_helpers import make_git_repo, set_xdg_paths

    repo = make_git_repo(tmp_path, readme_text="# Bugs\n")
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    calls = []

    def fake_start(campaign, authority, objective):
        calls.append((campaign, authority, objective))
        return len(calls) == 1, SimpleNamespace(backend="process")

    monkeypatch.setattr("src.control_plane.factory.service.start_process", fake_start)
    assert main(["factory", "start", "--authority", "safe"]) == 0
    assert main(["factory", "start", "--authority", "safe"]) == 0
    assert len(calls) == 2
    assert calls[0][0].target_dir != repo
    assert calls[0][1] == "strict"
    assert "already running" in capsys.readouterr().out


def test_factory_status_prefers_active_campaign_over_stopped_historical(tmp_path, monkeypatch, capsys):
    """Default status must report the active campaign, not an older stopped one."""
    from types import SimpleNamespace
    from src.control_plane.factory.campaign import resolve_campaign, prepare_campaign, _is_process_active_for_state_dir
    from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore
    from tests._factory_test_helpers import make_git_repo, set_xdg_paths

    repo = make_git_repo(tmp_path, readme_text="# Active\n")
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)

    # Historical campaign: stopped.
    old = resolve_campaign(prefer_active=True)
    old_store = SupervisorStateStore(old.state_dir / "supervisor")
    old_rec = old_store.load()
    old_rec.transition_to(SupervisorState.STOPPED, reason="old_stop")
    old_store.save(old_rec)

    # New explicit campaign for the same repository: running.
    new_state = tmp_path / "state" / "howlplane" / "factory" / "howlframe-safe-20260915-144921"
    new_state.mkdir(parents=True, exist_ok=True)
    new_target = tmp_path / "data" / "howlplane" / "worktrees" / "howlframe-safe-20260915-144921" / "target"
    new_target.mkdir(parents=True, exist_ok=True)
    (new_target / "README.md").write_text("# New target\n", encoding="utf-8")

    metadata = {
        "schema": "howlplane.factory.campaign/v1",
        "campaign_id": old.repository.campaign_id,
        "repository_root": str(repo),
        "remote": old.repository.remote,
        "default_branch": "main",
        "source_common_git_dir": str(repo / ".git"),
        "target_repo": str(new_target),
    }
    (new_state / "campaign.json").write_text(json.dumps(metadata), encoding="utf-8")
    new_store = SupervisorStateStore(new_state / "supervisor")
    new_rec = new_store.load()
    # A freshly-created supervisor record is already idle.
    new_store.save(new_rec)

    # Simulate an active process record for the new campaign.
    process_record = {
        "pid": 123456,
        "hostname": socket.gethostname(),
        "process_create_time": 1.0,
        "backend": "process",
        "command": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "log_path": str(new_state / "campaign.log"),
    }
    (new_state / "campaign").mkdir(parents=True, exist_ok=True)
    (new_state / "campaign" / "process.json").write_text(json.dumps(process_record), encoding="utf-8")

    def fake_active(state_dir):
        return str(Path(state_dir).resolve()) == str(new_state.resolve())
    monkeypatch.setattr("src.control_plane.factory.campaign._is_process_active_for_state_dir", fake_active)

    # Default status from the repository should resolve to the new campaign.
    resolved = resolve_campaign(prefer_active=True)
    assert resolved.state_dir == new_state
    assert resolved.target_dir == new_target


def test_factory_status_rejects_multiple_active_campaigns(tmp_path, monkeypatch):
    """Multiple active campaigns for the same repo must be reported, not guessed."""
    from src.control_plane.factory.campaign import resolve_campaign, CampaignError, discover_repository
    from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore
    from tests._factory_test_helpers import make_git_repo, set_xdg_paths

    repo = make_git_repo(tmp_path, readme_text="# Ambiguous\n")
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    repo_identity = discover_repository()

    base = tmp_path / "state" / "howlplane" / "factory"
    active_dirs = ["active-a", "active-b"]
    for name in active_dirs:
        state_dir = base / name
        state_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "data" / "howlplane" / "worktrees" / name / "target"
        target.mkdir(parents=True, exist_ok=True)
        (state_dir / "campaign.json").write_text(json.dumps({
            "schema": "howlplane.factory.campaign/v1",
            "campaign_id": repo_identity.campaign_id,
            "repository_root": str(repo_identity.root),
            "remote": repo_identity.remote,
            "default_branch": repo_identity.default_branch,
            "source_common_git_dir": str(repo_identity.common_git_dir),
            "target_repo": str(target),
        }), encoding="utf-8")
        store = SupervisorStateStore(state_dir / "supervisor")
        rec = store.load()
        # A freshly-created supervisor record starts as idle.
        store.save(rec)

    monkeypatch.setattr("src.control_plane.factory.campaign._is_process_active_for_state_dir", lambda _s: True)
    with pytest.raises(CampaignError) as exc:
        resolve_campaign(prefer_active=True)
    assert "Multiple active Factory campaigns" in str(exc.value)
    assert "active-a" in str(exc.value)
    assert "active-b" in str(exc.value)


def test_factory_service_unit_name_reflects_explicit_state_dir(tmp_path):
    """A non-default state directory gets its own systemd unit name."""
    from types import SimpleNamespace
    from src.control_plane.factory.service import _unit_name
    from src.control_plane.factory.campaign import FactoryCampaign, RepositoryIdentity

    repo = RepositoryIdentity(
        root=tmp_path,
        remote="https://github.com/owner/repo.git",
        default_branch="main",
        commit="abc",
        dirty=False,
        common_git_dir=tmp_path,
        campaign_id="abc123",
    )
    default_campaign = FactoryCampaign(repo, tmp_path / "abc123", tmp_path / "target")
    explicit_campaign = FactoryCampaign(repo, tmp_path / "repo-safe-20260915-144921", tmp_path / "target")

    assert _unit_name(default_campaign) == "howlplane-factory-abc123"
    assert _unit_name(explicit_campaign) == "howlplane-factory-repo-safe-20260915-144921"


def test_factory_status_explicit_state_dir_wins_without_repository(tmp_path, monkeypatch):
    """--state-dir alone resolves the campaign from saved metadata."""
    from src.control_plane.factory.campaign import campaign_from_state_dir
    from tests._factory_test_helpers import make_git_repo

    repo = make_git_repo(tmp_path)
    state_dir = tmp_path / "explicit-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (state_dir / "campaign.json").write_text(json.dumps({
        "schema": "howlplane.factory.campaign/v1",
        "campaign_id": "x123",
        "repository_root": str(repo),
        "remote": "https://github.com/owner/repo.git",
        "default_branch": "main",
        "source_common_git_dir": str(repo / ".git"),
        "target_repo": str(target),
    }), encoding="utf-8")

    campaign = campaign_from_state_dir(state_dir)
    assert campaign is not None
    assert campaign.state_dir == state_dir
    assert campaign.target_dir == target


def test_factory_run_once_requires_explicit_noninteractive_authority(tmp_path, monkeypatch):
    from tests._factory_test_helpers import make_git_repo, set_xdg_paths

    repo = make_git_repo(tmp_path, readme_text="# Bugs\n")
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    assert main(["factory", "run-once"]) == 1


def test_factory_cli_parses_max_work_items_and_canary():
    from src.control_plane.cli import build_parser

    parser = build_parser()
    run_args = parser.parse_args(["factory", "run", "--max-work-items", "5"])
    assert run_args.factory_action == "run"
    assert run_args.max_work_items == 5

    canary_args = parser.parse_args(["factory", "canary"])
    assert canary_args.factory_action == "canary"

    start_args = parser.parse_args(["factory", "start", "--max-work-items", "3"])
    assert start_args.factory_action == "start"
    assert start_args.max_work_items == 3

