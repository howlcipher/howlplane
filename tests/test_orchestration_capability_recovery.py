"""Regression contracts for capability recovery, hard-failure routing, and manifest migration.

The observed failure: a session created before per-agent ``capabilities``
existed was resumed; budget and engineering failures were not recorded, AGY
was reassigned straight after EXECUTION_BUDGET_EXCEEDED under a REROUTE that
named another agent, and the next capability write raised ``KeyError:
'capabilities'``.
"""

import json

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import TIMEOUT_SOURCE_HARNESS, TIMEOUT_SOURCE_KEY
from tests.test_orchestration import arguments, repository, result


pytestmark = pytest.mark.contract


def budget_exceeded(agent, role):
    outcome = result(agent, role, False, "")
    outcome.timed_out = True
    outcome.metadata = {TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS}
    return outcome


def accepted(agent, role):
    outcome = result(agent, role)
    outcome.stdout = {"review": "AUDIT_STATUS: CLEAN", "acceptance": "ACCEPTANCE_STATUS: ACCEPTED"}.get(role, "plan")
    return outcome


def install_all(tmp_path, monkeypatch, models=("m1", "m2")):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(module, "discover_models", lambda agent: list(models))


def persist(doc):
    doc["retain_report"] = True
    doc["lease"]["pid"] = 999999
    doc["lease"]["renewed_at"] = 0
    root = module.state_root()
    with module.locked(root):
        path = module.path_for(root, doc["id"])
        module.secure_write(path, doc)
    return path


def downgrade_to_v1(doc):
    """Reshape a current manifest into the pre-capability (v1) layout seen on disk."""
    for key in ("schema_version", "requested_orchestrator", "selected_orchestrator", "capability_notices"):
        doc.pop(key, None)
    doc["agents"] = {agent: {key: state[key] for key in ("installed", "models", "state")} for agent, state in doc["agents"].items()}
    return doc


def attempt(stage, agent, model, state, failure=None):
    return {"task_id": "t", "stage": stage, "agent": agent, "model": model, "state": state,
            "failure": failure, "started_at": module.now(), "finished_at": module.now()}


def observed_v1_session(repo, **overrides):
    """The resumed session from the incident, as a v1 manifest stopped mid-implementation."""
    doc = module.setup(arguments(repo, orchestrator="codex", policy="PLAN + EXECUTE", verify=["git", "diff", "--check"], **overrides), repo)
    doc["stage"], doc["status"] = "implementation", "IMPLEMENTATION"
    doc["attempts"] = [
        attempt("planning", "claude_code", "UNKNOWN", "REVOKED", "EXECUTION_PERMISSION_REQUIRED"),
        attempt("planning", "codex", "UNKNOWN", "SUCCEEDED"),
        attempt("implementation", "codex", "UNKNOWN", "REVOKED", "NO_REPOSITORY_CHANGE"),
        attempt("implementation", "claude_code", "UNKNOWN", "REVOKED", "EXECUTION_BUDGET_EXCEEDED"),
        attempt("implementation", "cursor", "m1", "REVOKED", "ENGINEERING_FAILURE"),
        attempt("implementation", "cursor", "m2", "REVOKED", "ENGINEERING_FAILURE"),
        attempt("implementation", "agy", "m1", "REVOKED", "EXECUTION_BUDGET_EXCEEDED"),
        attempt("implementation", "agy", "m2", "ASSIGNED"),
    ]
    doc["reroutes"] = [{"from": f"{a['agent']}:{a['model']}", "stage": a["stage"], "reason": a["failure"]}
                       for a in doc["attempts"] if a["failure"]]
    return downgrade_to_v1(doc)


def events(stderr, label):
    return [line for line in stderr.splitlines() if f"] {label}" in line]


def assert_reroutes_match_assignments(stderr):
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        if "] REROUTE" in line and "→" in line:
            target = line.split("→")[-1].split()[0]
            following = next(later for later in lines[index + 1:] if "] ASSIGN" in later)
            assert following.split("ASSIGN")[-1].split()[0] == target, (line, following)


# Schema and normalization


