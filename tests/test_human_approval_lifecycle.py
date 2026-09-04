"""
test_human_approval_lifecycle.py

Deterministic tests proving the human authority lifecycle:
1. awaiting_human -> approve -> resume -> complete (simulated safe executor)
2. awaiting_human -> reject -> terminal non-complete
3. duplicate approval is idempotent
4. contradictory decision fails closed
5. missing task fails clearly
6. approve on non-awaiting task fails appropriately
7. resume without approval does not bypass boundary
8. repository drift invalidates approval
9. evidence records human decision
10. status shows correct next action
11. CLI commands work with --json and stdout formats
"""

import json
from pathlib import Path
import subprocess
import pytest

from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.human_boundary import (
    HumanDecisionRecord,
    HumanLifecycleManager,
    RepositoryStateFingerprint,
    compute_repository_fingerprint,
    TaskRunNotFoundError,
    InvalidTaskStateError,
    ContradictoryDecisionError,
    RepositoryDriftError,
    StaleApprovalError,
    ApprovalRequiredError,
)
from src.control_plane.launcher import cmd_approve, cmd_reject, cmd_resume, cmd_status, build_parser
from src.control_plane.task_spec import TaskSpec
from src.control_plane.git_env import run_git_in_repo
from tests._git_test_helpers import git_in_repo, init_git_repo


def _init_git_repo(repo_path: Path) -> None:
    """Initializes a clean git repository."""
    init_git_repo(repo_path, files={"README.md": "# Test Repo\n"})


def _create_awaiting_human_task_run(
    repo_path: Path,
    task_id: str = "TASK-100",
    boundaries: list = None,
    diff_content: str = None,
) -> Path:
    """Creates a simulated awaiting_human task run directory."""
    run_dir = repo_path / ".task_runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id=task_id,
        repository=repo_path.name,
        objective="Accept temporary debt tombstone for refactoring",
        task_class="refactor",
        risk_level="medium",
        human_approval_requirements=boundaries or ["slop_debt_acceptance"],
        current_state="awaiting_human",
    )
    spec.save_to_file(str(run_dir / "task.yaml"))

    if diff_content is None:
        (repo_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
        fp = compute_repository_fingerprint(repo_path, run_dir)
        res_diff = run_git_in_repo(repo_path, ["diff", "HEAD"])
        diff_text = res_diff.stdout
        for f_name in fp.files_modified:
            f_path = repo_path / f_name
            if f_path.is_file():
                try:
                    diff_text += f"\n--- {f_name} ---\n" + f_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        (run_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    else:
        (run_dir / "diff.patch").write_text(diff_content, encoding="utf-8")

    trigger_name = (boundaries or ["slop_debt_acceptance"])[0]
    (run_dir / "decision_packet.md").write_text(
        f"# 🛑 Human Authority Decision Packet: Task `{task_id}`\n\n"
        f"## Objective\nAccept temporary debt tombstone for refactoring\n\n"
        f"## Boundary Triggers\n- ⚠️ **{trigger_name}**: Human authority boundary\n",
        encoding="utf-8",
    )
    (run_dir / "verification_result.json").write_text(
        json.dumps({"overall_status": "verified", "steps": []}),
        encoding="utf-8",
    )
    return run_dir


def test_scenario_1_approve_and_resume_to_complete(tmp_path: Path):
    """Scenario 1: awaiting_human -> approve -> resume -> complete."""
    _init_git_repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))

    run_dir = _create_awaiting_human_task_run(tmp_path, "TASK-101")

    # 1. Approve task
    record = HumanLifecycleManager.approve(
        target_repo=tmp_path,
        task_id="TASK-101",
        reason="Operator approved deployment plan",
        operator_source="cli",
        ledger=ledger,
    )
    assert record.decision == "approved"
    assert record.task_id == "TASK-101"
    assert record.reason == "Operator approved deployment plan"
    assert (run_dir / "human_decision.json").is_file()

    # 2. Resume task
    res = HumanLifecycleManager.resume(
        target_repo=tmp_path,
        task_id="TASK-101",
        ledger=ledger,
    )
    assert res.final_state == "complete"
    assert res.exit_code == 0

    # Verify task spec transitioned to complete
    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert spec.current_state == "complete"

    # Verify evidence ledger events
    entries = ledger.get_task_entries("TASK-101")
    actions = [e.action for e in entries]
    assert "human_approval" in actions
    assert "task_resumed" in actions
    assert "task_completed" in actions


