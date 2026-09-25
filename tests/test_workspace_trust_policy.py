"""Workspace trust policy: strict, prepare, and bypass, resolved once and applied at invocation.

Vendor trust, HowlPlane authorization, and a bypassed check are separate facts.
These tests pin that a bypass never claims vendor trust, only audited vendor
flags are used, stdin stays closed, and no vendor trust store is written.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane import agent_readiness, cli, orchestration, workspace_trust as trust
from howlplane.control_plane.agent_execution import AgentBackendRegistry
from howlplane.control_plane.config_loader import ConfigLoader
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec
from tests.test_orchestration import arguments, repository, result

pytestmark = pytest.mark.contract

AGENTS = ("codex", "claude_code", "agy", "cursor", "devin_cli")
NOT_ENFORCED = ("codex", "claude_code", "agy")
DEVIN_FLAG = ["--respect-workspace-trust", "false"]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Empty vendor stores: no directory is trusted by any vendor."""
    cursor_data = tmp_path / "vendor-cursor"
    devin_file = tmp_path / "vendor-devin" / "trusted_workspaces.json"
    devin_file.parent.mkdir(parents=True)
    devin_file.write_text(json.dumps({"trusted_paths": [str(tmp_path / "somewhere-else")]}))
    monkeypatch.setenv("CURSOR_DATA_DIR", str(cursor_data))
    monkeypatch.setenv("HOWLPLANE_DEVIN_TRUST_FILE", str(devin_file))
    return {"cursor": cursor_data, "devin": devin_file}


def use_policy(monkeypatch, policy):
    monkeypatch.setenv(trust.POLICY_ENV, policy)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- resolution
def test_built_in_default_is_prepare(monkeypatch):
    monkeypatch.delenv(trust.POLICY_ENV)
    assert trust.resolve_policy() == {"policy": trust.PREPARE, "source": "default"}


@pytest.mark.parametrize("configured", trust.POLICIES)
def test_configured_policy_is_used(configured, monkeypatch):
    monkeypatch.delenv(trust.POLICY_ENV)
    monkeypatch.setattr(trust, "_configured_policy", lambda loader=None: configured)
    assert trust.resolve_policy() == {"policy": configured, "source": "config"}


def test_precedence_is_cli_then_environment_then_config(monkeypatch):
    monkeypatch.setattr(trust, "_configured_policy", lambda loader=None: trust.STRICT)
    monkeypatch.setenv(trust.POLICY_ENV, trust.PREPARE)
    assert trust.resolve_policy() == {"policy": trust.PREPARE, "source": "environment"}
    assert trust.resolve_policy("bypass") == {"policy": trust.BYPASS, "source": "cli"}
    trust.set_cli_policy("bypass")
    assert trust.resolve_policy() == {"policy": trust.BYPASS, "source": "cli"}
    # Processes HowlPlane launches inherit the explicit choice.
    import os
    assert os.environ[trust.POLICY_ENV] == trust.BYPASS
    assert trust.cli_policy() == trust.BYPASS


@pytest.mark.parametrize("bad", ["yes", "", "trust", "BYPASS-ALL"])
def test_invalid_policy_is_rejected(bad, monkeypatch):
    with pytest.raises(ValueError):
        trust.validate_policy(bad, "test")
    if bad:
        monkeypatch.setenv(trust.POLICY_ENV, bad)
        with pytest.raises(ValueError):
            trust.resolve_policy()
    with pytest.raises(ValueError):
        trust.set_cli_policy(bad)


def test_cli_option_rejects_unknown_policy():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["agents", "doctor", "--workspace-trust", "yes"])
    for argv in (["agents", "doctor"], ["factory", "doctor"], ["factory", "run"], ["factory", "start"],
                 ["factory", "prepare"], ["factory", "canary"], ["factory", "run-once"], ["orchestrate", "goal"]):
        parsed = cli.build_parser().parse_args([*argv, "--workspace-trust", "strict"])
        assert parsed.workspace_trust == "strict"