def test_new_session_has_normalized_capability_structure(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    assert doc["schema_version"] == module.SCHEMA_VERSION
    for agent in module.AGENTS:
        state = doc["agents"][agent]
        assert state["capabilities"]["unattended_execution"] is None
        assert state["capacity"] == {}
    assert module.normalize_session(doc) == []


def test_manifest_missing_capabilities_normalizes_to_unknown_not_available(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = downgrade_to_v1(module.setup(arguments(repo), repo))
    del doc["agents"]["devin_cli"]
    doc["agents"]["cursor"]["capabilities"] = ["not", "a", "mapping"]

    notes = module.normalize_session(doc)

    assert notes == [f"Session manifest normalized from schema v1 → v{module.SCHEMA_VERSION}"]
    assert doc["schema_version"] == module.SCHEMA_VERSION
    for agent in module.AGENTS:
        assert doc["agents"][agent]["capabilities"]["unattended_execution"] is None
        assert doc["agents"][agent]["capacity"] == {}
    # An agent the old manifest never recorded gains no positive availability.
    assert doc["agents"]["devin_cli"]["state"] == "UNAVAILABLE"
    assert all(agent != "devin_cli" for agent, _ in module.candidates(doc, "planning"))


def test_capability_access_on_unnormalized_record_never_raises_key_error(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = downgrade_to_v1(module.setup(arguments(repo, orchestrator="AUTO"), repo))

    assert module.capability_skip_reason(doc, "agy", "implementation") is None
    module.record_capability_success(doc, "codex")
    module.record_capability_failure(doc, "claude_code", "EXECUTION_PERMISSION_REQUIRED")
    module.record_failure(doc, "agy", "implementation", "m1", "ENGINEERING_FAILURE")
    assert doc["agents"]["codex"]["capabilities"]["unattended_execution"] is True
    assert doc["agents"]["claude_code"]["capabilities"]["unattended_execution"] is False
    assert doc["agents"]["agy"]["capacity"]["implementation"]["state"] == "FAILED"


@pytest.mark.parametrize("corrupt", [
    lambda doc: doc["agents"].__setitem__("agy", "broken"),
    lambda doc: doc["agents"]["agy"].__setitem__("state", "MAYBE"),
    lambda doc: doc.pop("attempts"),
    lambda doc: doc["attempts"].append({"stage": "implementation"}),
    lambda doc: doc.__setitem__("schema_version", 99),
])
def test_unrecoverable_manifest_returns_structured_session_state_invalid(tmp_path, monkeypatch, capsys, corrupt):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    corrupt(doc)
    path = persist(doc)
    before = path.read_bytes()
    monkeypatch.setattr(module, "execute_assignment", lambda *args: pytest.fail("invalid state must not dispatch"))

    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 2

    output = capsys.readouterr().out
    assert "Status: SESSION_STATE_INVALID" in output
    assert "Reason: " in output and "KeyError" not in output
    assert path.read_bytes() == before


# Hard-failure routing


@pytest.mark.parametrize("agent", module.AGENTS)
def test_execution_budget_exceeded_times_out_the_attempt_and_falls_back_to_another_worker(tmp_path, monkeypatch, capsys, agent):
    """A deadline stop is attempt evidence: another worker takes over and capacity is untouched."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    calls = []

    def execute(document, role, name, model, cwd):
        calls.append((name, model))
        return budget_exceeded(name, role) if name == agent else accepted(name, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, orchestrator=agent)) == 0

    assert calls[0] == (agent, "m1")
    assert [name for name, _ in calls].count(agent) == 1
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    assert doc["agents"][agent]["capacity"] == {}
    assert doc["model_states"] == {}
    timed_out = doc["attempts"][0]
    assert (timed_out["state"], timed_out["failure"], timed_out["timeout_source"]) == (
        "TIMED_OUT", "EXECUTION_BUDGET_EXCEEDED", "harness")
    assert [(item["stage"], item["agent"], item["model"], item["execution_budget_seconds"])
            for item in doc["timed_out_assignments"]] == [("planning", agent, "m1", 300)]
    stderr = capsys.readouterr().err
    assert f"{module.AGENT_NAMES[agent]} planning stopped at the 300s execution budget (attempt only; capacity unchanged)" in stderr
    assert "capacity marked exhausted" not in stderr
    assert_reroutes_match_assignments(stderr)


def test_candidates_are_recomputed_after_hard_failure_including_configured_fallbacks(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo, orchestrator="AUTO", fallbacks="implementation:agy:m1,implementation:codex:m1"), repo)
    assert ("agy", "m1") in module.candidates(doc, "implementation")

    module.record_failure(doc, "agy", "implementation", "m1", "ENGINEERING_FAILURE")

    options = module.candidates(doc, "implementation")
    assert all(agent != "agy" for agent, _ in options)
    assert options[0] == ("codex", "m1")
    # Capacity is role-scoped: AGY stays eligible for independent review.
    assert any(agent == "agy" for agent, _ in module.candidates(doc, "review"))


def test_session_limit_exhausts_role_and_model(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo, orchestrator="AUTO"), repo)
    module.record_failure(doc, "claude_code", "implementation", "m1", "SESSION_LIMIT")
    assert doc["model_states"]["claude_code:m1"] == "EXHAUSTED"
    assert doc["agents"]["claude_code"]["capacity"]["implementation"]["state"] == "EXHAUSTED"
    assert module.capability_skip_reason(doc, "claude_code", "implementation") == "session limit reached"


def test_transient_failure_keeps_bounded_retry(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    calls = []

    def execute(document, role, agent, model, cwd):
        calls.append((agent, model))
        if agent == "agy":
            return result(agent, role, False, "connection refused")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, orchestrator="agy")) == 0
    assert calls.count(("agy", "m1")) == 2
    assert calls[2][0] != "agy"


# The observed incident


def test_observed_sequence_resumes_old_manifest_without_retrying_exhausted_workers(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = observed_v1_session(repo)
    goal, prior_attempts, prior_reroutes = doc["goal"], list(doc["attempts"]), list(doc["reroutes"])
    persist(doc)
    calls = []

    def execute(document, role, agent, model, cwd):
        calls.append((role, agent))
        if role == "implementation":
            assert agent == "devin_cli", f"{agent} was already excluded from implementation"
            (cwd / "README").write_text("changed\n")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 0

    assert calls == [("implementation", "devin_cli"), ("acceptance", "codex")]
    captured = capsys.readouterr()
    assert "capabilities" not in captured.out + captured.err
    assert "MIGRATE" in captured.err and f"schema v1 → v{module.SCHEMA_VERSION}" in captured.err
    resumed = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    assert resumed["status"] == "COMPLETE"
    assert resumed["goal"] == goal
    assert resumed["attempts"][:len(prior_attempts) - 1] == prior_attempts[:-1]
    assert resumed["attempts"][len(prior_attempts) - 1]["failure"] == "INTERRUPTED_OR_STALE_LEASE"
    assert resumed["reroutes"][:len(prior_reroutes)] == prior_reroutes
    agents = resumed["agents"]
    # The old budget stops replay as attempt evidence, never as role capacity.
    assert "implementation" not in agents["agy"]["capacity"]
    assert "implementation" not in agents["claude_code"]["capacity"]
    assert {(item["agent"], item["model"]) for item in resumed["timed_out_assignments"]} == {
        ("claude_code", "UNKNOWN"), ("agy", "m1")}
    assert agents["cursor"]["capacity"]["implementation"]["reason"] == "ENGINEERING_FAILURE"
    assert agents["codex"]["capacity"]["implementation"]["reason"] == "NO_REPOSITORY_CHANGE"
    # Replayed history carries negatives only; Codex's recorded planning success
    # is not promoted to confirmed unattended capability by the migration.
    assert resumed["migrations"][0]["from"] == 1
    assert agents["claude_code"]["capabilities"]["unattended_execution"] is False


def test_observed_sequence_live_run_reroutes_truthfully_and_never_repeats_agy(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    calls = []

    def execute(document, role, agent, model, cwd):
        calls.append((role, agent, model))
        if role != "implementation":
            return accepted(agent, role)
        if agent == "codex":
            return result(agent, role)
        if agent in {"claude_code", "agy"}:
            return budget_exceeded(agent, role)
        if agent == "cursor":
            return result(agent, role, False, "engine failure token=supersecret")
        (cwd / "README").write_text("changed\n")
        return result(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, orchestrator="codex", policy="PLAN + EXECUTE", verify=["git", "diff", "--check"])) == 0

    implementation = [(agent, model) for role, agent, model in calls if role == "implementation"]
    assert implementation == [("codex", "m1"), ("claude_code", "m1"), ("cursor", "m1"), ("agy", "m1"), ("devin_cli", "m1")]
    stderr = capsys.readouterr().err
    assert_reroutes_match_assignments(stderr)
    assert "] REROUTE     " in stderr and "AGY → Devin (EXECUTION_BUDGET_EXCEEDED)" in stderr
    assert "AGY implementation stopped at the 600s execution budget (attempt only; capacity unchanged)" in stderr
    assert "capacity marked exhausted" not in stderr
    assert "supersecret" not in stderr
    retained = module.state_root().glob("*.json")
    assert all("supersecret" not in path.read_text() for path in retained)


def test_all_workers_exhausted_hands_off_with_exclusions_instead_of_cycling(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    # One advertised model each, so the timed-out AGY assignment has no untried variant.
    install_all(tmp_path, monkeypatch, models=("m1",))
    persist(observed_v1_session(repo, devin_cli="RESERVED"))
    monkeypatch.setattr(module, "execute_assignment", lambda *args: pytest.fail("no eligible worker may be dispatched"))

    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 2

    captured = capsys.readouterr()
    assert "Status: HANDOFF REQUIRED" in captured.out
    assert "Excluded implementation workers" in captured.out
    excluded = "\n".join(events(captured.err, "EXCLUDED"))
    assert "AGY: timed out at the 300s execution budget; a retry needs a changed budget, model, or scope" in excluded
    assert "Claude: unattended execution unavailable" in excluded
    assert "Cursor: failed earlier this session (ENGINEERING_FAILURE)" in excluded
    assert "Codex: failed earlier this session (NO_REPOSITORY_CHANGE)" in excluded
    assert "Devin: reserved" in excluded
    assert "No eligible implementation workers remain" in captured.err
    assert "--execution-budget implementation=<seconds>" in captured.err
    assert "Timeout guidance:" in captured.out
    assert not events(captured.err, "ASSIGN")
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    assert doc["agents"]["devin_cli"]["state"] == "RESERVED"


def test_hard_failure_evidence_survives_resume_of_current_schema(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo, orchestrator="agy", policy="PLAN ONLY"), repo)
    module.record_failure(doc, "agy", "planning", "m1", "ENGINEERING_FAILURE")
    doc["attempts"].append(attempt("planning", "agy", "m1", "REVOKED", "ENGINEERING_FAILURE"))
    path = persist(doc)
    assert json.loads(path.read_text())["agents"]["agy"]["capacity"]["planning"]["state"] == "FAILED"
    calls = []
    monkeypatch.setattr(module, "execute_assignment",
                        lambda document, role, agent, model, cwd: calls.append(agent) or accepted(agent, role))

    assert module.command(arguments(repo, input="resume", orchestrator=None, policy="PLAN ONLY")) == 0
    assert calls and "agy" not in calls


def test_redacting_a_secret_bearing_failure_keeps_the_manifest_valid(tmp_path, monkeypatch):
    """Redacting serialized JSON must not swallow the closing quote of an already-redacted value."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    doc["attempts"].append(dict(attempt("planning", "codex", "m1", "REVOKED", "ENGINEERING_FAILURE"),
                                error=module.redact("engine failure token=supersecret"), next="kept"))
    path = persist(doc)
    module.save(path, doc, doc["lease"]["token"])
    stored = json.loads(path.read_text())
    assert stored["attempts"][-1]["error"] == "engine failure token=<redacted>"
    assert stored["attempts"][-1]["next"] == "kept"


def test_explicit_override_warning_is_announced_once_per_session(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=())
    doc = module.setup(arguments(repo, orchestrator="claude_code", policy="PLAN + EXECUTE + INDEPENDENT AUDIT"), repo)
    module.record_capability_failure(doc, "claude_code", "EXECUTION_PERMISSION_REQUIRED")
    persist(doc)

    def execute(document, role, agent, model, cwd):
        if role == "planning" and agent == "claude_code":
            return result(agent, role, False, "execution permission required")
        if role == "implementation":
            (cwd / "README").write_text("changed\n")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, input="resume", orchestrator=None, verify=["git", "diff", "--check"])) == 0
    stderr = capsys.readouterr().err
    # Claude stays overridden through planning and implementation, then audits.
    assert "] ASSIGN      Claude assigned task" in stderr.split("] AUDIT")[-1]
    assert len(events(stderr, "WARNING")) == 1
