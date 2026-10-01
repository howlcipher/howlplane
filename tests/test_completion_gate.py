"""#83: a code-changing governed task cannot reach ``complete`` without a valid TIA."""

from pathlib import Path

import pytest
import yaml

from howlplane.control_plane.agent_execution import FakeAgentBackend
from howlplane.control_plane.completion_gate import (
    GATE_BLOCKED_ACTION,
    evaluate_completion_gate,
    is_code_changing,
)
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.human_boundary import HumanLifecycleManager
from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from howlplane.control_plane.task_spec import TaskSpec
from tests._git_test_helpers import init_git_repo
from tests._tia_helpers import record_valid_tia
from tests.test_human_approval_lifecycle import _create_awaiting_human_task_run, _init_git_repo

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "paths,expected",
    [
        ([], False),
        (["README.md"], False),
        (["docs/guide/x.png.md", "documentation/TESTING.md", "notes.txt"], False),
        (["src/app.py"], True),
        (["README.md", "src/app.py"], True),
        (["pyproject.toml"], True),
        (["schemas/x.json"], True),
        (["tests/test_x.py"], True),
        (["Makefile"], True),
        (["docs/script.py"], False),  # under docs/: documentation tree
    ],
)
def test_code_change_classification_is_path_based(paths, expected):
    assert is_code_changing(paths) is expected


def test_gate_fails_closed_without_ledger_for_code_change():
    result = evaluate_completion_gate(None, "T-1", ["src/a.py"])
    assert not result.allowed and result.code_changing


def test_gate_allows_doc_only_without_assessment(tmp_path):
    ledger = EvidenceLedger(str(tmp_path / "l.jsonl"))
    assert evaluate_completion_gate(ledger, "T-1", ["README.md"]).allowed


def _repo(tmp_path: Path) -> Path:
    return init_git_repo(
        tmp_path / "repo",
        files={"AGENTS.md": "# ctx\n", "src/__init__.py": "", "src/a.py": "x = 1\n", "README.md": "# r\n"},
    )


def _orchestrator(repo: Path, tmp_path: Path, write):
    backend = FakeAgentBackend(agent_id="claude_code", side_effect=lambda task, cwd, prompt: write(cwd))
    return GovernedTaskOrchestrator(
        target_repo=repo,
        control_plane_root=tmp_path / "cp",
        config=OrchestrationConfig(
            custom_backend=backend,
            custom_reviewer_fn=lambda role, diff, task: "findings: []\n",
            acquire_locks=False,
            skip_doctor=True,
            enable_howlframe_audit=False,
        ),
    )


def _code_edit(cwd: Path):
    (cwd / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")


def _doc_edit(cwd: Path):
    (cwd / "README.md").write_text("# changed\n", encoding="utf-8")


def _spec(task_id: str) -> TaskSpec:
    return TaskSpec(task_id=task_id, repository="repo", objective="change", task_class="feature", risk_level="low")


def _actions(orch, task_id):
    return [e.action for e in orch.ledger.get_task_entries(task_id)]


def test_stage8_blocks_code_change_without_tia(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _code_edit)
    res = orch.run(_spec("G-1"))
    assert res.final_state == "blocked"
    assert res.exit_code == 1
    assert "Test Impact Assessment" in res.error_message
    assert "howlplane tia record --task-id G-1" in res.error_message
    actions = _actions(orch, "G-1")
    assert GATE_BLOCKED_ACTION in actions
    assert "task_completed" not in actions


def test_stage8_blocks_invalid_tia(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _code_edit)
    # Bypass record_assessment's own validation to plant a corrupt latest entry.
    from howlplane.control_plane.evidence_ledger import EvidenceEntry

    orch.ledger.append_entry(
        EvidenceEntry(task_id="G-2", agent_id="agent", action="test_impact_assessed",
                      metadata={"tia": {"schema": "x", "rationale": ""}})
    )
    res = orch.run(_spec("G-2"))
    assert res.final_state == "blocked"
    assert "invalid" in res.error_message
    assert "task_completed" not in _actions(orch, "G-2")


def test_stage8_completes_with_valid_tia(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _code_edit)
    record_valid_tia(orch.ledger, "G-3")
    res = orch.run(_spec("G-3"))
    assert res.final_state == "complete"
    assert "task_completed" in _actions(orch, "G-3")


def test_stage8_completes_doc_only_change_without_tia(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _doc_edit)
    res = orch.run(_spec("G-4"))
    assert res.final_state == "complete"
    assert GATE_BLOCKED_ACTION not in _actions(orch, "G-4")


def test_blocked_task_can_complete_after_tia_recorded(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _code_edit)
    assert orch.run(_spec("G-5")).final_state == "blocked"
    record_valid_tia(orch.ledger, "G-5")
    res = orch.run(_spec("G-5"))
    assert res.final_state == "complete"


def test_implementation_prompt_names_the_real_tia_command(tmp_path):
    orch = _orchestrator(_repo(tmp_path), tmp_path, _code_edit)
    prompt = orch._build_implementation_prompt(_spec("G-6"))
    assert "howlplane tia record --task-id G-6" in prompt
    assert str(orch.ledger.ledger_file) in prompt


# ---- human-approved resume path -------------------------------------------

def _approve(tmp_path, ledger, task_id):
    HumanLifecycleManager.approve(tmp_path, task_id, reason="ok", operator_source="cli", ledger=ledger)


def test_resume_cannot_bypass_tia(tmp_path):
    _init_git_repo(tmp_path)
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    _create_awaiting_human_task_run(tmp_path, "R-1")
    _approve(tmp_path, ledger, "R-1")
    res = HumanLifecycleManager.resume(tmp_path, "R-1", ledger=ledger)
    assert res.final_state == "blocked"
    assert "Test Impact Assessment" in res.error_message
    actions = [e.action for e in ledger.get_task_entries("R-1")]
    assert GATE_BLOCKED_ACTION in actions
    assert "task_completed" not in actions and "task_resumed" not in actions
    task_yaml = yaml.safe_load((tmp_path / ".task_runs" / "R-1" / "task.yaml").read_text(encoding="utf-8"))
    assert task_yaml["current_state"] == "blocked"


def test_resume_without_ledger_fails_closed_for_code_change(tmp_path):
    _init_git_repo(tmp_path)
    _create_awaiting_human_task_run(tmp_path, "R-2")
    HumanLifecycleManager.approve(tmp_path, "R-2", reason="ok", operator_source="cli")
    res = HumanLifecycleManager.resume(tmp_path, "R-2")
    assert res.final_state == "blocked"


def test_resume_with_valid_tia_completes(tmp_path):
    _init_git_repo(tmp_path)
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    _create_awaiting_human_task_run(tmp_path, "R-3")
    record_valid_tia(ledger, "R-3")
    _approve(tmp_path, ledger, "R-3")
    res = HumanLifecycleManager.resume(tmp_path, "R-3", ledger=ledger)
    assert res.final_state == "complete"
    assert "task_completed" in [e.action for e in ledger.get_task_entries("R-3")]