def test_config_file_section_is_canonical(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[ai_resources]\noperating_mode = "connected"\n\n[workspace_trust]\npolicy = "Bypass"\n')
    assert trust._configured_policy(ConfigLoader(config_path=str(tmp_path / "none.yaml"),
                                                 local_config_path=str(config))) == trust.BYPASS
    unset = tmp_path / "unset.toml"
    unset.write_text('[ai_resources]\noperating_mode = "connected"\n')
    assert trust._configured_policy(ConfigLoader(config_path=str(tmp_path / "none.yaml"),
                                                 local_config_path=str(unset))) is None
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[workspace_trust]\npolicy = "always"\n')
    with pytest.raises(Exception):
        ConfigLoader(config_path=str(tmp_path / "none.yaml"), local_config_path=str(invalid))


def test_explicit_policy_travels_to_the_factory_user_service(monkeypatch):
    from howlplane.control_plane.factory import service

    class Campaign:
        state_dir = Path("/s")
        target_dir = Path("/t")

    assert "--workspace-trust" not in service._command(Campaign(), None, None)
    trust.set_cli_policy("strict")
    command = service._command(Campaign(), None, None)
    assert command[command.index("--workspace-trust") + 1] == "strict"


# ---------------------------------------------------------------- effective readiness
@pytest.mark.parametrize("agent", NOT_ENFORCED)
@pytest.mark.parametrize("policy", trust.POLICIES)
def test_agents_without_trust_enforcement_need_no_mechanism(agent, policy):
    verdict = trust.effective(agent, trust.READY, policy)
    assert verdict["vendor_state"] == trust.NOT_ENFORCED and verdict["effective_state"] == trust.READY
    assert verdict["mechanism"] is None and trust.invocation_argv(agent, policy) == []


@pytest.mark.parametrize("agent,flag", [("cursor", "--trust"), ("devin_cli", "--respect-workspace-trust false")])
def test_untrusted_workspace_under_each_policy(agent, flag):
    strict = trust.effective(agent, trust.TRUST_REQUIRED, trust.STRICT)
    prepare = trust.effective(agent, trust.TRUST_REQUIRED, trust.PREPARE)
    bypass = trust.effective(agent, trust.TRUST_REQUIRED, trust.BYPASS)
    assert strict["effective_state"] == prepare["effective_state"] == trust.TRUST_REQUIRED
    assert strict["mechanism"] is None and prepare["mechanism"] is None
    assert bypass["effective_state"] == trust.READY and bypass["mechanism"] == flag
    # A bypass never rewrites the vendor's own verdict.
    assert bypass["vendor_state"] == trust.TRUST_REQUIRED


def test_devin_prepare_reports_operator_preparation():
    assert "operator preparation required" in trust.effective("devin_cli", trust.TRUST_REQUIRED, trust.PREPARE)["detail"]


def test_unknown_cli_is_never_bypassed():
    assert trust.invocation_argv("future_cli", trust.BYPASS) == []
    assert trust.effective("future_cli", trust.TRUST_REQUIRED, trust.BYPASS)["effective_state"] == trust.UNSUPPORTED
    # A new CLI's refusal still fails closed through its own adapter only.
    assert not trust.is_trust_refusal("future_cli", "Workspace Trust Required")


# ---------------------------------------------------------------- invocation argv
def captured_argv(agent, tmp_path, monkeypatch, **kwargs):
    from howlplane.control_plane import agent_execution
    seen = {}

    def spy(*args, **options):
        seen.update(options)
        return subprocess.CompletedProcess(options["args"], 0, "ok", "")

    monkeypatch.setattr(agent_execution.subprocess, "run", spy)
    backend = AgentBackendRegistry.get_backend(agent)
    monkeypatch.setattr(backend, "is_available", lambda: True)
    outcome = backend.execute(TaskSpec(task_id="t", repository=str(tmp_path), objective="o"), tmp_path,
                              role="review", prompt_override="x", timeout_seconds=5, **kwargs)
    assert seen["stdin"] is agent_execution.subprocess.DEVNULL
    return seen["args"], outcome


@pytest.mark.parametrize("agent", NOT_ENFORCED)
def test_bypass_adds_nothing_where_trust_is_not_enforced(agent, tmp_path, monkeypatch):
    prepared, _ = captured_argv(agent, tmp_path, monkeypatch, workspace_trust_policy=trust.PREPARE)
    bypassed, outcome = captured_argv(agent, tmp_path, monkeypatch, workspace_trust_policy=trust.BYPASS)
    assert prepared == bypassed
    assert outcome.metadata["workspace_trust"] == {"policy": trust.BYPASS, "mechanism": None}


@pytest.mark.parametrize("policy", [trust.STRICT, trust.PREPARE])
def test_cursor_and_devin_keep_vendor_trust_outside_bypass(policy, tmp_path, monkeypatch):
    cursor, _ = captured_argv("cursor", tmp_path, monkeypatch, workspace_trust_policy=policy)
    devin, _ = captured_argv("devin_cli", tmp_path, monkeypatch, workspace_trust_policy=policy)
    assert "--trust" not in cursor and "--respect-workspace-trust" not in devin


def test_bypass_uses_only_the_audited_flags_as_discrete_arguments(tmp_path, monkeypatch):
    cursor, _ = captured_argv("cursor", tmp_path, monkeypatch, workspace_trust_policy=trust.BYPASS)
    devin, outcome = captured_argv("devin_cli", tmp_path, monkeypatch, workspace_trust_policy=trust.BYPASS)
    assert cursor.count("--trust") == 1 and not {"--yolo", "--force", "-f"} & set(cursor)
    assert devin[-2:] == DEVIN_FLAG and devin[:2] == ["devin", "-p"]
    # Command permissions are unchanged: the read-only role stays in its read-only mode.
    assert devin[devin.index("--permission-mode") + 1] == "auto"
    assert outcome.metadata["workspace_trust"] == {"policy": trust.BYPASS,
                                                   "mechanism": "--respect-workspace-trust false"}


def test_backend_resolves_the_canonical_policy_without_a_caller_argument(tmp_path, monkeypatch):
    use_policy(monkeypatch, trust.BYPASS)
    devin, _ = captured_argv("devin_cli", tmp_path, monkeypatch)
    assert devin[-2:] == DEVIN_FLAG


def fake_vendor(tmp_path, monkeypatch, name, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


# Behaves like Devin 3000.11.3: refuses an untrusted directory in print mode
# unless --respect-workspace-trust false is passed, and records its argv and
# whether anything could be read from stdin.
FAKE_DEVIN = r'''
printf '%s\n' "$@" > "$FAKE_ARGV"
if read -r line; then echo "stdin-open" > "$FAKE_STDIN"; else echo "stdin-eof" > "$FAKE_STDIN"; fi
skip=0; prev=""
for a in "$@"; do [ "$prev" = "--respect-workspace-trust" ] && [ "$a" = "false" ] && skip=1; prev="$a"; done
if [ $skip = 0 ]; then echo "Error: Refusing to run in an untrusted workspace: $PWD" >&2; exit 1; fi
echo "HOWLPLANE_READ_ONLY_OK"
'''


def test_devin_runs_in_a_fresh_untrusted_workspace_under_bypass_without_trusting_it(stores, tmp_path, monkeypatch):
    workspace = tmp_path / "fresh-worktree"
    workspace.mkdir()
    fake_vendor(tmp_path, monkeypatch, "devin", FAKE_DEVIN)
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv"))
    monkeypatch.setenv("FAKE_STDIN", str(tmp_path / "stdin"))
    before = digest(stores["devin"])
    assert trust.check("devin_cli", workspace)["state"] == trust.TRUST_REQUIRED
    backend = AgentBackendRegistry.get_backend("devin_cli")
    task = TaskSpec(task_id="t", repository=str(workspace), objective="o")

    strict = backend.execute(task, workspace, role="review", prompt_override="read only", timeout_seconds=20,
                             workspace_trust_policy=trust.STRICT)
    assert not strict.success
    assert ProviderPoolManager.classify_result("devin_cli", strict).value == "WORKSPACE_TRUST_REQUIRED"

    bypass = backend.execute(task, workspace, role="review", prompt_override="read only", timeout_seconds=20,
                             workspace_trust_policy=trust.BYPASS)
    assert bypass.success and "HOWLPLANE_READ_ONLY_OK" in bypass.stdout
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[-2:] == DEVIN_FLAG
    assert (tmp_path / "stdin").read_text().strip() == "stdin-eof"
    # Bypassing the check trusted nothing: the vendor store is byte-identical.
    assert digest(stores["devin"]) == before
    status = agent_readiness.workspace_status("devin_cli", workspace, policy=trust.BYPASS)
    assert status["vendor_state"] == trust.TRUST_REQUIRED and status["state"] == trust.TRUST_REQUIRED
    assert status["effective_state"] == trust.READY and status["vendor_state"] != trust.READY
    assert status["mechanism"] == "--respect-workspace-trust false"


FAKE_CURSOR = r'''
ws=""; flag=0; prev=""
for a in "$@"; do [ "$prev" = "--workspace" ] && ws="$a"; [ "$a" = "--trust" ] && flag=1; prev="$a"; done
slug=$(printf '%s' "$ws" | sed -e 's/[^a-zA-Z0-9]/-/g' -e 's/--*/-/g' -e 's/^-//' -e 's/-$//')
dir="$CURSOR_DATA_DIR/projects/$slug"
if [ $flag = 1 ]; then mkdir -p "$dir"; echo '{}' > "$dir/.workspace-trusted"; fi
if [ ! -f "$dir/.workspace-trusted" ]; then echo "Workspace Trust Required"; exit 1; fi
echo "HOWLPLANE_READ_ONLY_OK"
'''


def test_cursor_runs_in_a_fresh_workspace_under_bypass_with_its_own_flag(stores, tmp_path, monkeypatch):
    workspace = tmp_path / "fresh-worktree"
    workspace.mkdir()
    fake_vendor(tmp_path, monkeypatch, "agent", FAKE_CURSOR)
    backend = AgentBackendRegistry.get_backend("cursor")
    task = TaskSpec(task_id="t", repository=str(workspace), objective="o")
    strict = backend.execute(task, workspace, role="review", prompt_override="x", timeout_seconds=20,
                             workspace_trust_policy=trust.STRICT)
    assert ProviderPoolManager.classify_result("cursor", strict).value == "WORKSPACE_TRUST_REQUIRED"
    assert agent_readiness.workspace_status("cursor", workspace, policy=trust.STRICT)["effective_state"] == \
        trust.TRUST_REQUIRED
    bypass = backend.execute(task, workspace, role="review", prompt_override="x", timeout_seconds=20,
                             workspace_trust_policy=trust.BYPASS)
    assert bypass.success
    # Cursor's --trust records Cursor's own trust, so its vendor state may now truthfully be READY.
    assert trust.check("cursor", workspace)["state"] == trust.READY


# ---------------------------------------------------------------- readiness, doctor, Factory
def test_refusal_binds_only_the_invocation_that_met_it(stores, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent_readiness.record_workspace_trust("devin_cli", str(workspace.resolve()), "implementation", "session",
                                           policy=trust.PREPARE)
    assert agent_readiness.workspace_blocked("devin_cli", workspace, policy=trust.PREPARE)
    assert agent_readiness.workspace_blocked("devin_cli", workspace, policy=trust.STRICT)
    assert agent_readiness.workspace_blocked("devin_cli", workspace, policy=trust.BYPASS) is None
    agent_readiness.record_workspace_trust("devin_cli", str(workspace.resolve()), "implementation", "session",
                                           policy=trust.BYPASS)
    # Refused even with the flag: nothing makes that folder usable.
    assert agent_readiness.workspace_blocked("devin_cli", workspace, policy=trust.BYPASS)


def fresh_worktree(tmp_path):
    repo = repository(tmp_path)
    target = tmp_path / "factory-root" / "canaries" / "c1" / "target"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "c1", str(target)], check=True)
    return target


def test_fresh_factory_worktree_is_ready_for_every_agent_under_bypass(stores, tmp_path):
    target = fresh_worktree(tmp_path)
    report = agent_readiness.workspace_report(target, policy=trust.BYPASS)
    assert report["workspace_trust_policy"] == {"policy": trust.BYPASS, "source": "cli"}
    assert report["authorized"] is False  # no authorization record is needed to invoke a CLI
    assert {agent: status["effective_state"] for agent, status in report["agents"].items()} == \
        {agent: trust.READY for agent in AGENTS}
    assert report["agents"]["devin_cli"]["vendor_state"] == trust.TRUST_REQUIRED
    assert report["agents"]["cursor"]["vendor_state"] == trust.TRUST_REQUIRED
    strict = agent_readiness.workspace_report(target, policy=trust.STRICT)
    assert {agent for agent, status in strict["agents"].items()
            if status["effective_state"] == trust.TRUST_REQUIRED} == {"cursor", "devin_cli"}


def _summary(agent):
    return {"agent": agent, "name": agent_readiness.SPECS[agent].name, "installed": True, "authenticated": True,
            "unattended_execution": True, "mutation_capable": True,
            "capacity": {"state": "UNKNOWN", "limits": []}, "live_smoke": {"status": "PASS", "last_status": "PASS"}}


def test_factory_is_not_degraded_by_vendor_trust_alone_under_bypass(stores, tmp_path):
    target = fresh_worktree(tmp_path)
    summaries = [_summary(agent) for agent in agent_readiness.AGENT_ORDER]
    bypass = agent_readiness.factory_readiness(summaries, agent_readiness.workspace_report(target, policy=trust.BYPASS))
    assert bypass["status"] == "READY" and bypass["workspace_trust_required"] == []
    assert bypass["workspace_trust_bypassed"] == ["Cursor", "Devin"]
    assert bypass["workspace_trust_policy"] == trust.BYPASS
    strict = agent_readiness.factory_readiness(summaries, agent_readiness.workspace_report(target, policy=trust.STRICT))
    assert strict["status"] == "DEGRADED" and strict["workspace_trust_required"] == ["Cursor", "Devin"]
    text = agent_readiness.render_factory(bypass, {"implementation": 1})
    assert "Trust policy:           BYPASS" in text and "Trust check bypassed:   Cursor, Devin" in text


def test_agents_doctor_reports_vendor_policy_and_effective_state(stores, tmp_path, monkeypatch, capsys):
    from tests.test_agent_readiness import install
    install(tmp_path, monkeypatch)
    target = fresh_worktree(tmp_path)
    assert cli.main(["agents", "doctor", "--repo", str(target), "--workspace-trust", "bypass", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    devin = document["workspace"]["agents"]["devin_cli"]
    assert (devin["vendor_state"], devin["policy"], devin["effective_state"], devin["mechanism"]) == (
        "TRUST_REQUIRED", "bypass", "READY", "--respect-workspace-trust false")
    assert document["workspace"]["workspace_trust_policy"] == {"policy": "bypass", "source": "cli"}
    trust.reset_cli_policy()
    assert cli.main(["agents", "doctor", "--repo", str(target), "--workspace-trust", "bypass"]) == 0
    out = capsys.readouterr().out
    devin_text = out[out.index("WORKSPACE READINESS"):]
    devin_text = devin_text[devin_text.index("  Devin\n"):]
    assert "Vendor trust:        TRUST REQUIRED" in devin_text and "Trust policy:        BYPASS" in devin_text
    assert "Effective workspace: READY" in devin_text and "Mechanism:           --respect-workspace-trust false" in devin_text
    trust.reset_cli_policy()
    assert cli.main(["agents", "doctor", "--repo", str(target), "--workspace-trust", "strict", "--json"]) == 0
    strict = json.loads(capsys.readouterr().out)["workspace"]["agents"]
    assert strict["devin_cli"]["effective_state"] == strict["cursor"]["effective_state"] == "TRUST_REQUIRED"


def test_prepare_under_bypass_authorizes_without_touching_vendors(stores, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane.factory import prepare
    from tests.test_workspace_trust import _prepare_args, _prepare_env
    repo = _prepare_env(tmp_path, monkeypatch, stores)
    before = digest(stores["devin"])
    use_policy(monkeypatch, trust.BYPASS)
    monkeypatch.setattr(trust, "_run", lambda *a, **k: pytest.fail("no vendor CLI runs under bypass"))
    assert prepare.command(_prepare_args(repo, yes=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["preparation"] == {}
    assert trust.authorized_scope_for(repo) is not None
    assert not (stores["cursor"] / "projects").exists() and digest(stores["devin"]) == before
    assert report["readiness"]["workspace_trust_required"] == []


# ---------------------------------------------------------------- routing
def _readied_pool():
    from howlplane.control_plane.resource_models import ReadinessStatus
    pool = ProviderPoolManager(probe_on_start=False)
    for state in pool._provider_states.values():
        state.readiness = ReadinessStatus.READY
    return pool


@pytest.mark.parametrize("agent", ["cursor", "devin_cli"])
def test_pool_eligibility_follows_policy_without_touching_capacity(agent, stores, tmp_path, monkeypatch):
    pool = _readied_pool()
    workspace = tmp_path / "fresh"
    workspace.mkdir()
    task = TaskSpec(task_id="t", repository=str(workspace), objective="o")
    before = pool.get_all_statuses()
    use_policy(monkeypatch, trust.STRICT)
    blocked = pool.select_resource(task, role="implementation", explicit_resource_id=agent)
    assert blocked.selected is None
    assert any(item.resource_id == agent and item.reason == "WORKSPACE_TRUST_REQUIRED" for item in blocked.exclusions)
    use_policy(monkeypatch, trust.BYPASS)
    chosen = pool.select_resource(task, role="implementation", explicit_resource_id=agent)
    assert chosen.selected is not None and chosen.selected.resource_id == agent
    assert pool.get_all_statuses() == before


def _session(tmp_path, policy):
    doc = {"agents": orchestration.inventory({}, tmp_path, policy), "repository": str(tmp_path),
           "orchestrator": "AUTO", "strategy": "BALANCED", "role_models": {}, "fallbacks": {}, "model_states": {},
           "known_models": {}, "timed_out_assignments": [], "goal": "g", "constraints": [],
           "execution_budget": orchestration.default_execution_budget(),
           "workspace_trust_policy": {"policy": policy, "source": "cli"}}
    return doc


def test_orchestration_candidates_follow_policy(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestration.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(orchestration, "discover_models", lambda agent: [])
    strict = {agent for agent, _ in orchestration.candidates(_session(tmp_path, trust.STRICT), "implementation")}
    bypass_doc = _session(tmp_path, trust.BYPASS)
    bypass = {agent for agent, _ in orchestration.candidates(bypass_doc, "implementation")}
    assert "cursor" not in strict and "devin_cli" not in strict
    assert {"cursor", "devin_cli"} <= bypass
    record = bypass_doc["agents"]["devin_cli"]["workspace_trust"]
    assert record["state"] == trust.TRUST_REQUIRED and record["effective_state"] == trust.READY
    # A trust refusal met with the flag stays binding; other failures keep their own class.
    orchestration.record_failure(bypass_doc, "devin_cli", "implementation", "UNKNOWN", "WORKSPACE_TRUST_REQUIRED")
    assert "workspace trust required" in orchestration.capability_skip_reason(bypass_doc, "devin_cli", "review")
    orchestration.record_failure(bypass_doc, "cursor", "implementation", "UNKNOWN", "SESSION_LIMIT")
    assert bypass_doc["agents"]["cursor"]["capacity"]["implementation"]["reason"] == "SESSION_LIMIT"


def test_orchestrate_session_under_bypass_dispatches_devin_and_records_no_trust_failure(stores, tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(orchestration.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(orchestration, "discover_models", lambda agent: [])
    with pytest.raises(ValueError, match="workspace trust policy strict"):
        trust.set_cli_policy("strict")
        orchestration.setup(arguments(repo, orchestrator="devin_cli"), repo)
    trust.set_cli_policy("bypass")
    seen = []

    def execute(doc, role, agent, model, cwd):
        seen.append((agent, orchestration.session_trust_policy(doc)))
        return result(agent, role)

    monkeypatch.setattr(orchestration, "execute_assignment", execute)
    assert orchestration.command(arguments(repo, orchestrator="devin_cli")) == 0
    assert seen == [("devin_cli", trust.BYPASS)]
    session = orchestration.active_sessions(orchestration.state_root(), repo, include_terminal=True)[0]
    assert session["workspace_trust_policy"]["policy"] == trust.BYPASS
    assert session["attempts"][0]["workspace_trust"] == {"policy": "bypass",
                                                         "mechanism": "--respect-workspace-trust false"}
    assert not (agent_readiness.load_cache().get("devin_cli") or {}).get("workspaces")


def test_execute_assignment_hands_the_session_policy_to_the_backend(tmp_path, monkeypatch):
    seen = {}

    class Backend:
        def execute(self, task, cwd, **kwargs):
            seen.update(kwargs)
            return result("devin_cli", "review")

    monkeypatch.setattr(AgentBackendRegistry, "get_backend", classmethod(lambda cls, agent, custom=None: Backend()))
    doc = {"id": "abcdef012345", "goal": "g", "constraints": [], "attempts": [],
           "execution_budget": orchestration.default_execution_budget(),
           "workspace_trust_policy": {"policy": trust.BYPASS, "source": "config"}}
    orchestration.execute_assignment(doc, "review", "devin_cli", "UNKNOWN", tmp_path)
    assert seen["workspace_trust_policy"] == trust.BYPASS


def test_unused_argparse_namespace_has_no_policy_side_effect():
    trust.set_cli_policy(getattr(argparse.Namespace(), "workspace_trust", None))
    assert trust.cli_policy() is None


@pytest.mark.parametrize("stderr", ["Error: 429 rate limit exceeded", "authentication required: please log in",
                                    "Unknown model: x"])
def test_bypassed_invocations_keep_their_real_failure_class(stderr):
    from tests.test_workspace_trust import failed
    plain = failed(stderr=stderr)
    bypassed = failed(stderr=stderr, workspace_trust={"policy": "bypass", "mechanism": "--respect-workspace-trust false"})
    assert ProviderPoolManager.classify_result("devin_cli", bypassed) == \
        ProviderPoolManager.classify_result("devin_cli", plain)
    assert ProviderPoolManager.classify_result("devin_cli", bypassed).value != "WORKSPACE_TRUST_REQUIRED"


def test_example_config_shows_the_canonical_bypass_setting():
    example = Path(__file__).resolve().parents[1] / "config" / "provider_resources.example.toml"
    loader = ConfigLoader(config_path=str(example.parent / "absent.yaml"), local_config_path=str(example))
    assert trust._configured_policy(loader) == trust.BYPASS


def test_cli_fails_fast_on_an_invalid_environment_policy(monkeypatch, capsys):
    monkeypatch.setenv(trust.POLICY_ENV, "always")
    assert cli.main(["agents", "doctor", "--agent", "codex"]) == 1
    assert "Invalid workspace trust policy" in capsys.readouterr().err
