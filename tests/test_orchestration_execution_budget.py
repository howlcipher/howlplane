"""Execution budget vs provider capacity.

The observed defect: an AGY implementation stopped at HowlPlane's own 300s
assignment deadline (AGY's derived --print-timeout) while its account still had
more than 90% of both usage windows left, and orchestration recorded that as
AGY's implementation capacity being EXHAUSTED for the session. A deadline stop
is attempt evidence; only real quota, session, or rate evidence is capacity,
and only at the scope it proves.
"""

from datetime import timedelta

import pytest

from howlplane.control_plane import agent_readiness
from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import (
    BUDGET_DERIVED_KEY,
    TIMEOUT_SOURCE_BUDGET,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
    AgyBackend,
)
from howlplane.control_plane.task_spec import TaskSpec
from tests.test_orchestration import arguments, repository, result
from tests.test_orchestration_capability_recovery import accepted, events, install_all, persist


pytestmark = pytest.mark.contract


def deadline_stop(agent, role, source=TIMEOUT_SOURCE_HARNESS):
    outcome = result(agent, role, False, "")
    outcome.timed_out = True
    outcome.metadata = {TIMEOUT_SOURCE_KEY: source}
    if source == TIMEOUT_SOURCE_BUDGET:
        outcome.metadata[BUDGET_DERIVED_KEY] = True
    return outcome


def record_backend(monkeypatch):
    """Replace every backend with one that records (role, timeout, prompt) and succeeds."""
    calls = []

    class Recorder:
        def execute(self, task, cwd, role, prompt_override, timeout_seconds, model_id):
            calls.append((role, timeout_seconds, prompt_override))
            return accepted("recorder", role)

    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", lambda agent: Recorder())
    return calls


def only(*agents):
    """Reserve every agent except the named ones."""
    return {agent: "RESERVED" for agent in module.AGENTS if agent not in agents}


# Budget policy


def test_execution_budget_parses_per_role_and_global_values_within_a_finite_ceiling():
    assert module.parse_execution_budget(["implementation=600"]) == {"implementation": 600}
    assert module.parse_execution_budget(["450"]) == {role: 450 for role in module.ROLES}
    assert module.parse_execution_budget(None) == {}
    for bad in (["implementation=0"], [f"review={module.MAX_EXECUTION_BUDGET_SECONDS + 1}"],
                ["deploy=60"], ["implementation=soon"]):
        with pytest.raises(ValueError):
            module.parse_execution_budget(bad)


def test_default_budget_is_unchanged_and_an_override_reaches_the_backend_and_agy_print_timeout(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo, execution_budget=["implementation=600"]), repo)
    assert doc["execution_budget"] == {"planning": 300, "implementation": 600, "review": 300, "acceptance": 300}
    calls = record_backend(monkeypatch)
    module.execute_assignment(doc, "implementation", "agy", "m1", repo)
    module.execute_assignment(doc, "review", "agy", "m1", repo)
    assert [(role, timeout) for role, timeout, _ in calls] == [("implementation", 600), ("review", 300)]
    command = AgyBackend().build_command(TaskSpec(task_id="t", repository=str(repo), objective="o"), repo,
                                         "implementation", "prompt", timeout_seconds=600)
    assert command[command.index("--print-timeout") + 1] == "585s"


