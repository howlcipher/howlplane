"""`howlplane work --json` and `resume --json`: the howlplane.work_result/v1 contract.

An agent drives HowlPlane through these documents, so they are pinned here: the
schema id, the keys an agent branches on, and the next command it should run.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from howlplane.control_plane import cli
from howlplane.control_plane.agent_execution import FakeAgentBackend
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.governance_cli import cmd_resume
from howlplane.control_plane.human_boundary import HumanLifecycleManager
from howlplane.control_plane.presentation.errors import OperatorFailure
from howlplane.control_plane.presentation.work_result import WORK_RESULT_SCHEMA, role_verdicts
from howlplane.control_plane.task_spec import VALID_TASK_CLASSES, TaskSpec
from howlplane.control_plane.verification import VerificationPlan, VerificationStep
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


# --- resume --json reads the run directory's own records (issues.md #34) ---

SEVERITIES = ("blocker", "high", "medium", "low", "informational")  # most severe first
CORRECTNESS, SECURITY = "correctness-reviewer", "security-reviewer"


def _put(run_dir, rel, data):
    path = run_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _edit(run_dir, rel, **changes):
    """Rewrites one JSON record; a value of ... removes that key."""
    path = run_dir / rel
    record = {**json.loads(path.read_text(encoding="utf-8")), **changes}
    _put(run_dir, rel, {k: v for k, v in record.items() if v is not ...})


def _review(role, status, duration, independent):
    attempts = [{"resource_id": "codex", "duration_seconds": duration}]
    return {"role": role, "status": status, "duration_seconds": duration,
            "failover": {"engaged": False, "attempts": attempts}, "independence": {"independent": independent}}


def _resumable_run(tmp_path, monkeypatch):
    """A completed run directory in a real repository: two review cycles and every record a run leaves."""
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")
    run_dir = repo / ".task_runs" / "HP-RESUME-1"
    run_dir.mkdir(parents=True)
    TaskSpec(task_id="HP-RESUME-1", repository="repo", objective="Tighten auth checks",
             current_state="complete").save_to_file(run_dir / "task.yaml")
    steps = [VerificationStep(step_id="s1", name="unit tests", command="pytest -q", status="verified",
                              exit_code=0, duration_seconds=2.0),
             VerificationStep(step_id="s2", name="lint", command="flake8", category="lint", status="verified",
                              exit_code=0, duration_seconds=0.75)]
    _put(run_dir, "verification_result.json",
         VerificationPlan(task_id="HP-RESUME-1", steps=steps, overall_status="passed").to_json())
    _put(run_dir, "implementation/attempts/01-claude_code/attempt_record.json",
         {"attempt": 1, "resource_id": "claude_code", "agent_id": "claude_code", "success": True,
          "exit_code": 0, "duration_seconds": 7.25, "failure_class": None})
    _put(run_dir, f"reviews/{CORRECTNESS}/result.json", _review(CORRECTNESS, "findings_detected", 3.5, False))
    _put(run_dir, f"reviews/{CORRECTNESS}_findings.yaml",
         yaml.dump([{"id": "A", "severity": "low"}, {"id": "B", "severity": "high"}]))
    _put(run_dir, f"reviews/{SECURITY}/result.json", _review(SECURITY, "clean", 1.25, True))
    _put(run_dir, f"reviews/{SECURITY}_findings.yaml", "[]\n")
    _put(run_dir, "remediation/cycle-01/result.json", {"duration_seconds": 4.0})
    _put(run_dir, "remediation/cycle-01/re_review/test-falsifier/result.json",
         _review("test-falsifier", "clean", 2.0, True))
    _put(run_dir, "remediation/cycle-01/re_review/test-falsifier_findings.yaml", "[]\n")
    return repo, run_dir


def _resumed(capsys, tmp_path, repo):
    args = cli.build_parser().parse_args(
        ["resume", "HP-RESUME-1", "--repo", str(repo), "--ledger-file", str(tmp_path / "ledger.jsonl"), "--json"])
    assert cmd_resume(args) == 0
    return json.loads(capsys.readouterr().out)


def test_resume_json_reports_what_the_run_directory_recorded(tmp_path, monkeypatch, capsys):
    repo, run_dir = _resumable_run(tmp_path, monkeypatch)
    document = _resumed(capsys, tmp_path, repo)
    work = document["work_result"]
    assert document["schema"] == "howlplane.orchestrator/v1" and work["schema"] == WORK_RESULT_SCHEMA

    plan = json.loads((run_dir / "verification_result.json").read_text(encoding="utf-8"))
    assert work["verification"]["status"] == plan["overall_status"] != "none", work["verification"]
    assert [(s["name"], s["status"]) for s in work["verification"]["steps"]] == \
        [(s["name"], s["status"]) for s in plan["steps"]]

    paths = [*run_dir.glob("reviews/*/result.json"), *run_dir.glob("remediation/cycle-*/re_review/*/result.json")]
    records = {}
    for path in paths:
        parts = path.relative_to(run_dir).parts
        cycle = 1 if parts[0] == "reviews" else int(parts[1].split("-")[1]) + 1
        record = json.loads(path.read_text(encoding="utf-8"))
        findings = yaml.safe_load((path.parent.parent / f"{record['role']}_findings.yaml").read_text(encoding="utf-8"))
        records[record["role"]] = (cycle, record, findings)
    last = max(cycle for cycle, _, _ in records.values())
    assert len(work["reviews"]) == len(paths) and len(records) == len(paths)
    for review in work["reviews"]:
        cycle, record, findings = records[review["role"]]
        assert review == {
            "role": record["role"], "verdict": "findings" if findings else "pass", "finding_count": len(findings),
            "highest_severity": min((f["severity"] for f in findings), key=SEVERITIES.index, default=None),
            "earlier_cycle": cycle < last, "self_review": record["independence"]["independent"] is False}

    cycle_totals = {c: round(sum(r["duration_seconds"] for k, r, _ in records.values() if k == c), 3)
                    for c in {c for c, _, _ in records.values()}}
    assert len(work["review_cycles"]) > 1
    assert work["review_cycles"] == [
        {"cycle_index": c, "roles": sorted(r["role"] for k, r, _ in records.values() if k == c),
         "duration_seconds": cycle_totals[c]} for c in sorted(cycle_totals)]

    attempt_files = sorted(run_dir.glob("implementation/attempts/*/attempt_record.json"))
    attempt = json.loads(attempt_files[-1].read_text(encoding="utf-8"))
    remediation = json.loads((run_dir / "remediation/cycle-01/result.json").read_text(encoding="utf-8"))
    assert work["provider_execution"] == {
        "agent_id": attempt["agent_id"], "resource_id": attempt["resource_id"], "attempt": attempt["attempt"],
        "attempts": len(attempt_files), "success": attempt["success"], "exit_code": attempt["exit_code"],
        "duration_seconds": attempt["duration_seconds"], "failure_class": attempt["failure_class"]}
    assert work["duration_seconds"] == pytest.approx(
        sum(json.loads(f.read_text(encoding="utf-8"))["duration_seconds"] for f in attempt_files)
        + remediation["duration_seconds"] + sum(cycle_totals.values()) + sum(s["duration_seconds"] for s in plan["steps"]))


def _bad_findings(content, role):
    return (lambda d: (d / f"reviews/{role}_findings.yaml").unlink() if content is None
            else _put(d, f"reviews/{role}_findings.yaml", content),
            lambda w, d: next(r for r in w["reviews"] if r["role"] == role) == {
                "role": role, "highest_severity": None, "finding_count": None, "earlier_cycle": True,
                "self_review": role == CORRECTNESS,
                "verdict": "findings" if role == CORRECTNESS else "findings_unrecorded"})


def _no_duration(w, d):
    return w["duration_seconds"] is None and [c["duration_seconds"] for c in w["review_cycles"]] == [None, None]


def _edit_reviews(**changes):
    def mutate(d):
        for role in (CORRECTNESS, SECURITY):
            _edit(d, f"reviews/{role}/result.json", **changes)
        _edit(d, "remediation/cycle-01/re_review/test-falsifier/result.json", **changes)
    return mutate


UNRECORDED = {
    "verification_missing": (lambda d: (d / "verification_result.json").unlink(),
                             lambda w, d: w["verification"] == {"status": "none", "steps": []}
                             and w["duration_seconds"] is None),
    "verification_corrupt": (lambda d: _put(d, "verification_result.json", "{not json"),
                             lambda w, d: w["verification"]["status"] == "unreadable" and w["verification"]["reason"]
                             and w["verification"]["steps"] == [] and w["duration_seconds"] is None),
    "review_durations_absent": (_edit_reviews(duration_seconds=...), _no_duration),
    "zero_duration_without_attempts": (_edit_reviews(duration_seconds=0.0, failover={"attempts": []}), _no_duration),
    "boolean_duration": (_edit_reviews(duration_seconds=True), _no_duration),
    "remediation_duration_absent": (lambda d: (d / "remediation/cycle-01/result.json").unlink(),
                                    lambda w, d: w["duration_seconds"] is None
                                    and all(c["duration_seconds"] is not None for c in w["review_cycles"])),
    "no_implementation_records": (lambda d: [p.unlink() for p in d.glob("implementation/attempts/*/*.json")],
                                  lambda w, d: w["provider_execution"] is None and w["duration_seconds"] is None),
    "no_review_records": (lambda d: [p.unlink() for p in d.glob("reviews/*/result.json")]
                          + [p.unlink() for p in d.glob("remediation/*/re_review/*/result.json")],
                          lambda w, d: w["review_cycles"] is None and w["reviews"] == []
                          and w["duration_seconds"] is None),
    **{f"{role}_findings_{label}": _bad_findings(content, role)
       for role in (CORRECTNESS, SECURITY) for label, content in (("missing", None), ("empty", ""), ("corrupt", ": [bad"))},
    **{f"reviewer_{status}": (lambda d, s=status: _edit(d, f"reviews/{SECURITY}/result.json", status=s),
                              lambda w, d: next(r for r in w["reviews"] if r["role"] == SECURITY)["verdict"] == "failed")
       for status in ("reviewer_failure", "malformed_output", "output_invalid")},
}


@pytest.mark.parametrize("case", sorted(UNRECORDED))
def test_resume_json_says_so_when_the_records_do_not_hold_a_value(tmp_path, monkeypatch, capsys, case):
    repo, run_dir = _resumable_run(tmp_path, monkeypatch)
    mutate, holds = UNRECORDED[case]
    mutate(run_dir)
    work = _resumed(capsys, tmp_path, repo)["work_result"]
    assert holds(work, run_dir), work


# --- an unknown --task-class is a usage error, refused before anything else happens ---

@pytest.mark.parametrize("as_json", [True, False])
def test_bad_task_class_is_refused_before_pin_repository_or_run_directory(tmp_path, monkeypatch, capsys, as_json):
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")
    pinned = []
    monkeypatch.setattr(cli, "_run_pinned_if_needed", lambda *args: pinned.append(args))  # a late check would reach it

    code = cli.main(["work", "Tighten auth checks", "--repo", str(repo), "--task-class", "bogus_class",
                     *(["--json"] if as_json else [])])
    captured = capsys.readouterr()

    assert code == 1 and captured.out == "" and pinned == []
    assert not (repo / ".task_runs").exists()
    assert "bogus_class" in captured.err and all(name in captured.err for name in VALID_TASK_CLASSES)
    assert "INTERNAL_ERROR" not in captured.err and "diagnostic" not in captured.err.lower()
    if as_json:
        error = json.loads(captured.err.strip().splitlines()[-1])
        assert error["schema"] == "howlplane.error/v1" and error["code"] == "USAGE_ERROR"


@pytest.mark.parametrize("task_class", sorted(VALID_TASK_CLASSES))
def test_every_valid_task_class_still_works(tmp_path, monkeypatch, capsys, task_class):
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")

    code = cli.main(["work", "Tighten auth checks", "--repo", str(repo), "--skip-doctor", "--json",
                     "--task-class", task_class])
    document = json.loads(capsys.readouterr().out)

    assert code in (0, 2, 3) and document["schema"] == WORK_RESULT_SCHEMA
    assert yaml.safe_load((Path(document["run_dir"]) / "task.yaml").read_text(encoding="utf-8"))["task_class"] == task_class


def test_cmd_work_itself_refuses_a_bad_task_class_before_any_repository_access(tmp_path, monkeypatch):
    repo = _init_test_git_repo(tmp_path / "repo")
    touched = []
    for name in ("_resolve_repo", "find_control_plane_root"):
        monkeypatch.setattr(cli, name, lambda *args, _n=name, **kwargs: touched.append(_n))
    args = cli.build_parser().parse_args(["work", "Tighten auth checks", "--repo", str(repo),
                                          "--task-class", "bogus_class"])

    with pytest.raises(OperatorFailure) as refused:
        cli.cmd_work(args)

    assert refused.value.error.code == "USAGE_ERROR" and "bogus_class" in refused.value.error.message
    assert touched == [] and not (repo / ".task_runs").exists()