def test_scenario_2_reject_transitions_to_failed(tmp_path: Path):
    """Scenario 2: awaiting_human -> reject -> terminal non-complete (failed)."""
    _init_git_repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))

    run_dir = _create_awaiting_human_task_run(tmp_path, "TASK-102")

    record = HumanLifecycleManager.reject(
        target_repo=tmp_path,
        task_id="TASK-102",
        reason="Blocked by security policy review",
        operator_source="cli",
        ledger=ledger,
    )
    assert record.decision == "rejected"
    assert record.reason == "Blocked by security policy review"

    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert spec.current_state == "failed"

    # Attempting to resume a rejected task fails
    with pytest.raises(InvalidTaskStateError, match="rejected"):
        HumanLifecycleManager.resume(
            target_repo=tmp_path,
            task_id="TASK-102",
            ledger=ledger,
        )

    # Evidence ledger records rejection
    entries = ledger.get_task_entries("TASK-102")
    actions = [e.action for e in entries]
    assert "human_rejection" in actions


def test_scenario_3_duplicate_approval_is_idempotent(tmp_path: Path):
    """Scenario 3: Repeated approval on the same task is a safe idempotent no-op."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-103")

    rec1 = HumanLifecycleManager.approve(tmp_path, "TASK-103", reason="First approval")
    rec2 = HumanLifecycleManager.approve(tmp_path, "TASK-103", reason="First approval")

    assert rec1.decision == "approved"
    assert rec2.decision == "approved"
    assert rec1.timestamp == rec2.timestamp


def test_scenario_4_contradictory_decision_fails_closed(tmp_path: Path):
    """Scenario 4: Contradictory decisions (approve then reject, or reject then approve) fail closed."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-104A")
    _create_awaiting_human_task_run(tmp_path, "TASK-104B")

    # Approve then reject -> fails closed
    HumanLifecycleManager.approve(tmp_path, "TASK-104A", reason="Approved first")
    with pytest.raises(ContradictoryDecisionError, match="previously APPROVED"):
        HumanLifecycleManager.reject(tmp_path, "TASK-104A", reason="Attempting contradictory reject")

    # Reject then approve -> fails closed
    HumanLifecycleManager.reject(tmp_path, "TASK-104B", reason="Rejected first")
    with pytest.raises(ContradictoryDecisionError, match="previously REJECTED"):
        HumanLifecycleManager.approve(tmp_path, "TASK-104B", reason="Attempting contradictory approve")


def test_scenario_5_missing_task_fails_clearly(tmp_path: Path):
    """Scenario 5: Operating on a non-existent task fails clearly with TaskRunNotFoundError."""
    _init_git_repo(tmp_path)

    with pytest.raises(TaskRunNotFoundError):
        HumanLifecycleManager.approve(tmp_path, "NONEXISTENT-TASK")

    with pytest.raises(TaskRunNotFoundError):
        HumanLifecycleManager.reject(tmp_path, "NONEXISTENT-TASK")

    with pytest.raises(TaskRunNotFoundError):
        HumanLifecycleManager.resume(tmp_path, "NONEXISTENT-TASK")


def test_scenario_6_approve_on_non_awaiting_task_fails(tmp_path: Path):
    """Scenario 6: Approve on completed, failed, or discovered task fails appropriately."""
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".task_runs" / "TASK-106"
    run_dir.mkdir(parents=True)

    spec = TaskSpec(task_id="TASK-106", repository=tmp_path.name, objective="Test", current_state="discovered")
    spec.save_to_file(str(run_dir / "task.yaml"))

    with pytest.raises(InvalidTaskStateError, match="discovered"):
        HumanLifecycleManager.approve(tmp_path, "TASK-106")

    spec_complete = TaskSpec(task_id="TASK-106", repository=tmp_path.name, objective="Test", current_state="complete")
    spec_complete.save_to_file(str(run_dir / "task.yaml"))
    with pytest.raises(InvalidTaskStateError, match="already COMPLETE"):
        HumanLifecycleManager.approve(tmp_path, "TASK-106")


