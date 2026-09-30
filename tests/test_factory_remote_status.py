#!/usr/bin/env python3
"""Redacted Factory status publish and ranked-backlog admission."""

import json
from pathlib import Path

import pytest

from howlplane.control_plane.backlog_source import BacklogSource, source_file_rank
from howlplane.control_plane.cli import main
from howlplane.control_plane.factory import status_publish
from howlplane.control_plane.factory.status_publish import (
    STATUS_SCHEMA,
    arm_periodic_publish,
    build_redacted_status,
    default_publish_path,
    publish_status,
    read_armed_publish_path,
    read_mission_campaign_id,
    redact_text,
)
from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
from howlplane.control_plane.factory.task_queue import load_queue
from tests._factory_test_helpers import make_supervisor


ROOT = Path(__file__).resolve().parents[1]
GITHUB_TOKEN = "ghp_" + ("a" * 36)
BEARER = "Bearer " + ("b" * 24)
HOME_PATH = "/home/alice/dev/howlplane"
HOST_PATH = "/run/media/system/tallgeese/dev/howlplane"


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
        "recent_completed": [{"output": "SECRET_TASK_OUTPUT " + GITHUB_TOKEN}],
        "recent_failed": [{"stderr": "failed " + HOME_PATH}],
        "recent_parked": [{"note": "parked raw transcript"}],
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
    assert "SECRET_TASK_OUTPUT" not in blob
    assert "raw transcript" not in blob
    assert "tallgeese" not in blob
    assert "/home/" not in blob
    assert "workspace_file" not in snapshot
    assert "provider_inventory" not in snapshot
    assert "worktree" not in snapshot
    assert "recent_completed" not in snapshot
    assert "recent_failed" not in snapshot
    assert "recent_parked" not in snapshot
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


def test_default_publish_path_is_the_factory_status_snapshot(tmp_path):
    path = default_publish_path(tmp_path)
    assert path == tmp_path / "factory" / "status" / "remote-snapshot.json"


def test_mission_id_is_found_from_the_factory_status_directory(tmp_path):
    checkout = tmp_path / "checkout"
    snapshot = checkout / "factory" / "status" / "remote-snapshot.json"
    snapshot.parent.mkdir(parents=True)
    dogfood = checkout / ".dogfood"
    dogfood.mkdir()
    (dogfood / "mission_state.json").write_text(
        json.dumps({"campaign_id": "2026-09-27-continuous-improvement"}),
        encoding="utf-8",
    )
    assert read_mission_campaign_id(snapshot) == "2026-09-27-continuous-improvement"


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
    record.recent_completed.append({"output": "SECRET_TASK_OUTPUT"})
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
    assert "SECRET_TASK_OUTPUT" not in published
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
    supervisor.state_record.recent_failed.append({"stderr": "RAW_FAILURE_OUTPUT"})
    supervisor._persist()
    dest = tmp_path / "pub" / "factory-status.json"
    arm_periodic_publish(state, dest)
    supervisor.run_once()
    published = dest.read_text(encoding="utf-8")
    assert "d" * 24 not in published
    assert "RAW_FAILURE_OUTPUT" not in published
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


def test_issues_md_pending_row_is_the_admit_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "issues.md").write_text(
        "\n".join([
            "## Ranked Backlog",
            "| # | Title | Status | Score | Rationale |",
            "| --- | --- | --- | --- | --- |",
            "| 7 | [Live bug](#7) | Pending | 2.0 | open |",
            "| 8 | [Blocked](#8) | Pending — blocked on #1 | 2.0 | wait |",
        ]),
        encoding="utf-8",
    )
    (repo / "improvements.md").write_text(
        "\n".join([
            "## Ranked Backlog",
            "| # | Title | Status | Score | Rationale |",
            "| --- | --- | --- | --- | --- |",
            "| 3 | [Live improvement](#3) | Pending | 1.5 | open |",
        ]),
        encoding="utf-8",
    )
    selection = BacklogSource(repo).select()
    assert selection.files_read == ["issues.md", "improvements.md"]
    assert [item.item_id for item in selection.eligible] == ["7", "3"]
    bug, improvement = selection.eligible
    assert bug.kind == "bug"
    assert improvement.kind == "improvement"
    assert source_file_rank(bug) == 0
    assert source_file_rank(improvement) == 1
    assert all(item.status == "Pending" for item in selection.eligible)


def test_committed_examples_match_the_admit_contracts():
    tasks = load_queue(
        ROOT / "factory" / "examples" / "factory-queue.example.json", ROOT
    )
    assert [task.status for task in tasks] == ["PROPOSED"]
    example = ROOT / "factory" / "examples" / "ranked-backlog-row.md"
    assert example.name not in {"bugs.md", "issues.md", "improvements.md"}
    assert "Pending" in example.read_text(encoding="utf-8")


# --- Row 71: read-side contract (freshness, absent, tip identity) ---------------

