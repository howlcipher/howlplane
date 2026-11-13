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
import shlex
import subprocess
from types import SimpleNamespace
import pytest

from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from howlplane.control_plane.human_boundary import (
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
from howlplane.control_plane.launcher import cmd_approve, cmd_reject, cmd_resume, cmd_status, build_parser
from howlplane.control_plane.task_spec import TaskSpec
from howlplane.control_plane.git_baseline import capture_baseline
from howlplane.control_plane.git_env import run_git_in_repo, sanitized_git_env
from tests._git_test_helpers import git_in_repo, init_git_repo
from tests._tia_helpers import record_valid_tia


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
    record_valid_tia(ledger, "TASK-101")

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
    record_valid_tia(ledger, "TASK-109")

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
    ledger_file = tmp_path / "cli-ledger.jsonl"
    record_valid_tia(EvidenceLedger(str(ledger_file)), "TASK-110")

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
    opts_appr = parser.parse_args(
        ["approve", "TASK-110", "--repo", str(tmp_path), "--reason", "LGTM", "--ledger-file", str(ledger_file)]
    )
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
    opts_resume = parser.parse_args(["resume", "TASK-110", "--repo", str(tmp_path), "--ledger-file", str(ledger_file), "--json"])
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
    opts_rej = parser.parse_args(
        ["reject", "TASK-113", "--repo", str(tmp_path), "--reason", "Not ready", "--json",
         "--ledger-file", str(tmp_path / "ledger.jsonl")]
    )
    ret = cmd_reject(opts_rej)
    assert ret == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["decision"] == "rejected"
    assert data["reason"] == "Not ready"
    assert data["task_id"] == "TASK-113"


# --- reject lists the changes the task left in the working tree (issues.md #26) ---

_LEFTOVER_BASE_FILES = {
    "README.md": "# Test Repo\n", "mod.txt": "m\n", "gone.txt": "g\n", "ren_old.txt": "r\n",
    "sp ace.txt": "s\n", "g*b.txt": "glob\n", "gxb.txt": "decoy\n",
}
_HEADING = "Changes left in the working tree (not discarded):"
_DISCARD = "To discard exactly these changes (printed, not run):"
_RESTORE = "  {git} restore --staged --worktree -- "
_UNDETERMINED = ["The changes this task left in the working tree could not be determined."]
_NOT_LISTED = ["The leftover changes could not be listed."]


def _listing(rows, commands):
    return ["", _HEADING, *rows, _DISCARD, *commands]


def _tree_state(repo: Path):
    """Porcelain status plus every path (files with bytes, directories as None) outside .git/.task_runs."""
    files = {
        p.relative_to(repo).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(repo.rglob("*"))
        if not {".git", ".task_runs"} & set(p.relative_to(repo).parts)
    }
    return run_git_in_repo(repo, ["status", "--porcelain"]).stdout, files


def _apply_ops(repo: Path, ops) -> None:
    for op, *rest in ops:
        if op == "write":
            (repo / rest[0]).parent.mkdir(parents=True, exist_ok=True)
            (repo / rest[0]).write_text(rest[1], encoding="utf-8")
        elif op == "rm":
            (repo / rest[0]).unlink()
        elif op == "git":
            git_in_repo(repo, rest)
        else:  # a real broken repository: git status fails on a corrupt index
            (repo / ".git" / "index").write_bytes(b"not an index")


def _reject_scenario(tmp_path: Path, capsys, pre, ops, baseline, as_json: bool) -> SimpleNamespace:
    """Real repo, real baseline.json (or a broken one), task changes, then the real reject CLI."""
    repo = tmp_path / "repo"
    init_git_repo(repo, files=_LEFTOVER_BASE_FILES)
    run_dir = _create_awaiting_human_task_run(repo, "TASK-LEFT", diff_content="")
    _apply_ops(repo, pre)
    baseline_file = run_dir / "baseline.json"
    if baseline == "dir":
        baseline_file.mkdir()
    elif baseline is None:
        baseline_file.write_text(json.dumps(capture_baseline(repo).to_dict()), encoding="utf-8")
    elif baseline != "missing":
        baseline_file.write_text(baseline, encoding="utf-8")
    pre_task = _tree_state(repo)
    _apply_ops(repo, ops)
    before = _tree_state(repo)
    argv = ["reject", "TASK-LEFT", "--repo", str(repo), "--reason", "No", "--ledger-file", str(tmp_path / "l.jsonl")]
    assert cmd_reject(build_parser().parse_args(argv + (["--json"] if as_json else []))) == 0
    out = capsys.readouterr().out
    assert _tree_state(repo) == before
    persisted = json.loads((run_dir / "human_decision.json").read_text(encoding="utf-8"))
    assert persisted["decision"] == "rejected" and "leftover_changes" not in persisted
    assert TaskSpec.load_from_file(str(run_dir / "task.yaml")).current_state == "failed"
    assert "human_rejection" in [e.action for e in EvidenceLedger(str(tmp_path / "l.jsonl")).get_task_entries("TASK-LEFT")]
    return SimpleNamespace(repo=repo, run_dir=run_dir, out=out, pre_task=pre_task)


_MOD = ("write", "mod.txt", "changed\n")
_NEW = ("write", "new.txt", "n\n")
_NEWDIR = ("write", "newdir/a.txt", "a\n")
_GONE = ("rm", "gone.txt")
_LEFTOVER_CASES = pytest.mark.parametrize("pre, ops, baseline, tail, leftover", [
    pytest.param([], [_MOD], None,
                 _listing(["  modified:  mod.txt"], [_RESTORE + "mod.txt"]),
                 ([], ["mod.txt"], []), id="modified_only"),
    pytest.param([], [_NEW], None,
                 _listing(["  untracked: new.txt"], ["  {git} clean -f -- new.txt"]),
                 (["new.txt"], [], []), id="untracked_file_only"),
    pytest.param([], [_GONE], None,
                 _listing(["  deleted:   gone.txt"], [_RESTORE + "gone.txt"]),
                 ([], [], ["gone.txt"]), id="deleted_only"),
    pytest.param([], [_MOD, _NEW, _GONE], None,
                 _listing(["  untracked: new.txt", "  modified:  mod.txt", "  deleted:   gone.txt"],
                          [_RESTORE + "mod.txt gone.txt", "  {git} clean -f -- new.txt"]),
                 (["new.txt"], ["mod.txt"], ["gone.txt"]), id="modified_untracked_deleted"),
    pytest.param([], [_NEWDIR], None,
                 _listing(["  untracked: newdir/"], ["  {git} clean -fd -- newdir/"]),
                 (["newdir/"], [], []), id="untracked_directory_only"),
    pytest.param([], [_NEW, _NEWDIR], None,
                 _listing(["  untracked: new.txt", "  untracked: newdir/"], ["  {git} clean -fd -- new.txt newdir/"]),
                 (["new.txt", "newdir/"], [], []), id="untracked_file_and_directory"),
    pytest.param([], [("write", "staged.txt", "s\n"), ("git", "add", "staged.txt"), ("git", "rm", "gone.txt"),
                      ("git", "mv", "ren_old.txt", "ren_new.txt")], None,
                 _listing(["  added:     ren_new.txt", "  added:     staged.txt",
                           "  deleted:   gone.txt", "  deleted:   ren_old.txt"],
                          [_RESTORE + "gone.txt ren_old.txt ren_new.txt staged.txt"]),
                 (["ren_new.txt", "staged.txt"], [], ["gone.txt", "ren_old.txt"]), id="staged_add_delete_rename"),
    pytest.param([("write", "gxb.txt", "dirt\n")],
                 [("write", "g*b.txt", "x\n"), ("write", "sp ace.txt", "x\n"), ("write", "new file.txt", "n\n")], None,
                 _listing(["  untracked: 'new file.txt'", "  modified:  'g*b.txt'", "  modified:  'sp ace.txt'"],
                          [_RESTORE + "'g*b.txt' 'sp ace.txt'", "  {git} clean -f -- 'new file.txt'"]),
                 (["new file.txt"], ["g*b.txt", "sp ace.txt"], []), id="glob_and_space_names"),
    pytest.param([("write", "README.md", "dirt\n"), ("write", "pre.txt", "p\n")], [_MOD, _NEW], None,
                 _listing(["  untracked: new.txt", "  modified:  mod.txt"],
                          [_RESTORE + "mod.txt", "  {git} clean -f -- new.txt"]),
                 (["new.txt"], ["mod.txt"], []), id="pre_existing_dirt_is_not_listed"),
    pytest.param([], [], None, ["No changes left in the working tree."], ([], [], []), id="clean_tree"),
    pytest.param([("write", "README.md", "dirt\n")], [], None, ["No changes left in the working tree."],
                 ([], [], []), id="clean_tree_with_pre_existing_dirt"),
    pytest.param([], [_MOD], "missing", _UNDETERMINED, None, id="baseline_missing"),
    pytest.param([], [_MOD], "{not json", _UNDETERMINED, None, id="baseline_corrupt"),
    pytest.param([], [_MOD], "", _UNDETERMINED, None, id="baseline_empty"),
    pytest.param([], [_MOD, ("corrupt_index",)], None, _UNDETERMINED, None, id="git_status_fails"),
    pytest.param([], [_MOD], "dir", _NOT_LISTED, None, id="listing_fails_baseline_is_a_directory"),
])


@_LEFTOVER_CASES
def test_reject_text_lists_leftover_changes_and_prints_working_discard_commands(
    tmp_path: Path, capsys, pre, ops, baseline, tail, leftover
):
    """Exact layout after the Terminal state line; the printed commands, run as printed, undo exactly the task."""
    s = _reject_scenario(tmp_path, capsys, pre, ops, baseline, as_json=False)
    lines = s.out.splitlines()
    terminal = lines.index("Terminal state:     FAILED (Rejected)")
    assert lines[terminal + 1] == "=" * 60
    git = f"git -C {shlex.quote(str(s.repo.resolve()))} --literal-pathspecs"
    assert lines[terminal + 2:] == [t.format(git=git) for t in tail]
    commands = [shlex.split(line) for line in lines if line.startswith("  git ")]
    for argv in commands:
        subprocess.run(argv, check=True, capture_output=True, env=sanitized_git_env())
    if commands:
        assert _tree_state(s.repo) == s.pre_task


@_LEFTOVER_CASES
def test_reject_json_keeps_existing_output_and_adds_leftover_changes(
    tmp_path: Path, capsys, pre, ops, baseline, tail, leftover
):
    """The recorded decision prints byte-for-byte as before; one trailing key carries the listing."""
    s = _reject_scenario(tmp_path, capsys, pre, ops, baseline, as_json=True)
    recorded = HumanDecisionRecord.load_from_file(str(s.run_dir / "human_decision.json")).to_json()
    assert s.out.startswith(recorded[:-2] + ',\n  "leftover_changes": ')
    added, modified, deleted = leftover or ([], [], [])
    assert json.loads(s.out)["leftover_changes"] == {
        "files_added": added, "files_modified": modified, "files_deleted": deleted,
        "determined": leftover is not None,
    }
