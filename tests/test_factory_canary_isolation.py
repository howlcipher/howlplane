#!/usr/bin/env python3
"""Deterministic regression tests for bounded canary isolation and activity resolution."""

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from howlplane.control_plane.authority_envelope import (
    ENVELOPE_FILENAME,
    create_envelope,
    load_envelope,
    save_envelope,
)
from howlplane.control_plane.authority_profile import get_profile
from howlplane.control_plane.cli import (
    _build_factory_supervisor,
    _resolve_factory_campaign,
    _select_factory_authority,
    build_parser,
    cmd_factory_status,
)
from howlplane.control_plane.factory.campaign import (
    CampaignError,
    FactoryCampaign,
    _is_process_active_for_state_dir,
    campaign_from_state_dir,
    discover_repository,
    resolve_campaign,
)
from howlplane.control_plane.factory.service import _command
from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
from tests._factory_test_helpers import make_git_repo, set_xdg_paths


def _write_campaign_meta(state_dir: Path, target_dir: Path, repo_id) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "howlplane.factory.campaign/v1",
        "campaign_id": repo_id.campaign_id,
        "repository_root": str(repo_id.root),
        "remote": repo_id.remote,
        "default_branch": repo_id.default_branch,
        "source_common_git_dir": str(repo_id.common_git_dir),
        "target_repo": str(target_dir),
    }
    (state_dir / "campaign.json").write_text(json.dumps(meta), encoding="utf-8")


def test_historical_stopped_campaign_not_active(tmp_path):
    """Stopped process status in process.json must not be reported as active."""
    state = tmp_path / "hist-state"
    proc_dir = state / "campaign"
    proc_dir.mkdir(parents=True, exist_ok=True)
    proc_data = {
        "pid": 999999,
        "backend": "process",
        "command": ["python", "--state-dir", str(state)],
        "status": "stopped",
    }
    (proc_dir / "process.json").write_text(json.dumps(proc_data), encoding="utf-8")
    assert _is_process_active_for_state_dir(state) is False


def test_stale_process_record_not_active(tmp_path):
    """A dead PID in process.json without running process must report not active."""
    state = tmp_path / "stale-state"
    proc_dir = state / "campaign"
    proc_dir.mkdir(parents=True, exist_ok=True)
    proc_data = {
        "pid": 999998,
        "hostname": "non-existent-box",
        "backend": "process",
        "command": ["python", "--state-dir", str(state)],
        "status": "running",
    }
    (proc_dir / "process.json").write_text(json.dumps(proc_data), encoding="utf-8")
    assert _is_process_active_for_state_dir(state) is False


def test_quarantined_bad_authority_dir_not_active(tmp_path):
    """A renamed or quarantined state directory must not inherit active status."""
    real_state = tmp_path / "e952"
    bad_state = tmp_path / "e952.bad-authority-20260913-192100"
    proc_dir = bad_state / "campaign"
    proc_dir.mkdir(parents=True, exist_ok=True)
    # Copied process.json has the old path in --state-dir
    proc_data = {
        "pid": 12345,
        "unit_name": "howlplane-factory-e952",
        "backend": "systemd",
        "command": ["python", "--state-dir", str(real_state)],
        "status": "running",
    }
    (proc_dir / "process.json").write_text(json.dumps(proc_data), encoding="utf-8")
    assert _is_process_active_for_state_dir(bad_state) is False


def test_two_genuinely_live_campaigns_raise_ambiguity(tmp_path, monkeypatch):
    """Two genuinely active campaigns must raise an unambiguous error."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    rid = discover_repository()

    base_state = tmp_path / "state" / "howlplane" / "factory"
    base_data = tmp_path / "data" / "howlplane" / "worktrees"
    for tag in ("live-1", "live-2"):
        _write_campaign_meta(base_state / tag, base_data / tag / "target", rid)

    monkeypatch.setattr(
        "howlplane.control_plane.factory.campaign._is_process_active_for_state_dir",
        lambda _s: True,
    )
    with pytest.raises(CampaignError, match="Multiple active Factory campaigns"):
        resolve_campaign(prefer_active=True)


def test_continuous_factory_retains_stable_campaign(tmp_path, monkeypatch):
    """Continuous Factory without --max-work-items keeps canonical repo campaign."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    rid = discover_repository()

    parser = build_parser()
    args = parser.parse_args(["factory", "start"])
    campaign = _resolve_factory_campaign(args, prepare=False)
    assert campaign.state_dir.name == rid.campaign_id


def test_bounded_run_creates_fresh_campaign_identity(tmp_path, monkeypatch):
    """A bounded canary run creates an isolated, non-colliding directory identity."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)
    rid = discover_repository()

    parser = build_parser()
    args = parser.parse_args(["factory", "start", "--max-work-items", "1"])
    campaign = _resolve_factory_campaign(args, prepare=True)
    assert campaign.state_dir.name != rid.campaign_id
    assert "-canary-" in campaign.state_dir.name
    assert campaign.state_dir.exists()
    assert campaign.target_dir.exists()


def test_two_bounded_runs_create_separate_identities(tmp_path, monkeypatch):
    """Successive bounded canary launches get separate state and worktrees."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)

    c1 = resolve_campaign(bounded=True)
    c2 = resolve_campaign(bounded=True)
    assert c1.state_dir != c2.state_dir
    assert c1.target_dir != c2.target_dir