def test_a_manifest_budget_beyond_the_ceiling_is_invalid_not_clamped(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    doc["execution_budget"]["implementation"] = module.MAX_EXECUTION_BUDGET_SECONDS * 10
    with pytest.raises(module.SessionStateInvalid):
        module.normalize_session(doc)


# The AGY regression


def test_agy_derived_print_timeout_keeps_agy_capacity_and_eligibility(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    implementation = []

    def execute(document, role, agent, model, cwd):
        if role != "implementation":
            return accepted(agent, role)
        implementation.append((agent, model))
        # AGY stops at its budget-derived print timeout, leaving partial work.
        (cwd / "README").write_text("partial\n" if agent == "agy" else "done\n")
        return deadline_stop(agent, role, TIMEOUT_SOURCE_BUDGET) if agent == "agy" else result(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    args = arguments(repo, orchestrator="codex", policy="PLAN + EXECUTE", verify=["git", "diff", "--check"],
                     strategy="ECONOMY", codex="AUTO", agy="AUTO", devin_cli="AUTO",
                     claude_code="RESERVED", cursor="RESERVED")
    assert module.command(args) == 0

    # AGY timed out; another worker took over rather than AGY's next model.
    assert implementation[0] == ("agy", "m1") and implementation[1][0] != "agy"
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    attempt = next(item for item in doc["attempts"] if item["agent"] == "agy" and item["stage"] == "implementation")
    assert (attempt["state"], attempt["timeout_source"], attempt["budget_derived"], attempt["partial_changes"]) == (
        "TIMED_OUT", TIMEOUT_SOURCE_BUDGET, True, True)
    agy = doc["agents"]["agy"]
    assert agy["state"] == "AVAILABLE" and agy["capacity"] == {}
    assert not any(key.startswith("agy:") for key in doc["model_states"])
    # The identical assignment is barred; everything else about AGY is not.
    assert module.timed_out(doc, "implementation", "agy", "m1")
    assert ("agy", "m1") not in module.candidates(doc, "implementation")
    assert ("agy", "m2") in module.candidates(doc, "implementation")
    doc["implementer"] = "codex"
    assert ("agy", "m1") in module.candidates(doc, "review")
    # Nothing reached provider capacity evidence either.
    assert not agent_readiness.load_cache().get("agy", {}).get("limits")
    stderr = capsys.readouterr().err
    assert "AGY implementation stopped at the 300s execution budget (attempt only; capacity unchanged); partial changes kept" in stderr
    assert "EXHAUSTED" not in stderr and "capacity marked exhausted" not in stderr


def test_agy_stays_eligible_for_an_independent_task_after_a_timeout(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    first = module.setup(arguments(repo), repo)
    module.record_timeout(first, "implementation", "agy", "m1", 300)
    other = module.setup(arguments(repo, input="Rename the CLI flag"), repo)
    assert ("agy", "m1") in module.candidates(other, "implementation")


def test_partial_changes_from_a_timed_out_attempt_are_handed_to_the_next_worker(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    doc["attempts"].append({"stage": "implementation", "agent": "agy", "model": "m1", "state": "TIMED_OUT",
                            "failure": "EXECUTION_BUDGET_EXCEEDED", "partial_changes": True})
    calls = record_backend(monkeypatch)
    module.execute_assignment(doc, "implementation", "codex", "m1", repo)
    assert "reached its execution budget and left partial changes" in calls[0][2]


# No identical retry, across resume


def timed_out_session(repo):
    doc = module.setup(arguments(repo, orchestrator="agy", policy="PLAN + EXECUTE", **only("agy")), repo)
    doc["stage"], doc["status"] = "implementation", "IMPLEMENTATION"
    doc["orchestrator"] = "agy"
    doc["attempts"].append({"task_id": "t", "stage": "implementation", "agent": "agy", "model": "m1",
                            "state": "TIMED_OUT", "failure": "EXECUTION_BUDGET_EXCEEDED",
                            "started_at": module.now(), "finished_at": module.now()})
    module.record_timeout(doc, "implementation", "agy", "m1", 300)
    return doc


def test_resume_never_reruns_a_timed_out_assignment_unchanged(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    persist(timed_out_session(repo))
    monkeypatch.setattr(module, "execute_assignment", lambda *args: pytest.fail("identical assignment re-dispatched"))

    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 2
    captured = capsys.readouterr()
    assert "Status: HANDOFF REQUIRED" in captured.out
    assert "AGY: timed out at the 300s execution budget" in "\n".join(events(captured.err, "EXCLUDED"))


@pytest.mark.parametrize("change", [{"execution_budget": ["implementation=600"]}, {"retry_timeouts": True}])
def test_a_meaningful_change_permits_exactly_one_new_attempt(tmp_path, monkeypatch, change):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    persist(timed_out_session(repo))
    budgets = []

    def execute(document, role, agent, model, cwd):
        if role == "implementation":
            budgets.append(module.execution_budget(document, role))
            return deadline_stop(agent, role)
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, input="resume", orchestrator=None, **change)) == 2
    assert budgets == [600 if "execution_budget" in change else 300]
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    if "retry_timeouts" in change:
        assert doc["timeout_retries"][0]["cleared"][0]["agent"] == "agy"
    # The retry timed out again: that changed assignment is now barred as well.
    assert module.timed_out(doc, "implementation", "agy", "m1")


# Genuine capacity keeps its scope


def test_quota_exhaustion_is_model_scoped_and_recorded_as_provider_evidence(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)

    def execute(document, role, agent, model, cwd):
        if agent == "agy" and model == "m1":
            return result(agent, role, False, "quota exhausted")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, orchestrator="agy", **only("agy", "codex"))) == 0
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    assert doc["model_states"] == {"agy:m1": "EXHAUSTED"}
    assert "planning" not in doc["agents"]["agy"]["capacity"]
    assert ("agy", "m2") in module.candidates(doc, "planning")
    limits = agent_readiness.load_cache()["agy"]["limits"]
    assert [(item["state"], item["scope"], item["model"]) for item in limits] == [("QUOTA_EXHAUSTED", "model", "m1")]
    # A later session starts with that model already known exhausted, and only that model.
    later = module.setup(arguments(repo, input="Another goal"), repo)
    assert later["model_states"] == {"agy:m1": "EXHAUSTED"}
    assert later["agents"]["agy"]["state"] == "AVAILABLE"


def test_account_scope_limit_removes_the_agent_until_it_expires(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=())
    agent_readiness.record_session_outcome("agy", "UNKNOWN", "SESSION_LIMIT")
    doc = module.setup(arguments(repo), repo)
    assert doc["agents"]["agy"]["state"] == "UNAVAILABLE"
    assert doc["agents"]["codex"]["state"] == "AVAILABLE"
    cache = agent_readiness.load_cache()
    expired = agent_readiness.now() - timedelta(seconds=1)
    cache["agy"]["limits"][0]["expires_at"] = agent_readiness.iso(expired)
    agent_readiness.save_cache(cache)
    assert module.setup(arguments(repo), repo)["agents"]["agy"]["state"] == "AVAILABLE"


def test_execution_budget_never_writes_provider_capacity_evidence():
    agent_readiness.record_session_outcome("agy", "m1", "EXECUTION_BUDGET_EXCEEDED")
    assert not agent_readiness.load_cache().get("agy", {}).get("limits")


# Migration of sessions recorded with the old semantics


def test_v2_budget_exhaustion_migrates_to_attempt_evidence(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo, orchestrator="AUTO"), repo)
    doc["schema_version"] = 2
    for key in ("timed_out_assignments", "execution_budget"):
        doc.pop(key)
    at = module.now()
    doc["agents"]["agy"].update(state="DEGRADED", capacity={"implementation": {
        "state": "EXHAUSTED", "reason": "EXECUTION_BUDGET_EXCEEDED", "model": "m1", "evidence_time": at, "scope": "session"}})
    doc["agents"]["codex"].update(state="DEGRADED", capacity={"implementation": {
        "state": "EXHAUSTED", "reason": "SESSION_LIMIT", "model": "m1", "evidence_time": at, "scope": "session"}})
    module.record_capability_failure(doc, "claude_code", "EXECUTION_PERMISSION_REQUIRED")
    doc["agents"]["claude_code"]["capacity"] = {"planning": {
        "state": "EXHAUSTED", "reason": "EXECUTION_BUDGET_EXCEEDED", "model": "UNKNOWN", "evidence_time": at, "scope": "session"}}

    assert module.normalize_session(doc) == [f"Session manifest normalized from schema v2 → v{module.SCHEMA_VERSION}"]

    assert doc["agents"]["agy"]["capacity"] == {} and doc["agents"]["agy"]["state"] == "AVAILABLE"
    assert module.timed_out(doc, "implementation", "agy", "m1")
    assert ("agy", "m2") in module.candidates(doc, "implementation")
    # Real capacity and capability evidence survive untouched.
    assert doc["agents"]["codex"]["capacity"]["implementation"]["reason"] == "SESSION_LIMIT"
    assert doc["agents"]["claude_code"]["state"] == "DEGRADED"
    assert doc["agents"]["claude_code"]["capacity"] == {}
    assert doc["execution_budget"] == module.default_execution_budget()


# Selection evidence


def test_auto_selection_reports_observable_readiness_evidence(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    verified = agent_readiness.now() - timedelta(minutes=8)
    agent_readiness.save_cache({"agy": {"live_smoke": {"observed_at": agent_readiness.iso(verified), "status": "PASS",
                                                         "latency_seconds": 3.2, "exit_code": 0}}})
    monkeypatch.setattr(module, "execute_assignment", lambda document, role, agent, model, cwd: accepted(agent, role))
    assert module.command(arguments(repo, orchestrator="agy", **only("agy"))) == 0
    select = events(capsys.readouterr().err, "SELECT")
    assert any("AGY for planning: unattended verified · live smoke passed 8m ago · capacity UNKNOWN" in line
               for line in select), select
