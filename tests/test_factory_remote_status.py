#!/usr/bin/env python3
"""Redacted Factory status publish and untrusted owner-direction admission."""

import json
from pathlib import Path

import pytest

from howlplane.control_plane.cli import main
from howlplane.control_plane.factory.owner_direction import (
    OWNER_DIRECTION_SCHEMA,
    discover_owner_directions,
)
from howlplane.control_plane.factory.status_publish import (
    STATUS_SCHEMA,
    arm_periodic_publish,
    build_redacted_status,
    publish_status,
    read_armed_publish_path,
    redact_text,
)
from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
from howlplane.control_plane.factory.task_queue import load_queue
from howlplane.control_plane.factory.work_item import WorkItemOrigin, WorkItemState
from tests._factory_test_helpers import make_supervisor


ROOT = Path(__file__).resolve().parents[1]
GITHUB_TOKEN = "ghp_" + ("a" * 36)
BEARER = "Bearer " + ("b" * 24)
HOME_PATH = "/home/alice/dev/howlplane"
HOST_PATH = "/run/media/system/tallgeese/dev/howlplane"


def _direction(direction_id="remote-1", **extra):
    payload = {
        "schema": OWNER_DIRECTION_SCHEMA,
        "id": direction_id,
        "title": "Observe the campaign",
        "goal": "Report blockers without starting a second Factory.",
    }
    payload.update(extra)
    return payload


def _write_direction(repo: Path, name: str, payload: dict) -> None:
    inbox = repo / "factory" / "owner_direction"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(json.dumps(payload), encoding="utf-8")


def test_redact_text_strips_tokens_cookies_and_host_paths():
    repository = "howlcipher/howlplane"
    raw = " ".join([
        "Authorization: " + BEARER,
        "token=" + GITHUB_TOKEN,
        "Cookie: session=supersecretvalue",
        "api_key=sk-" + ("c" * 20),
        "https://user:s3cret@github.com/howlcipher/howlplane.git?token=abcd",
        HOME_PATH,
        HOST_PATH,
        "C:\\Users\\alice\\dev\\howlplane",
    ])
    cleaned = redact_text(raw, repository)
    assert GITHUB_TOKEN not in cleaned
    assert "supersecretvalue" not in cleaned
    assert "sk-" not in cleaned
    assert "s3cret" not in cleaned
    assert "/home/alice" not in cleaned
    assert "tallgeese" not in cleaned
    assert "C:\\Users" not in cleaned
    assert repository in cleaned
    assert "[path]" not in cleaned
    assert "[redacted]" in cleaned


def test_redacted_snapshot_shape_omits_private_status_fields():
    status = {
        "campaign_id": "abc123",
        "project": "https://github.com/howlcipher/howlplane.git",
        "state": "dispatching",
        "current_dispatch_id": "D-live",
        "current_work_item_id": "WI-1",
        "last_tick_at": "2026-09-29T00:00:00+00:00",
        "last_successful_tick_at": "2026-09-29T00:00:01+00:00",
        "last_error": "failed " + BEARER,
        "failure_count": 2,
        "workspace_file": HOST_PATH + "/factory/howl-workspace.yaml",
        "worktree": HOME_PATH,
        "target_repository": HOST_PATH,
        "provider_inventory": [{"token": GITHUB_TOKEN}],
        "process": {"command": ["howlplane", "--token", GITHUB_TOKEN]},
        "parked_items": [{
            "work_item_id": "WI-parked",
            "state": "awaiting_owner",
            "blocker": "needs owner " + GITHUB_TOKEN,
        }],
        "proposals_awaiting_authority": [{"proposal_id": "P-1"}],
        "authority": "not configured",
    }
    snapshot = build_redacted_status(
        status,
        mission_campaign_id="2026-09-27-continuous-improvement",
        published_at="2026-09-29T01:00:00+00:00",
    )
    blob = json.dumps(snapshot)
    assert snapshot["schema"] == STATUS_SCHEMA
    assert snapshot["redacted"] is True
    assert snapshot["campaign_id"] == "abc123"
    assert snapshot["mission_campaign_id"] == "2026-09-27-continuous-improvement"
    assert snapshot["repository"] == "howlcipher/howlplane"
    assert snapshot["state"] == "dispatching"
    assert snapshot["current_dispatch"] == "D-live"
    assert snapshot["owner_required"] is True
    assert snapshot["last_tick_at"] == "2026-09-29T00:00:00+00:00"
    assert GITHUB_TOKEN not in blob
    assert "tallgeese" not in blob
    assert "/home/" not in blob
    assert "workspace_file" not in snapshot
    assert "provider_inventory" not in snapshot
    assert "worktree" not in snapshot
    classes = {item["class"] for item in snapshot["blockers"]}
    assert "OWNER_REQUIRED" in classes
    assert any(item.get("work_item_id") == "WI-parked" for item in snapshot["blockers"])


