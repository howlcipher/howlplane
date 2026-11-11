"""`howlplane work --implementation-timeout SECONDS`: a bounded, recorded limit (issues.md #31)."""

import json
import os
import site
import subprocess
import sys

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.agent_execution import FakeAgentBackend
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.human_boundary import HumanLifecycleManager
from howlplane.control_plane.orchestration import MAX_EXECUTION_BUDGET_SECONDS
from howlplane.control_plane.orchestrator import OrchestrationConfig, compute_remediation_timeout
from tests._control_plane_home import private_control_plane
from tests._tia_helpers import record_valid_tia
from tests.test_closed_loop_orchestrator import _init_test_git_repo, _use_unprobed_pool
from tests.test_self_host_isolation import ENTRY_SCRIPT, copy_package, git
from tests.test_work_result_contract import MEDIUM

pytestmark = pytest.mark.unit

FLAG = "--implementation-timeout"
LIMITS = "execution_limits.json"


class RecordingBackend(FakeAgentBackend):
    """A fake provider that records the time limit it is given for every role."""

    def __init__(self, **kwargs):
        super().__init__(agent_id="claude_code", side_effect=self._touch, **kwargs)
        self.limits = []

    @staticmethod
    def _touch(task, cwd, prompt):
        (cwd / "src" / "auth.py").write_text("def authenticate(u, t):\n    return bool(u) and False\n", encoding="utf-8")

    def execute(self, task, cwd, role="implementation", **kwargs):
        self.limits.append((role, kwargs.get("timeout_seconds")))
        return super().execute(task, cwd, role=role, **kwargs)


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    """A repository, a recording provider and `run(*argv)`; reviewers raise a finding on request."""
    _use_unprobed_pool(monkeypatch)
    private_control_plane(monkeypatch, tmp_path)
    repo = _init_test_git_repo(tmp_path / "repo")
    state = {"backend": RecordingBackend(), "finding": False, "cycles": 0}
    real_config = cli.OrchestrationConfig

    def config(**kwargs):
        return real_config(
            **kwargs, custom_backend=state["backend"], max_remediation_cycles=state["cycles"],
            custom_reviewer_fn=lambda role, diff, task: MEDIUM if state["finding"] and role == "test-falsifier"
            else "findings: []\n",
        )

    monkeypatch.setattr(cli, "OrchestrationConfig", config)

    def run(*argv, **options):
        state.update(options)
        if "timed_out" in options:
            state["backend"] = RecordingBackend(default_timed_out=True, default_exit_code=1)
        code = cli.main(["work", "Tighten auth checks", "--repo", str(repo), "--skip-doctor", *argv])
        out = capsys.readouterr()
        return code, out

    run.repo, run.state, run.tmp_path = repo, state, tmp_path
    return run


def _doc(out):
    return json.loads(out.out)


def _limits_file(doc):
    from pathlib import Path
    return json.loads((Path(doc["run_dir"]) / LIMITS).read_text(encoding="utf-8"))


@pytest.mark.parametrize("flags, expected", [
    ((), 600), ((FLAG, "1"), 1), ((FLAG, "240"), 240), ((FLAG, str(MAX_EXECUTION_BUDGET_SECONDS)), 1800),
])
def test_implementation_deadline_reaches_the_provider_and_the_record(world, flags, expected):
    code, out = world("--execute", "--json", *flags)

    doc = _doc(out)
    # The run may park on the missing Test Impact Assessment; the limit is what matters here.
    assert doc["schema"] == "howlplane.work_result/v1" and code == doc["exit_code"]
    assert world.state["backend"].limits[0] == ("implementation", expected)
    assert doc["implementation_timeout_seconds"] == expected
    assert _limits_file(doc) == {"implementation_timeout_seconds": expected}


@pytest.mark.parametrize("flags, expected", [((), 690), ((FLAG, "100"), 190), ((FLAG, "1"), 3)])
def test_remediation_attempt_gets_the_timeout_derived_from_the_limit(world, flags, expected):
    # One medium finding (60s) and one touched file (30s) on top of the limit, capped at 3x the limit.
    world("--execute", "--json", *flags, finding=True, cycles=1)

    remediation = [limit for role, limit in world.state["backend"].limits if role == "remediation"]
    assert remediation and remediation[0] == expected


@pytest.mark.parametrize("config, expected", [
    (dict(timeout_seconds=1800, run_mode="bounded"), 300),  # the bounded-run ceiling beats 3 x 1800
    (dict(timeout_seconds=10), 30),  # the multiplier ceiling: 10 + 60 would be 70
    (dict(timeout_seconds=1800), 1860),  # a long limit adds only the one medium finding (60s)
])
def test_derived_remediation_limit_keeps_its_ceilings(config, expected):
    findings = [type("Finding", (), {"severity": "medium"})()]
    assert compute_remediation_timeout(OrchestrationConfig(**config), findings, []) == expected


