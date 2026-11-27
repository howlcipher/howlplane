"""A refused command never ran: `work` keeps the candidate and sends it to review (issues.md #33).

In HP-SELF-29B the Claude implementer ran `ls` outside its grants. The CLI
refused it and carried on, but HowlPlane failed the attempt and rolled back a
finished 189-line candidate. The owner's rule (2026-11-26): when an attempt
fails only because commands were refused and it left a candidate, the
refusals are evidence, and review and verification decide the candidate.
With no candidate the attempt fails as before. No grant changes.

The backend below is the real `ClaudeCodeBackend` result handling over a
scripted CLI run, so the marker the orchestrator reads is the one Claude
Code's own result envelope produces.
"""

import json
from pathlib import Path

import pytest

from howlplane.control_plane.agent_execution import (
    REFUSED_COMMANDS_KEY,
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_KEY,
    AgentExecutionResult,
    ClaudeCodeBackend,
    FakeAgentBackend,
    SubprocessAgentBackend,
)
from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from howlplane.control_plane.resource_models import ProviderFailureClass
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec
from tests._tia_helpers import record_valid_tia
from tests.test_closed_loop_orchestrator import _init_test_git_repo

pytestmark = pytest.mark.integration

REFUSED = "ls /tmp/pytest-of-root"
ORIGINAL = "def authenticate(username, token):\n    return False\n"
FINDING = "findings:\n  - id: F1\n    title: Still broken\n    severity: high\n    category: correctness\n"


def keep_behavior(repo: Path, call: int) -> None:
    """A real edit that keeps the fixture's test passing; each call changes the file again."""
    path = repo / "src/auth.py"
    path.write_text(path.read_text() + f"# reviewed change {call}\n")


def break_behavior(repo: Path, call: int) -> None:
    (repo / "src/auth.py").write_text("def authenticate(username, token):\n    return True\n")


class _ScriptedClaudeRun(SubprocessAgentBackend):
    """Stands in for the Claude CLI process: makes its edits, then prints its result envelope."""

    def execute(self, task, cwd, role="implementation", **kwargs):
        self.calls.append(role)
        if self.edits.get(role):
            self.edits[role](Path(cwd), len(self.calls))
        denials = [{"tool_name": "Bash", "tool_input": {"command": REFUSED}}] if role in self.refuse else []
        envelope = {"type": "result", "result": "Done.", "permission_denials": denials}
        return AgentExecutionResult(agent_id=self.agent_id, role=role, command="claude", exit_code=0,
                                    stdout=json.dumps(envelope), stderr="", duration_seconds=0.1, success=True)


class RefusedClaude(ClaudeCodeBackend, _ScriptedClaudeRun):
    """`ClaudeCodeBackend.execute` interprets the scripted run exactly as it would a real one."""

    def __init__(self, edits, refuse=("implementation", "remediation")):
        super().__init__()
        self.edits, self.refuse, self.calls = edits, set(refuse), []

    def is_available(self):
        return True


def run(tmp_path, backend, review=FINDING, cycles=1, attempts=1):
    """Runs one governed task with a pool; returns the result, its evidence actions and the pool."""
    repo = _init_test_git_repo(tmp_path / "repo")
    pool = ProviderPoolManager(probe_on_start=False)
    orchestrator = GovernedTaskOrchestrator(repo, control_plane_root=tmp_path / "control_plane", config=OrchestrationConfig(
        custom_backend=backend, provider_pool=pool, custom_reviewer_fn=lambda role, diff, task: review,
        max_remediation_cycles=cycles, max_provider_failover_attempts=attempts, acquire_locks=False,
    ))
    task = TaskSpec(task_id="T-33", repository="fixture", objective="Fix a real defect")
    record_valid_tia(orchestrator.ledger, task.task_id)  # the completion gate requires a TIA
    result = orchestrator.run(task)
    events = [(e.action, e.metadata) for e in orchestrator.ledger.list_all_entries()]
    return result, events, pool, repo


def actions(events, name):
    return [metadata for action, metadata in events if action == name]


