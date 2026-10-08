"""Read-only roles go only to CLIs that enforce them; network is an explicit opt-in for mutating roles.

DOG-037 (howl-cubs-dogfood S1): AGY, assigned review with `--mode plan`, ran the project's
CLI, used the network and rewrote tracked files; the session ended BLOCKED and the verified
implementation was lost. DOG-038: the Codex implementer had no network, so a goal that must
fetch public data could not run, and no option granted it.
"""

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import CodexBackend
from howlplane.control_plane.task_spec import TaskSpec
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import accepted, install_all
from tests.test_orchestration_handoff_recovery import only


pytestmark = pytest.mark.contract


def run(tmp_path, monkeypatch, **overrides):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    assigned = []

    def execute(doc, role, agent, model, cwd):
        assigned.append((role, agent))
        if role == "implementation":
            (cwd / "README").write_text("implemented\n")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    values = dict(input="Update README", orchestrator=None, policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
                  verify=["true"])
    values.update(overrides)
    return repo, module.command(arguments(repo, **values)), assigned


def test_agy_never_gets_a_read_only_role_in_auto_routing(tmp_path, monkeypatch, capsys):
    repo, code, assigned = run(tmp_path, monkeypatch, **only("codex", "agy"))
    out, err = capsys.readouterr()

    assert ("implementation", "codex") in assigned
    assert not [role for role, agent in assigned if agent == "agy" and role in module.READ_ONLY_ROLES]
    # The only non-implementer cannot be trusted read-only, so the audit is honestly blocked, not run.
    assert code == 2 and "AUDIT BLOCKED" in out
    assert "its CLI does not enforce read-only operation" in out + err


def test_agy_can_still_audit_nothing_but_implement(tmp_path, monkeypatch, capsys):
    repo, code, assigned = run(tmp_path, monkeypatch, **only("codex", "agy", "cursor"))

    assert code == 0 and "Status: COMPLETE" in capsys.readouterr().out
    assert ("review", "cursor") in assigned
    assert all(agent != "agy" for role, agent in assigned if role in module.READ_ONLY_ROLES)


def test_explicitly_chosen_agy_orchestrator_plans_and_accepts_but_never_reviews(tmp_path, monkeypatch, capsys):
    repo, code, assigned = run(tmp_path, monkeypatch, orchestrator="agy", **only("codex", "agy", "cursor"))

    assert code == 0
    assert ("planning", "agy") in assigned and ("acceptance", "agy") in assigned
    assert ("review", "agy") not in assigned


def capture_task(monkeypatch):
    seen = {}

    class Backend:
        def execute(self, task, repo, role="implementation", **kwargs):
            seen[role] = dict(task.metadata)
            return accepted("codex", role)

    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", lambda agent: Backend())
    return seen


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_network_reaches_only_mutating_roles(tmp_path, monkeypatch, enabled):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    seen = capture_task(monkeypatch)
    args = arguments(repo, input="Fetch public data", orchestrator="codex", worker_network=enabled, **only("codex"))
    doc = module.setup(args, repo)
    assert doc["worker_network"] is enabled

    for role in ("planning", "implementation", "remediation", "review", "acceptance"):
        module.execute_assignment(doc, role, "codex", "m1", repo)

    for role in ("implementation", "remediation"):
        assert bool(seen[role].get("worker_network")) is enabled
    for role in ("planning", "review", "acceptance"):
        assert "worker_network" not in seen[role]


def test_codex_gets_sandbox_network_only_when_the_task_asks_for_it(tmp_path):
    backend = CodexBackend()
    plain = TaskSpec(task_id="T-1", repository="r", objective="o")
    networked = TaskSpec(task_id="T-2", repository="r", objective="o", metadata={"worker_network": True})
    setting = "sandbox_workspace_write.network_access=true"

    assert setting not in backend.build_command(plain, tmp_path, "implementation", "p")
    command = backend.build_command(networked, tmp_path, "implementation", "p")
    assert command[command.index(setting) - 1] == "-c" and command[-1] == "p"
    # Read-only roles stay in Codex's default read-only sandbox, network included.
    assert setting not in backend.build_command(networked, tmp_path, "review", "p")


def test_worker_network_flag_parses_and_defaults_off():
    from howlplane.control_plane.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["orchestrate", "Goal", "--worker-network"]).worker_network is True
    assert parser.parse_args(["orchestrate", "Goal"]).worker_network is False