def test_explicit_state_dir_wins_in_bounded_run(tmp_path, monkeypatch):
    """Explicit --state-dir always overrides bounded campaign generation."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    custom_state = tmp_path / "custom-canary-state"

    parser = build_parser()
    args = parser.parse_args(["factory", "start", "--max-work-items", "1", "--state-dir", str(custom_state)])
    campaign = _resolve_factory_campaign(args, prepare=True, force_resolve=True)
    assert campaign.state_dir.resolve() == custom_state.resolve()


def test_explicit_authority_conflict_fails_closed(tmp_path):
    """Conflicting requested authority against existing envelope must fail closed."""
    state_dir = tmp_path / "conflict-state"
    camp_dir = state_dir / "campaign"
    camp_dir.mkdir(parents=True, exist_ok=True)
    env = create_envelope(get_profile("howlframe-overnight"), "CAMP-1", "cli:test")
    save_envelope(env, camp_dir)

    campaign = SimpleNamespace(
        state_dir=state_dir,
        repository=SimpleNamespace(remote="https://github.com/howlcipher/howlframe"),
    )
    args = SimpleNamespace(authority="safe", authority_profile=None)

    with pytest.raises(ValueError, match="conflicts with existing campaign authority envelope"):
        _select_factory_authority(args, campaign)


def test_fresh_bounded_authority_safe_persisted(tmp_path, monkeypatch, capsys):
    """Fresh canary with --authority safe sets safe/strict envelope and surfaces safe."""
    repo = make_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    set_xdg_paths(monkeypatch, tmp_path)

    parser = build_parser()
    args = parser.parse_args(["factory", "canary", "--authority", "safe"])
    campaign = _resolve_factory_campaign(args, prepare=True)
    authority = _select_factory_authority(args, campaign)
    assert authority == "strict"

    _build_factory_supervisor(args)
    envelope = load_envelope(campaign.state_dir / "campaign")
    assert envelope.profile_id == "strict"

    status_args = parser.parse_args(["factory", "status", "--state-dir", str(campaign.state_dir)])
    cmd_factory_status(status_args)
    captured = capsys.readouterr().out
    assert "Authority: safe" in captured


def test_max_work_items_command_propagation(tmp_path):
    """Service command builder propagates max_work_items and authority profile."""
    repo = SimpleNamespace(campaign_id="test123", root=tmp_path)
    camp = FactoryCampaign(repo, tmp_path / "state", tmp_path / "target")
    cmd = _command(camp, authority_profile="strict", objective="canary", max_work_items=1)
    assert "--max-work-items" in cmd
    idx = cmd.index("--max-work-items")
    assert cmd[idx + 1] == "1"
    assert "--authority-profile" in cmd
    a_idx = cmd.index("--authority-profile")
    assert cmd[a_idx + 1] == "strict"


def test_bounded_campaign_restart_preserves_isolated_state(tmp_path):
    """Restarting or querying a bounded state dir does not alter its identity."""
    repo = make_git_repo(tmp_path / "repo")
    rid = discover_repository(repo)
    state = tmp_path / "howlframe-canary-fixed"
    target = tmp_path / "target-fixed"
    _write_campaign_meta(state, target, rid)

    resolved = campaign_from_state_dir(state)
    assert resolved is not None
    assert resolved.state_dir.resolve() == state.resolve()
    assert resolved.target_dir.resolve() == target.resolve()


def test_bounded_work_item_accounting_and_reports_intact(tmp_path):
    """Bounded supervisor properly completes 1 work item and generates reports."""
    from howlplane.control_plane.factory.dispatcher import DispatchOutcome
    from howlplane.control_plane.factory.work_item import WorkItemState
    from tests._factory_test_helpers import (
        RecordingDispatcher,
        make_supervisor,
        ready_work_item,
    )

    dispatcher = RecordingDispatcher([
        DispatchOutcome(
            success=True,
            work_item_id="item-canary",
            next_work_item_state=WorkItemState.SHIPPED,
            reason="governed_lifecycle_completed",
        ),
    ])
    supervisor, now, _ = make_supervisor(tmp_path, dispatcher=dispatcher)
    supervisor._state_record.run_mode = "bounded"
    supervisor._state_record.max_work_items = 1
    supervisor._state_record.bounded_run_started_at = now["t"].isoformat()
    supervisor._persist()

    item = ready_work_item(supervisor.work_item_store, title="canary-item", identity_keys=["ci"])
    supervisor.run()

    rec = supervisor.state_record
    assert rec.work_items_dispatched == 1
    assert rec.state == "stopped"
    assert rec.bounded_run_stop_reason == "bounded_work_item_completed"

    reports_dir = tmp_path / "reports"
    assert len(list(reports_dir.glob("bounded_run_*.json"))) == 1
    assert len(list(reports_dir.glob("bounded_run_*.md"))) == 1
