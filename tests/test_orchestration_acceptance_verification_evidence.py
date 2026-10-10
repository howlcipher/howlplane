"""HowlPlane verification evidence reaches acceptance (DOG-044).

The R004 acceptor had read-only tools and rejected a passing tree because the
orchestration prompt omitted the harness result. A failing result is reachable
here through a hand-built session document; normal orchestration reworks it
before acceptance.
"""

import pytest

from howlplane.control_plane import orchestration as module
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import install_all
from tests.test_orchestration_handoff_recovery import only
from tests.test_orchestration_review_diff_evidence import RecordingBackend, run_session


pytestmark = pytest.mark.contract


def acceptance_prompt(tmp_path, monkeypatch, tests, repo=None, verify=None):
    repo = repo or repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    backend = RecordingBackend(lambda _: None)
    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", staticmethod(lambda agent: backend))
    doc = module.setup(arguments(repo, input="Goal", orchestrator="codex", verify=verify, **only("codex")), repo)
    doc["tests"] = tests
    module.execute_assignment(doc, "acceptance", "codex", "m1", repo)
    return backend.prompts["acceptance"]


@pytest.mark.parametrize("code,output", [(0, "12 passed"), (1, "1 failed")])
def test_acceptance_receives_harness_result(tmp_path, monkeypatch, code, output):
    prompt = acceptance_prompt(tmp_path, monkeypatch, [
        {"command": "pytest -q", "exit_code": code, "output": output + "\n"},
    ])

    assert "harness evidence, not implementer claims" in prompt
    assert f"`pytest -q` exit {code}" in prompt and output in prompt


@pytest.mark.parametrize("diff_check", [False, True])
def test_acceptance_says_when_no_verification_ran(tmp_path, monkeypatch, diff_check):
    tests = [{"command": "git diff --check", "exit_code": 0, "output": ""}] if diff_check else []
    prompt = acceptance_prompt(tmp_path, monkeypatch, tests)

    assert "no verification command ran on the current tree" in prompt
    assert ("diff check ran; it is not a test command" in prompt) is diff_check
    assert "git diff --check` exit" not in prompt


def test_only_latest_result_for_each_command_is_shown(tmp_path, monkeypatch):
    prompt = acceptance_prompt(tmp_path, monkeypatch, [
        {"command": "pytest -q", "exit_code": 1, "output": "old failure"},
        {"command": "pytest -q", "exit_code": 0, "output": "latest pass"},
    ])

    assert "`pytest -q` exit 0" in prompt and "latest pass" in prompt
    assert "exit 1" not in prompt and "old failure" not in prompt


def test_changed_resume_command_is_marked_as_the_active_gate(tmp_path, monkeypatch):
    prompt = acceptance_prompt(tmp_path, monkeypatch, [
        {"command": "pytest old-suite", "exit_code": 0, "output": "old command pass"},
        {"command": "pytest new-suite", "exit_code": 0, "output": "active command pass"},
    ], verify=["pytest", "new-suite"])

    assert "configured verification command is `pytest new-suite`" in prompt
    assert "`pytest old-suite` exit 0" in prompt
    assert "`pytest new-suite` exit 0" in prompt


def test_review_output_is_unchanged_with_and_without_results(tmp_path, monkeypatch):
    results = [{"command": "pytest -q", "exit_code": 0, "output": "12 passed\n"}]
    before = module.verification_evidence_for_review({"tests": results})
    assert module.verification_evidence_for_review({"tests": []}) == ""

    # Acceptance consumes the same result formatter, retaining review's exact text.
    repo = repository(tmp_path)
    assert acceptance_prompt(tmp_path, monkeypatch, results, repo).count(before) == 1


def test_r004_explicit_verify_pass_is_in_acceptance_prompt(tmp_path, monkeypatch, capsys):
    _, status, prompts = run_session(tmp_path, monkeypatch, lambda repo: (repo / "result.txt").write_text("done\n"))
    output, _ = capsys.readouterr()

    assert status == 0, output
    assert "The session's verification command is" not in prompts["acceptance"]
    assert "`true` exit 0" in prompts["acceptance"]
    assert "harness evidence, not implementer claims" in prompts["acceptance"]


def test_reconciliation_clears_results_before_acceptance(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    doc = module.setup(arguments(repo, input="Goal", orchestrator="codex", **only("codex")), repo)
    doc["tests"] = [{"command": "pytest -q", "exit_code": 0, "output": "stale pass"}]
    doc["repository_evidence"] = {"root": str(repo), "head": "stale", "status": ""}

    module.reconcile(doc, repo)
    prompt = acceptance_prompt(tmp_path, monkeypatch, doc["tests"], repo)

    assert doc["tests"] == []
    assert "stale pass" not in prompt
    assert "no verification command ran on the current tree" in prompt