def _snapshot_file(tmp_path, published_at, schema=status_publish.STATUS_SCHEMA):
    path = tmp_path / "factory" / "status" / "remote-snapshot.json"
    path.parent.mkdir(parents=True)
    doc = status_publish.build_redacted_status({"state": "idle"}, published_at=published_at)
    doc["schema"] = schema
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_reader_distinguishes_fresh_stale_absent_and_invalid(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    absent = status_publish.read_snapshot(tmp_path / "nope.json", now=now)
    assert absent["freshness"] == status_publish.SNAPSHOT_ABSENT and absent["snapshot"] is None

    fresh = _snapshot_file(tmp_path, (now - timedelta(seconds=60)).isoformat())
    assert status_publish.read_snapshot(fresh, now=now)["freshness"] == status_publish.FRESH
    assert status_publish.read_snapshot(fresh, now=now, fresh_seconds=30)["freshness"] == status_publish.STALE

    future = tmp_path / "future.json"
    future.write_text(fresh.read_text().replace(
        json.loads(fresh.read_text())["published_at"], (now + timedelta(days=1)).isoformat()))
    assert status_publish.read_snapshot(future, now=now)["freshness"] == status_publish.SNAPSHOT_INVALID

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert status_publish.read_snapshot(bad, now=now)["freshness"] == status_publish.SNAPSHOT_INVALID
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": "other/v9", "published_at": now.isoformat()}))
    assert status_publish.read_snapshot(wrong, now=now)["freshness"] == status_publish.SNAPSHOT_INVALID


def test_reader_reports_git_tip_identity_and_never_writes(tmp_path):
    import subprocess
    from datetime import datetime, timezone
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = _snapshot_file(tmp_path, datetime.now(timezone.utc).isoformat())
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n",
                    "commit", "-q", "-m", "snap"], check=True)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    result = status_publish.read_snapshot(path, repo_root=tmp_path)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert result["tip_sha"] == head == result["head_sha"]
    assert result["path"] == "factory/status/remote-snapshot.json"
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_published_snapshot_carries_stable_operator_summary():
    doc = status_publish.build_redacted_status({"state": "waiting_for_authority"})
    assert doc["operator"] == {"label": "OWNER REQUIRED", "severity": "attention",
                               "reason_code": "OWNER_REQUIRED", "owner_required": True}


def test_factory_snapshot_cli_absent_is_not_healthy(tmp_path, capsys):
    from howlplane.control_plane.cli import main
    assert main(["factory", "snapshot", "--repo", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["freshness"] == "SNAPSHOT_ABSENT"


# --- Row 72: Pending identity and validation evidence (read-only, same admit path) ---

def _backlog_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "improvements.md").write_text("\n".join([
        "## Ranked Backlog",
        "| # | Title | Status | Score | Rationale |",
        "| --- | --- | --- | --- | --- |",
        "| 3 | [Live](#3-live) | Pending | 1.5 | open |",
        "| 4 | [Blocked](#4-b) | Pending — blocked on #3 | 1.0 | wait |",
        "| 5 | [No detail](#5-n) | Pending | 1.0 | open |",
        "",
        "### 3. Live",
        "",
        "Acceptance: something.",
    ]), encoding="utf-8")
    return repo


def test_pending_projection_shares_identity_with_the_admit_path(tmp_path):
    from howlplane.control_plane import backlog_source
    repo = _backlog_repo(tmp_path)
    projection = backlog_source.pending_projection(repo)
    selection = BacklogSource(repo).select()
    eligible = [r for r in projection["rows"] if r["eligible"]]
    assert [r["task_id"] for r in eligible] == [i.task_id for i in selection.eligible]
    assert eligible[0]["item_id"] == "3" and eligible[0]["rank"] == 1
    blocked = next(r for r in projection["rows"] if r["item_id"] == "4")
    assert not blocked["eligible"] and blocked["reason"].startswith("STATUS_NOT_ELIGIBLE")


def test_validation_evidence_is_deterministic_and_does_not_write(tmp_path):
    from howlplane.control_plane import backlog_source
    repo = _backlog_repo(tmp_path)
    before = {p.name: p.read_bytes() for p in repo.iterdir()}
    first = backlog_source.validate_pending_row(repo, "3")
    assert first == backlog_source.validate_pending_row(repo, "3")
    assert first["admittable"] and first["task_id"] == "HOWLFRAM-IMP-3" and first["detail_sha256"]
    assert backlog_source.validate_pending_row(repo, "5")["reason"] == "MISSING_DETAIL_SECTION"
    assert backlog_source.validate_pending_row(repo, "4")["admittable"] is False
    assert backlog_source.validate_pending_row(repo, "99")["found"] is False
    assert {p.name: p.read_bytes() for p in repo.iterdir()} == before


def test_factory_pending_cli_validate_exit_codes(tmp_path, capsys):
    repo = _backlog_repo(tmp_path)
    assert main(["factory", "pending", "--repo", str(repo), "--validate", "3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "howlplane.backlog.validation/v1"
    assert main(["factory", "pending", "--repo", str(repo), "--validate", "4"]) == 1