def test_a_timed_out_run_still_names_the_applied_limit_without_json(world):
    code, out = world("--execute", FLAG, "7", timed_out=True)

    assert code != 0
    record = next(world.repo.glob(f".task_runs/*/{LIMITS}"))
    assert json.loads(record.read_text(encoding="utf-8")) == {"implementation_timeout_seconds": 7}
    assert world.state["backend"].limits[0] == ("implementation", 7)


@pytest.mark.parametrize("flags, expected", [((), 600), ((FLAG, "45"), 45)])
def test_dry_run_json_carries_the_limit_and_records_it(world, flags, expected):
    code, out = world("--dry-run", "--json", *flags)

    doc = _doc(out)
    assert code == 0 and doc["final_state"] == "prepared"
    assert doc["implementation_timeout_seconds"] == expected
    assert _limits_file(doc) == {"implementation_timeout_seconds": expected}
    assert world.state["backend"].limits == []  # a dry run never reaches the provider


@pytest.mark.parametrize("value", ["0", "1801", "-5", "abc", "10.5", "true", ""])
@pytest.mark.parametrize("as_json", [False, True])
def test_a_bad_value_is_refused_before_anything_is_created(world, value, as_json):
    code, out = world("--dry-run", FLAG, value, *(["--json"] if as_json else []))

    assert code == 1 and out.out == ""
    assert "1 to 1800" in out.err
    assert not list(world.repo.glob(".task_runs*")) and world.state["backend"].limits == []
    if as_json:
        error = json.loads(out.err.strip().splitlines()[-1])
        assert error["schema"] == "howlplane.error/v1" and error["code"] == "USAGE_ERROR"


@pytest.mark.parametrize("value, ok", [(5, True), ("5", True), (1800, True), (True, False), (False, False),
                                       (0, False), (1801, False), (7.5, False), (None, False)])
def test_the_parser_accepts_ints_and_never_raises_a_type_error(value, ok):
    if ok:
        assert cli.parse_implementation_timeout(value) == int(value)
    else:
        with pytest.raises(cli.OperatorFailure) as failure:
            cli.parse_implementation_timeout(value)
        assert failure.value.error.code == "USAGE_ERROR"


def test_interop_lists_the_flag_for_the_work_command(capsys):
    assert cli.main(["interop", "--json"]) == 0

    commands = {tuple(c["path"]): c for c in json.loads(capsys.readouterr().out)["commands"]}
    assert FLAG in commands[("work",)]["flags"]


@pytest.mark.parametrize("keep_record, expected", [(True, 75), (False, None)])
def test_resume_json_reports_the_limit_read_from_the_run_record(world, capsys, keep_record, expected):
    code, out = world("--execute", "--json", FLAG, "75", finding=True)
    doc = _doc(out)
    assert code == 2 and doc["implementation_timeout_seconds"] == 75
    if not keep_record:
        (world.repo / ".task_runs" / doc["task_id"] / LIMITS).unlink()
    ledger_file = world.tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))
    record_valid_tia(ledger, doc["task_id"])
    HumanLifecycleManager.approve(target_repo=world.repo, task_id=doc["task_id"], reason="accepted",
                                  operator_source="cli", ledger=ledger)

    resumed = cli.main(["resume", doc["task_id"], "--repo", str(world.repo),
                        "--ledger-file", str(ledger_file), "--json"])

    assert resumed == 0
    resume_doc = json.loads(capsys.readouterr().out)
    assert resume_doc["schema"] == "howlplane.orchestrator/v1"  # existing output unchanged
    assert resume_doc["work_result"]["implementation_timeout_seconds"] == expected
    assert world.state["backend"].limits.count(("implementation", 75)) == 1  # resume re-runs no implementation


def test_a_refusal_in_the_real_entry_point_creates_no_pin_or_worktree(tmp_path):
    repo = tmp_path / "selfhost"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.test")
    copy_package(repo / "src/howlplane")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "feature: self host copy")
    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir()
    home.mkdir()
    (home / "AGENTS.md").write_text("rules")
    env = {k: v for k, v in os.environ.items() if k not in ("HOWLPLANE_PINNED_TOOL", "PYTHONPATH")}
    env.update(HOME=str(home), PYTHONUSERBASE=site.getuserbase(), HOWLPLANE_HOME=str(home),
               XDG_DATA_HOME=str(data))

    result = subprocess.run(
        [sys.executable, "-c", ENTRY_SCRIPT, str(repo / "src"), "work", "Do it", "--repo", str(repo), FLAG, "1801"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 1 and "1 to 1800" in result.stderr
    assert not (data / "howlplane" / "tool").exists()
    assert not (repo / ".task_runs").exists()