def first_attempt(result, pool):
    """The first implementation attempt's saved result, and the pool state of the resource that ran it."""
    resource_id = result.implementation_attempts[0]["resource_id"]
    saved = Path(result.run_dir) / "implementation" / "attempts" / f"01-{resource_id}" / "result.json"
    return json.loads(saved.read_text()), pool.get_resource_status(resource_id)


def test_a_refused_command_no_longer_costs_a_finished_candidate(tmp_path):
    backend = RefusedClaude({"implementation": keep_behavior})
    result, events, pool, _ = run(tmp_path, backend, review="findings: []\n")

    assert result.final_state == "complete"
    assert backend.calls == ["implementation"], "the candidate is reviewed, not retried elsewhere"
    assert result.review_cycles and result.verification_plan.overall_status == "passed"
    saved, state = first_attempt(result, pool)
    assert saved["success"] is True and saved["metadata"][REFUSED_COMMANDS_KEY] == [REFUSED]
    assert result.implementation_attempts[0][REFUSED_COMMANDS_KEY] == [REFUSED], "shown with the attempt"
    assert TOOL_PERMISSION_KEY not in saved["metadata"]
    assert [m[REFUSED_COMMANDS_KEY] for m in actions(events, "implementation_refusals_recorded")] == [[REFUSED]]
    assert state.consecutive_failures == 0 and not state.role_exclusions, "the provider is not charged"


def test_a_refusal_that_left_no_candidate_still_fails(tmp_path):
    result, events, pool, _ = run(tmp_path, RefusedClaude({}), review="findings: []\n")

    assert result.final_state == "failed"
    assert result.implementation_attempts[0]["failure_class"] == ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED.value
    assert not actions(events, "implementation_refusals_recorded")
    assert first_attempt(result, pool)[1].consecutive_failures == 1


def test_verification_still_rejects_a_refused_attempts_candidate(tmp_path):
    result, events, _, _ = run(tmp_path, RefusedClaude({"implementation": break_behavior}), review="findings: []\n")

    assert actions(events, "implementation_refusals_recorded"), "the candidate reached the gates"
    assert result.verification_plan.overall_status != "passed"
    assert result.final_state != "complete"


def test_review_findings_still_gate_a_refused_remediation(tmp_path):
    backend = RefusedClaude({"implementation": keep_behavior, "remediation": keep_behavior})
    result, events, _, _ = run(tmp_path, backend)

    assert backend.calls == ["implementation", "remediation"]
    assert [m["cycle"] for m in actions(events, "remediation_refusals_recorded")] == [1]
    assert len(result.review_cycles) == 2, "the remediated candidate went back to review"
    assert result.final_state != "complete", "the reviewer's finding still stands"


def test_a_refused_remediation_that_changed_nothing_still_fails(tmp_path):
    backend = RefusedClaude({"implementation": keep_behavior})
    result, events, _, repo = run(tmp_path, backend)

    assert backend.calls == ["implementation", "remediation"]
    assert result.final_state == "failed"
    assert not actions(events, "remediation_refusals_recorded")
    assert actions(events, "remediation_failed")
    assert (repo / "src/auth.py").read_text() == ORIGINAL, "the failed task is rolled back as before"


class UnmarkedDenial(FakeAgentBackend):
    """A backend that reports a denial without the refusal-only marker, as every other CLI does."""

    def execute(self, task, cwd, role="implementation", **kwargs):
        result = super().execute(task, cwd, role=role, **kwargs)
        keep_behavior(Path(cwd), 1)
        result.success, result.metadata = False, {TOOL_PERMISSION_KEY: TOOL_PERMISSION_DENIED}
        return result


def test_a_denial_without_the_marker_fails_as_before(tmp_path):
    result, events, _, repo = run(tmp_path, UnmarkedDenial(agent_id="claude_code"), review="findings: []\n")

    assert result.final_state == "failed"
    assert not actions(events, "implementation_refusals_recorded")
    assert (repo / "src/auth.py").read_text() == ORIGINAL