def test_scenario_7_resume_without_approval_fails(tmp_path: Path):
    """Scenario 7: Resume on awaiting_human task without approval refuses to bypass boundary."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-107")

    with pytest.raises(ApprovalRequiredError, match="requires explicit human approval"):
        HumanLifecycleManager.resume(tmp_path, "TASK-107")


def test_scenario_8_repository_drift_invalidates_approval(tmp_path: Path):
    """Scenario 8: Repository changes after review/approval invalidate approval as STALE."""
    _init_git_repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))

    _create_awaiting_human_task_run(tmp_path, "TASK-108")

    # Approve under current repo state
    HumanLifecycleManager.approve(tmp_path, "TASK-108", ledger=ledger)

    # Introduce repository drift by committing new code
    (tmp_path / "extra_file.py").write_text("print('drift')\n", encoding="utf-8")
    git_in_repo(tmp_path, ["add", "extra_file.py"])
    git_in_repo(tmp_path, ["commit", "-m", "drift commit"])

    # Attempting to resume with drifted repo must raise StaleApprovalError and record event
    with pytest.raises(StaleApprovalError, match="STALE"):
        HumanLifecycleManager.resume(tmp_path, "TASK-108", ledger=ledger)

    entries = ledger.get_task_entries("TASK-108")
    actions = [e.action for e in entries]
    assert "stale_approval_detected" in actions


def test_scenario_9_evidence_ledger_lifecycle_events(tmp_path: Path):
    """Scenario 9: Evidence ledger records all required lifecycle events."""
    _init_git_repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))

    _create_awaiting_human_task_run(tmp_path, "TASK-109")

    # Record human decision requested
    ledger.append_entry(
        EvidenceEntry(
            task_id="TASK-109",
            agent_id="control_plane",
            action="human_decision_requested",
            artifact="decision_packet.md",
        )
    )

    HumanLifecycleManager.approve(tmp_path, "TASK-109", reason="Approved", ledger=ledger)
    HumanLifecycleManager.resume(tmp_path, "TASK-109", ledger=ledger)

    entries = ledger.get_task_entries("TASK-109")
    actions = [e.action for e in entries]
    assert "human_decision_requested" in actions
    assert "human_approval" in actions
    assert "task_resumed" in actions
    assert "task_completed" in actions


def test_scenario_10_status_ux_and_cli_dispatch(tmp_path: Path, capsys):
    """Scenario 10: Status UX surfaces pending, approved, and next actions accurately."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-110")

    parser = build_parser()

    # Test status before approval
    opts = parser.parse_args(["status", "--repo", str(tmp_path)])
    ret = cmd_status(opts)
    assert ret == 0
    out = capsys.readouterr().out
    assert "AWAITING_HUMAN" in out
    assert "howlplane approve TASK-110" in out
    assert "howlplane reject TASK-110" in out

    # Test CLI approve command
    opts_appr = parser.parse_args(["approve", "TASK-110", "--repo", str(tmp_path), "--reason", "LGTM"])
    ret_appr = cmd_approve(opts_appr)
    assert ret_appr == 0
    out_appr = capsys.readouterr().out
    assert "TASK AUTHORIZED: TASK-110" in out_appr
    assert "howlplane resume TASK-110" in out_appr

    # Test status after approval
    opts_status2 = parser.parse_args(["status", "--repo", str(tmp_path)])
    cmd_status(opts_status2)
    out_status2 = capsys.readouterr().out
    assert "APPROVED" in out_status2
    assert "howlplane resume TASK-110" in out_status2

    # Test CLI resume command
    opts_resume = parser.parse_args(["resume", "TASK-110", "--repo", str(tmp_path), "--json"])
    ret_resume = cmd_resume(opts_resume)
    assert ret_resume == 0
    out_resume = capsys.readouterr().out
    res_dict = json.loads(out_resume)
    assert res_dict["final_state"] == "complete"


def test_drift_before_approval_blocked(tmp_path: Path):
    """Proves that code modifications before approval block approval with RepositoryDriftError."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-111")

    # Change the local working tree code after review
    (tmp_path / "app.py").write_text("print('different content')\n", encoding="utf-8")

    with pytest.raises(RepositoryDriftError, match="code has changed"):
        HumanLifecycleManager.approve(tmp_path, "TASK-111")


def test_human_decision_record_serialization_roundtrip():
    """Proves HumanDecisionRecord serialization and deserialization integrity."""
    fp = RepositoryStateFingerprint(
        commit_sha="abcdef123456",
        dirty=True,
        diff_sha256="1234567890abcdef",
        files_modified=["src/app.py"],
    )
    rec = HumanDecisionRecord(
        task_id="TASK-112",
        decision="approved",
        boundary_triggers=["infrastructure_apply"],
        operator_source="cli",
        reason="Verified manually",
        repository="test-repo",
        repository_state=fp,
        decision_packet_sha256="packetsha",
    )

    d = rec.to_dict()
    assert d["schema"] == "howlplane.human_decision/v1"
    assert d["repository_state"]["commit_sha"] == "abcdef123456"

    loaded = HumanDecisionRecord.from_dict(d)
    assert loaded.task_id == "TASK-112"
    assert loaded.decision == "approved"
    assert loaded.repository_state.commit_sha == "abcdef123456"
    assert loaded.repository_state.files_modified == ["src/app.py"]


def test_cli_reject_command(tmp_path: Path, capsys):
    """Proves CLI reject command execution and json output."""
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "TASK-113")

    parser = build_parser()
    opts_rej = parser.parse_args(["reject", "TASK-113", "--repo", str(tmp_path), "--reason", "Not ready", "--json"])
    ret = cmd_reject(opts_rej)
    assert ret == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["decision"] == "rejected"
    assert data["reason"] == "Not ready"
    assert data["task_id"] == "TASK-113"