def test_idle_dispatch_and_waiting_for_authority_are_owner_visible():
    snapshot = build_redacted_status({
        "state": "waiting_for_authority",
        "current_dispatch_id": None,
        "last_tick_at": None,
        "last_error": None,
    })
    assert snapshot["current_dispatch"] == "idle"
    assert snapshot["campaign_id"] is None
    assert snapshot["owner_required"] is True
    assert snapshot["blockers"][0]["class"] == "OWNER_REQUIRED"


def test_publish_refuses_state_directory_and_keeps_private_error(tmp_path):
    state = tmp_path / "state"
    inside = state / "factory-status.json"
    with pytest.raises(ValueError):
        publish_status({"state": "idle", "last_error": BEARER}, inside, state_dir=state)
    outside = tmp_path / "pub" / "factory-status.json"
    written = publish_status(
        {"state": "idle", "last_error": BEARER, "project": "howlcipher/howlplane"},
        outside,
        state_dir=state,
        published_at="2026-09-29T01:00:00+00:00",
    )
    body = written.read_text(encoding="utf-8")
    assert BEARER not in body
    assert "b" * 24 not in body
    assert json.loads(body)["current_dispatch"] == "idle"


def test_status_publish_does_not_rewrite_supervisor_state(tmp_path, capsys):
    state = tmp_path / "state"
    store = SupervisorStateStore(state / "supervisor")
    record = store.load()
    record.last_error = "Authorization: " + BEARER + " token=" + GITHUB_TOKEN
    record.objective = "work in " + HOME_PATH
    store.save(record)
    state_path = state / "supervisor" / "factory_supervisor.json"
    original = state_path.read_bytes()
    dest = tmp_path / "pub" / "factory-status.json"
    code = main([
        "factory", "status",
        "--state-dir", str(state),
        "--publish",
        "--publish-path", str(dest),
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert state_path.read_bytes() == original
    assert GITHUB_TOKEN in state_path.read_text(encoding="utf-8")
    published = dest.read_text(encoding="utf-8")
    assert GITHUB_TOKEN not in published
    assert "supersecret" not in published
    assert "/home/alice" not in published
    snapshot = json.loads(published)
    assert snapshot["schema"] == STATUS_SCHEMA
    assert snapshot["state"] == record.state
    assert snapshot["current_dispatch"] == "idle"
    assert "Published redacted status:" in captured.out


def test_arm_periodic_ignores_relative_marker_paths(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "status_publish.path"
    marker.write_text("relative/factory-status.json\n", encoding="utf-8")
    assert read_armed_publish_path(state) is None
    dest = tmp_path / "pub" / "factory-status.json"
    arm_periodic_publish(state, dest)
    armed = read_armed_publish_path(state)
    assert armed == dest.resolve()


def test_run_once_refreshes_armed_snapshot_without_leaking_state(tmp_path):
    state = tmp_path / "state"
    supervisor, _now, _sleeps = make_supervisor(tmp_path, state_dir=state)
    supervisor.state_record.last_error = "Cookie: session=" + ("d" * 24)
    supervisor._persist()
    dest = tmp_path / "pub" / "factory-status.json"
    arm_periodic_publish(state, dest)
    supervisor.run_once()
    published = dest.read_text(encoding="utf-8")
    assert "d" * 24 not in published
    assert json.loads(published)["schema"] == STATUS_SCHEMA
    private = (state / "supervisor" / "factory_supervisor.json").read_text(encoding="utf-8")
    assert "d" * 24 in private


def test_publish_failure_does_not_fail_the_supervisor_tick(tmp_path):
    state = tmp_path / "state"
    supervisor, _now, _sleeps = make_supervisor(tmp_path, state_dir=state)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    arm_periodic_publish(state, blocker / "factory-status.json")
    result = supervisor.run_once()
    assert result.state == "waiting_for_work"
    assert not (blocker / "factory-status.json").exists()


def test_owner_direction_file_cannot_trust_itself(tmp_path):
    repo = tmp_path / "repo"
    _write_direction(repo, "remote-1.json", _direction(trusted_provenance=True, trusted=True))
    _write_direction(repo, "skip.example.json", _direction("skipped"))
    _write_direction(repo, "bad.json", {"schema": "nope"})
    found = discover_owner_directions(repo, "howlcipher/howlplane")
    assert len(found) == 1
    assert found[0]["origin"] == "owner_direction"
    assert found[0]["trusted_provenance"] is False
    assert found[0]["repository"] == "howlcipher/howlplane"
    assert found[0]["identity_keys"] == ["factory/owner_direction", "remote-1"]


def test_owner_direction_skips_repository_mismatch(tmp_path):
    repo = tmp_path / "repo"
    _write_direction(
        repo, "other.json", _direction("other", repository="howlcipher/other")
    )
    assert discover_owner_directions(repo, "howlcipher/howlplane") == []


def test_owner_direction_skips_symlinks(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_direction("linked")), encoding="utf-8")
    inbox = repo / "factory" / "owner_direction"
    inbox.mkdir(parents=True, exist_ok=True)
    link = inbox / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink unsupported")
    assert discover_owner_directions(repo, "howlcipher/howlplane") == []


def test_supervisor_parks_owner_direction_and_still_readies_backlog(tmp_path):
    repo = tmp_path / "repo"
    _write_direction(repo, "remote-1.json", _direction(trusted_provenance=True))
    state = tmp_path / "state"
    supervisor, _now, _sleeps = make_supervisor(
        tmp_path,
        state_dir=state,
        discovery=lambda: [{
            "origin": "existing_backlog",
            "repository": "howlcipher/howlplane",
            "title": "bug",
            "identity_keys": ["bugs.md", "1"],
            "evidence_refs": ["bugs.md#1"],
            "evidence_fingerprints": ["backlog:bugs.md:1"],
            "source_file_rank": 0,
            "source_rank": 1,
            "kind": "bug",
        }],
    )
    supervisor.state_record.target_repository = str(repo)
    supervisor._persist()
    supervisor.tick()
    items = {item.origin: item for item in supervisor.work_item_store.list_all()}
    assert items[WorkItemOrigin.OWNER_DIRECTION].state == WorkItemState.AWAITING_OWNER
    assert items[WorkItemOrigin.EXISTING_BACKLOG].state in (
        WorkItemState.READY, WorkItemState.SHIPPED,
    )


def test_workspace_file_discovers_direction_in_declared_repo(tmp_path):
    repo = tmp_path / "repo"
    _write_direction(repo, "remote-2.json", _direction("remote-2"))
    workspace = tmp_path / "workspace.yaml"
    workspace.write_text(
        'repositories:\n  - path: "%s"\n    repository: howlcipher/howlplane\n' % repo.as_posix(),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    supervisor, _now, _sleeps = make_supervisor(tmp_path, state_dir=state)
    supervisor.state_record.workspace_file = str(workspace)
    supervisor._persist()
    supervisor.tick()
    item = supervisor.work_item_store.list_all()[0]
    assert item.origin == WorkItemOrigin.OWNER_DIRECTION
    assert item.state == WorkItemState.AWAITING_OWNER
    assert item.repository == "howlcipher/howlplane"


def test_committed_examples_match_the_admit_contracts(tmp_path):
    example = ROOT / "factory" / "examples" / "owner_direction.example.json"
    payload = json.loads(example.read_text(encoding="utf-8"))
    assert payload["schema"] == OWNER_DIRECTION_SCHEMA
    repo = tmp_path / "repo"
    inbox = repo / "factory" / "owner_direction"
    inbox.mkdir(parents=True)
    (inbox / example.name).write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    assert discover_owner_directions(repo, "howlcipher/howlplane") == []
    (inbox / "HOWL-011-example.json").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8"
    )
    found = discover_owner_directions(repo, "howlcipher/howlplane")
    assert len(found) == 1
    assert found[0]["trusted_provenance"] is False
    assert found[0]["identity_keys"][1] == "HOWL-011-example"
    tasks = load_queue(
        ROOT / "factory" / "examples" / "factory-queue.example.json", ROOT
    )
    assert [task.status for task in tasks] == ["PROPOSED"]
    assert (ROOT / "factory" / "examples" / "ranked-backlog-row.md").name not in {
        "bugs.md", "improvements.md",
    }
