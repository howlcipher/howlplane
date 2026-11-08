"""`howlplane work --json` and `resume --json`: the howlplane.work_result/v1 contract.

An agent drives HowlPlane through these documents, so they are pinned here: the
schema id, the keys an agent branches on, and the next command it should run.
"""

import json
import re

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.agent_execution import FakeAgentBackend
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.governance_cli import cmd_resume
from howlplane.control_plane.human_boundary import HumanLifecycleManager
from howlplane.control_plane.presentation.work_result import WORK_RESULT_SCHEMA, role_verdicts
from tests._control_plane_home import private_control_plane
from tests._tia_helpers import record_valid_tia
from tests.test_closed_loop_orchestrator import _init_test_git_repo, _use_unprobed_pool

pytestmark = pytest.mark.unit

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CONTRACT_KEYS = {
    "schema", "task_id", "final_state", "exit_code", "run_dir", "implementer", "changes",
    "reviews", "remediation_cycles", "verification", "follow_ups", "decision_packet",
    "error", "next_action",
}

MEDIUM = """
findings:
  - id: "MED-1"
    reviewer_role: "test-falsifier"
    title: "Empty token path untested"
    severity: "medium"
    category: "test_gap"
    location: "tests/test_auth.py"
    claim: "c"
    evidence: "e"
"""


def _fake_agents(monkeypatch):
    """`work --execute` with a fake implementer and a falsifier that never relents."""
    real_config = cli.OrchestrationConfig

    def touch(task, cwd, prompt):
        (cwd / "src" / "auth.py").write_text(
            "def authenticate(username, token):\n    return bool(username) and False\n", encoding="utf-8"
        )

    def config(**kwargs):
        return real_config(
            **kwargs,
            custom_backend=FakeAgentBackend(agent_id="claude_code", side_effect=touch),
            custom_reviewer_fn=lambda role, diff, task: MEDIUM if role == "test-falsifier" else "findings: []\n",
            custom_remediation_fn=lambda task, cwd, findings: None,
            max_remediation_cycles=0,
        )

    monkeypatch.setattr(cli, "OrchestrationConfig", config)


def _work(capsys, repo, *extra):
    args = cli.build_parser().parse_args(
        ["work", "Tighten auth checks", "--repo", str(repo), "--skip-doctor", "--json", *extra]
    )
    code = cli.cmd_work(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured


def test_work_execute_json_is_one_v1_document_that_names_the_next_command(tmp_path, monkeypatch, capsys):
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    _fake_agents(monkeypatch)
    repo = _init_test_git_repo(tmp_path / "repo")

    code, doc, captured = _work(capsys, repo, "--execute")

    assert code == 2 and doc["exit_code"] == 2
    assert CONTRACT_KEYS <= doc.keys() and doc["schema"] == WORK_RESULT_SCHEMA
    assert ANSI.search(captured.out) is None and "[HowlPlane]" not in captured.out  # progress stays on stderr
    assert doc["final_state"] == "awaiting_human"
    task_id = doc["task_id"]
    assert doc["next_action"]["command"] == f"howlplane approve {task_id}"
    assert doc["next_action"]["then"] == f"howlplane resume {task_id}"
    assert doc["next_action"]["alternatives"] == [f"howlplane reject {task_id}"]
    assert doc["decision_packet"].endswith("decision_packet.md")
    assert doc["verification"]["status"] == "passed"  # verified before it asked (issues.md #23)
    falsifier = next(r for r in doc["reviews"] if r["role"] == "test-falsifier")
    assert falsifier == {"role": "test-falsifier", "verdict": "findings", "finding_count": 1,
                         "highest_severity": "medium", "earlier_cycle": False, "self_review": False}
    assert doc["changes"]["files_modified"] == ["src/auth.py"]

    # The agent follows next_action: approve, then resume, and reads the same contract back.
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))
    record_valid_tia(ledger, task_id)
    HumanLifecycleManager.approve(target_repo=repo, task_id=task_id, reason="medium nit accepted",
                                  operator_source="cli", ledger=ledger)
    resume_args = cli.build_parser().parse_args(
        ["resume", task_id, "--repo", str(repo), "--ledger-file", str(ledger_file), "--json"]
    )
    assert cmd_resume(resume_args) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["schema"] == "howlplane.orchestrator/v1"  # existing output unchanged
    assert resumed["work_result"]["schema"] == WORK_RESULT_SCHEMA
    assert resumed["work_result"]["final_state"] == "complete"
    assert resumed["work_result"]["next_action"]["reason"] == "COMPLETE"


def test_work_dry_run_json_reports_the_plan_without_parking_anything(tmp_path, monkeypatch, capsys):
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")

    code, doc, _ = _work(capsys, repo, "--dry-run")

    assert code == 0 and doc["schema"] == WORK_RESULT_SCHEMA
    assert doc["final_state"] == "prepared" and doc["requires_human_approval"] is False
    assert doc["implementer"] and doc["reviewers"]
    assert doc["next_action"]["command"] is None and doc["next_action"]["reason"] == "PREPARED"
    assert doc["launch_command"]


def test_work_json_preflight_failure_is_a_howlplane_error_on_stderr(tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import doctor

    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")
    monkeypatch.setattr(doctor, "check_git_status",
                        lambda repo: type("R", (), {"status": "error", "message": "detached HEAD"})())

    code = cli.main(["work", "Tighten auth checks", "--repo", str(repo), "--json"])
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["schema"] == "howlplane.error/v1" and error["code"] == "PREFLIGHT_FAILED"
    assert "detached HEAD" in error["message"]


def test_role_verdicts_report_an_earlier_clean_verdict_and_an_unrun_role():
    from howlplane.control_plane.review_runner import ReviewCycleResult, SingleReviewResult

    clean = SingleReviewResult(reviewer_role="correctness-reviewer", reviewer_name="codex", status="clean")
    cycles = [ReviewCycleResult(cycle_index=1, reviewer_results={"correctness-reviewer": clean}),
              ReviewCycleResult(cycle_index=2, reviewer_results={})]
    verdicts = {v["role"]: v for v in role_verdicts(cycles, ["correctness-reviewer", "security-reviewer"])}
    assert verdicts["correctness-reviewer"]["verdict"] == "pass"
    assert verdicts["correctness-reviewer"]["earlier_cycle"] is True
    assert verdicts["security-reviewer"]["verdict"] == "not_run"
