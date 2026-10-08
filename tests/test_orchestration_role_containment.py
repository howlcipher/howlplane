"""AGY stays out of enforced read-only roles; worker network is an explicit mutating-role opt-in.

DOG-037: AGY review used ``--mode plan``, ran the project CLI, and rewrote tracked files.
DOG-038: Codex implementation had no network, and no option granted it.
"""

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import (
    AgyBackend,
    ClaudeCodeBackend,
    CodexBackend,
    CursorBackend,
    DevinCLIBackend,
    GeminiCLIBackend,
)
from howlplane.control_plane.task_spec import TaskSpec
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import accepted, budget_exceeded, install_all, persist
from tests.test_orchestration_handoff_recovery import only


pytestmark = pytest.mark.contract

EVERY_ROLE = ("planning", "implementation", "remediation", "review", "acceptance")
MUTATING = ("implementation", "remediation")
NETWORK = "sandbox_workspace_write.network_access=true"
BACKENDS = {
    "codex": CodexBackend, "agy": AgyBackend, "cursor": CursorBackend,
    "claude_code": ClaudeCodeBackend, "gemini_cli": GeminiCLIBackend, "devin_cli": DevinCLIBackend,
}
SKIP = "its CLI does not enforce read-only operation"


def tree(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    return repo


def note(assigned, role, agent, cwd):
    assigned.append((role, agent))
    if role == "implementation":
        (cwd / "README").write_text("implemented\n")
    return accepted(agent, role)


def bind(monkeypatch, execute):
    monkeypatch.setattr(module, "execute_assignment", execute)


def agy_readonly(assigned):
    return [role for role, agent in assigned if agent == "agy" and role in module.READ_ONLY_ROLES]


def run(tmp_path, monkeypatch, **overrides):
    repo = tree(tmp_path, monkeypatch)
    assigned = []
    bind(monkeypatch, lambda doc, role, agent, model, cwd: note(assigned, role, agent, cwd))
    values = dict(input="Update README", orchestrator=None, policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
                  verify=["true"])
    values.update(overrides)
    return module.command(arguments(repo, **values)), assigned


def opened(tmp_path, monkeypatch, **overrides):
    repo = tree(tmp_path, monkeypatch)
    return repo, module.setup(arguments(repo, **overrides), repo)


def watch(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, agent):
            self.agent = agent

        def execute(self, task, cwd, role="implementation", **kwargs):
            seen[(self.agent, role)] = task
            return accepted(self.agent, role)

    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", lambda agent: Recorder(agent))
    return seen


def carries_network(task, repo, agent, role):
    command = BACKENDS[agent]().build_command(task, repo, role, "prompt")
    return NETWORK in command


def test_agy_never_gets_a_read_only_role_in_auto_routing(tmp_path, monkeypatch, capsys):
    code, assigned = run(tmp_path, monkeypatch, **only("codex", "agy"))
    out, err = capsys.readouterr()

    assert ("implementation", "codex") in assigned and not agy_readonly(assigned)
    # The only non-implementer cannot be trusted read-only, so the audit is honestly blocked, not run.
    assert code == 2 and "AUDIT BLOCKED" in out and SKIP in out + err


def test_agy_can_still_audit_nothing_but_implement(tmp_path, monkeypatch, capsys):
    code, assigned = run(tmp_path, monkeypatch, **only("codex", "agy", "cursor"))

    assert code == 0 and "Status: COMPLETE" in capsys.readouterr().out
    assert ("review", "cursor") in assigned and not agy_readonly(assigned)


def test_explicitly_chosen_agy_orchestrator_plans_and_accepts_but_never_reviews(tmp_path, monkeypatch, capsys):
    code, assigned = run(tmp_path, monkeypatch, orchestrator="agy", **only("codex", "agy", "cursor"))

    assert code == 0
    assert ("planning", "agy") in assigned and ("acceptance", "agy") in assigned
    assert ("review", "agy") not in assigned


@pytest.mark.parametrize("orchestrator,role,eligible", [
    ("AUTO", "planning", False),
    ("AUTO", "review", False),
    ("AUTO", "acceptance", False),
    ("agy", "planning", True),
    ("agy", "review", False),
    ("agy", "acceptance", True),
])
def test_configured_read_only_fallback_cannot_select_unenforced_agy(
        tmp_path, monkeypatch, orchestrator, role, eligible):
    _, doc = opened(tmp_path, monkeypatch, orchestrator=orchestrator, fallbacks=f"{role}:agy:m1")
    present = ("agy", "m1") in module.candidates(doc, role)
    reason = module.capability_skip_reason(doc, "agy", role)

    assert present is eligible
    assert reason == ("explicit override" if eligible else SKIP)


def test_acceptance_takeover_skips_agy_after_auto_orchestrator_disqualification(tmp_path, monkeypatch):
    repo = tree(tmp_path, monkeypatch)
    assigned = []

    def execute(doc, role, agent, model, cwd):
        if role == "implementation" and agent == "cursor":
            assigned.append((role, agent))
            denied = accepted(agent, role)
            denied.success = False
            denied.exit_code = 1
            denied.stderr = "EXECUTION_PERMISSION_REQUIRED"
            return denied
        return note(assigned, role, agent, cwd)

    bind(monkeypatch, execute)
    args = arguments(repo, orchestrator="AUTO", strategy="ECONOMY", policy="PLAN + EXECUTE",
                     verify=["true"], **only("cursor", "agy", "codex"))
    assert module.command(args) == 0
    assert ("planning", "cursor") in assigned and ("implementation", "agy") in assigned
    assert ("acceptance", "codex") in assigned and not agy_readonly(assigned)


@pytest.mark.parametrize("stage,policy", [
    ("planning", "PLAN ONLY"),
    ("review", "PLAN + EXECUTE + INDEPENDENT AUDIT"),
    ("acceptance", "PLAN + EXECUTE"),
])
def test_resume_auto_session_selected_agy_never_dispatches_read_only_role_to_agy(
        tmp_path, monkeypatch, stage, policy):
    repo, doc = opened(tmp_path, monkeypatch, orchestrator="AUTO", policy=policy, **only("agy", "codex", "cursor"))
    # An older session selected AGY before the read-only eligibility rule existed.
    doc.update(orchestrator="agy", selected_orchestrator="agy", stage=stage, status=stage.upper())
    if stage != "planning":
        doc["implementer"] = "codex"
    persist(doc)
    assigned = []
    bind(monkeypatch, lambda document, role, agent, model, cwd: note(assigned, role, agent, cwd))

    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 0
    assert assigned and not agy_readonly(assigned)


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_network_reaches_only_mutating_roles(tmp_path, monkeypatch, enabled):
    repo = tree(tmp_path, monkeypatch)
    seen = watch(monkeypatch)
    doc = module.setup(arguments(repo, input="Fetch public data", orchestrator="codex",
                                 worker_network=enabled, **only("codex")), repo)
    assert doc["worker_network"] is enabled
    for role in EVERY_ROLE:
        module.execute_assignment(doc, role, "codex", "m1", repo)

    for role in MUTATING:
        assert bool(seen[("codex", role)].metadata.get("worker_network")) is enabled
    for role in module.READ_ONLY_ROLES:
        assert "worker_network" not in seen[("codex", role)].metadata


def test_codex_gets_sandbox_network_only_when_the_task_asks_for_it(tmp_path):
    plain = TaskSpec(task_id="T-1", repository="r", objective="o")
    networked = TaskSpec(task_id="T-2", repository="r", objective="o", metadata={"worker_network": True})

    assert not carries_network(plain, tmp_path, "codex", "implementation")
    command = CodexBackend().build_command(networked, tmp_path, "implementation", "p")
    assert command[command.index(NETWORK) - 1] == "-c" and command[-1] == "p"
    # Read-only roles stay in Codex's default read-only sandbox, network included.
    for role in ("planning", "review", "acceptance", "security-reviewer", "writing:summary"):
        assert not carries_network(networked, tmp_path, "codex", role)


@pytest.mark.parametrize("enabled", [False, True])
def test_resume_worker_network_only_configures_codex_mutating_roles(tmp_path, monkeypatch, enabled):
    repo, doc = opened(tmp_path, monkeypatch, orchestrator="codex")
    doc.pop("worker_network")  # A session created before the flag existed.
    persist(doc)
    seen = watch(monkeypatch)

    def inspect_resumed(document, path, cwd, progress):
        assert document.get("worker_network", False) is enabled
        for role in EVERY_ROLE:
            for agent in BACKENDS:
                module.execute_assignment(document, role, agent, "m1", cwd)
        return 0

    monkeypatch.setattr(module, "run", inspect_resumed)
    assert module.command(arguments(repo, input="resume", orchestrator=None, worker_network=enabled)) == 0
    for role in EVERY_ROLE:
        for agent in BACKENDS:
            task = seen[(agent, role)]
            assert carries_network(task, repo, agent, role) is (
                enabled and agent == "codex" and role in MUTATING)
            if role in module.READ_ONLY_ROLES:
                assert "worker_network" not in task.metadata


@pytest.mark.parametrize("role,policy,orchestrator,outcome", [
    ("planning", "PLAN ONLY", "agy", "ok"),
    ("planning", "PLAN + EXECUTE", "codex", "fail"),
    ("planning", "PLAN + EXECUTE", "codex", "timeout"),
    ("planning", "PLAN + EXECUTE", "agy", "ok"),
    ("review", "PLAN + EXECUTE + INDEPENDENT AUDIT", "codex", "ok"),
    ("review", "PLAN + EXECUTE + INDEPENDENT AUDIT", "codex", "findings"),
    ("acceptance", "PLAN + EXECUTE", "codex", "ok"),
    ("acceptance", "PLAN + EXECUTE", "codex", "reject"),
])
def test_read_only_repository_change_blocks_and_cannot_resume(
        tmp_path, monkeypatch, capsys, role, policy, orchestrator, outcome):
    repo = tree(tmp_path, monkeypatch)
    assigned = []

    def execute(doc, current, agent, model, cwd):
        if current != role:
            return note(assigned, current, agent, cwd)
        assigned.append((current, agent))
        (cwd / "read-only-write.txt").write_text("changed\n")
        if outcome == "fail":
            failed = accepted(agent, current)
            failed.success = False
            failed.exit_code = 1
            failed.stderr = "boom"
            return failed
        if outcome == "timeout":
            return budget_exceeded(agent, current)
        if outcome == "findings":
            found = accepted(agent, current)
            found.stdout = "BLOCKING: planner write\nAUDIT_STATUS: FINDINGS"
            return found
        if outcome == "reject":
            rejected = accepted(agent, current)
            rejected.stdout = "not ready\nACCEPTANCE_STATUS: REJECTED"
            return rejected
        return accepted(agent, current)

    bind(monkeypatch, execute)
    # A second planner and reviewer are available, so a missed block would reroute.
    code = module.command(arguments(
        repo, input="Update README", orchestrator=orchestrator, policy=policy, verify=["true"],
        **only("agy", "codex", "claude_code", "cursor")))
    out = capsys.readouterr().out
    order = ("planning", "implementation", "review", "acceptance")
    later = set(order[order.index(role) + 1:])

    assert code == 2 and "Status: BLOCKED" in out and "Resumable: no" in out
    assert "READ_ONLY_ROLE_MUTATED_REPOSITORY" in out and "Rework rounds:" not in out
    assert [item[0] for item in assigned if item[0] == role] == [role]
    assert not any(current in later for current, _agent in assigned)
    assert sum(current == "implementation" for current, _agent in assigned) == (0 if role == "planning" else 1)
    with pytest.raises(ValueError, match="No unfinished session"):
        module.command(arguments(repo, input="resume", orchestrator=None))


def test_planning_failure_without_a_write_stays_resumable(tmp_path, monkeypatch, capsys):
    repo = tree(tmp_path, monkeypatch)

    def execute(doc, role, agent, model, cwd):
        failed = accepted(agent, role)
        failed.success = False
        failed.exit_code = 1
        failed.stderr = "boom"
        return failed

    bind(monkeypatch, execute)
    code = module.command(arguments(repo, orchestrator="codex", policy="PLAN ONLY", **only("codex")))
    out = capsys.readouterr().out

    assert code == 2 and "Resumable: yes" in out and "READ_ONLY_ROLE_MUTATED_REPOSITORY" not in out


def test_worker_network_flag_parses_and_defaults_off():
    from howlplane.control_plane.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["orchestrate", "Goal", "--worker-network"]).worker_network is True
    assert parser.parse_args(["orchestrate", "Goal"]).worker_network is False
    assert parser.parse_args(["orchestrate", "resume", "--worker-network"]).worker_network is True
