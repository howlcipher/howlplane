"""Workspace trust: per-agent vendor trust, HowlPlane authorization, preparation, and routing.

The vendor store formats and refusal texts below are the ones observed on the
installed CLIs (Cursor 2026.09.23, Devin 3000.11.3); Codex, Claude Code, and AGY
were observed to run in an untrusted directory without any prompt.
"""

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from howlplane.control_plane import agent_readiness, orchestration, workspace_trust as trust
from howlplane.control_plane.agent_execution import AgentExecutionResult
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from tests._factory_test_helpers import make_git_repo, set_xdg_paths

pytestmark = pytest.mark.contract

CURSOR_REFUSAL = ("\n⚠ Workspace Trust Required\n\n  Cursor Agent can execute code and access files in this directory.\n"
                  "  Do you trust the contents of this directory?\n\n  To proceed, you can either:\n"
                  "    • Pass --trust, --yolo, or -f if you trust this directory\n")
DEVIN_REFUSAL = ("Error: Refusing to run in an untrusted workspace: /w\nStart `devin` interactively in this directory "
                 "to trust it, or set `respect_workspace_trust: false` in your config to restore the previous behavior.\n")
REFUSALS = {"cursor": CURSOR_REFUSAL, "devin_cli": DEVIN_REFUSAL}
ENFORCING = ("cursor", "devin_cli")
NOT_ENFORCED = ("codex", "claude_code", "agy")


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Empty vendor stores: nothing is trusted until a test says so."""
    cursor_data = tmp_path / "vendor-cursor"
    devin_file = tmp_path / "vendor-devin" / "trusted_workspaces.json"
    monkeypatch.setenv("CURSOR_DATA_DIR", str(cursor_data))
    monkeypatch.setenv("HOWLPLANE_DEVIN_TRUST_FILE", str(devin_file))
    return {"cursor": cursor_data, "devin": devin_file}


def cursor_trust(stores, path):
    marker = stores["cursor"] / "projects" / trust.cursor_slug(str(path))
    marker.mkdir(parents=True, exist_ok=True)
    (marker / ".workspace-trusted").write_text(json.dumps({"workspacePath": str(path), "trustMethod": "cli-flag"}))


def devin_trust(stores, *paths):
    stores["devin"].parent.mkdir(parents=True, exist_ok=True)
    stores["devin"].write_text(json.dumps({"trusted_paths": [str(p) for p in paths]}))


def trust_both(stores, path):
    cursor_trust(stores, path)
    devin_trust(stores, path)


def failed(stdout="", stderr="", **metadata):
    return AgentExecutionResult(agent_id="x", role="implementation", command="x", exit_code=1, stdout=stdout,
                                stderr=stderr, duration_seconds=0.1, success=False, metadata=metadata or None)


# ---------------------------------------------------------------- vendor trust (read-only)
@pytest.mark.parametrize("agent", NOT_ENFORCED)
def test_noninteractive_modes_without_trust_prompts_are_ready_anywhere(agent, stores, tmp_path):
    result = trust.check(agent, tmp_path / "never-seen")
    assert result["state"] == trust.READY and result["scope"] == "not_enforced"
    assert result["source"] == "observed CLI behavior"


@pytest.mark.parametrize("agent", ENFORCING)
def test_fresh_directory_requires_trust(agent, stores, tmp_path):
    assert trust.check(agent, tmp_path)["state"] == trust.TRUST_REQUIRED


@pytest.mark.parametrize("agent", ENFORCING)
def test_trusted_root_covers_fresh_child_worktrees_but_not_siblings(agent, stores, tmp_path):
    root = tmp_path / "factory-root"
    (root / "target").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    trust_both(stores, root)
    inherited = trust.check(agent, root / "target")
    assert inherited["state"] == trust.READY and inherited["trusted_by"] == str(root.resolve())
    # A worktree created after preparation (not on disk yet) inherits too.
    assert trust.check(agent, root / "canaries" / "c1" / "target")["state"] == trust.READY
    assert trust.check(agent, tmp_path / "elsewhere")["state"] == trust.TRUST_REQUIRED


def test_cursor_never_inherits_from_home_or_very_short_paths(stores, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "home" / "projects" / "repo"
    workspace.mkdir(parents=True)
    cursor_trust(stores, tmp_path / "home")
    assert trust.check("cursor", workspace)["state"] == trust.TRUST_REQUIRED
    assert not trust._cursor_inheritable(Path("/srv"))
    assert not trust._cursor_inheritable(Path("/srv/x"))
    assert trust._cursor_inheritable(Path("/srv/x/y"))
    cursor_trust(stores, workspace)
    assert trust.check("cursor", workspace)["state"] == trust.READY


def test_cursor_slug_matches_the_cli():
    # Observed directory names under ~/.cursor/projects.
    assert trust.cursor_slug("/run/media/system/tallgeese/dev/howlplane") == "run-media-system-tallgeese-dev-howlplane"
    assert trust.cursor_slug("/srv/howl trust.probe/Root") == "srv-howl-trust-probe-Root"


def test_devin_matches_canonical_paths_only(stores, tmp_path):
    real = tmp_path / "real"
    (real / "child").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    devin_trust(stores, link)
    assert trust.check("devin_cli", real / "child")["state"] == trust.READY
    devin_trust(stores, tmp_path / "rea")  # a string prefix, not an ancestor
    assert trust.check("devin_cli", real)["state"] == trust.TRUST_REQUIRED


def test_devin_store_follows_xdg_data_home_like_devin(tmp_path, monkeypatch):
    monkeypatch.delenv("HOWLPLANE_DEVIN_TRUST_FILE")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert trust.devin_trust_file() == tmp_path / "data" / "devin" / "cli" / "trusted_workspaces.json"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert trust.devin_trust_file() == tmp_path / "home" / ".local" / "share" / "devin" / "cli" / "trusted_workspaces.json"


def test_unreadable_devin_store_is_an_error_never_ready(stores, tmp_path):
    stores["devin"].parent.mkdir(parents=True)
    stores["devin"].write_text("{not json")
    assert trust.check("devin_cli", tmp_path)["state"] == trust.ERROR


def test_unknown_agent_is_unsupported():
    assert trust.check("gemini_cli", "/")["state"] == trust.UNSUPPORTED


def test_deleted_and_recreated_slot_keeps_inherited_trust_but_exact_trust_is_stale(stores, tmp_path):
    root = tmp_path / "root"
    slot = root / "slot-01"
    slot.mkdir(parents=True)
    cursor_trust(stores, slot)
    assert trust.check("cursor", slot)["trusted_by"] == str(slot.resolve())
    slot.rmdir()
    # Exact-path trust names a directory that no longer exists: nothing else inherits it.
    assert trust.check("cursor", tmp_path / "root" / "slot-02")["state"] == trust.TRUST_REQUIRED
    cursor_trust(stores, root)
    slot.mkdir()
    assert trust.check("cursor", slot)["state"] == trust.READY


# ---------------------------------------------------------------- refusal detection
@pytest.mark.parametrize("agent", ENFORCING)
def test_refusal_is_recognized_only_for_its_own_cli(agent):
    assert trust.is_trust_refusal(agent, REFUSALS[agent])
    other = "devin_cli" if agent == "cursor" else "cursor"
    assert not trust.is_trust_refusal(other, REFUSALS[agent])
    for agent_without_prompt in NOT_ENFORCED + ("unknown",):
        assert not trust.is_trust_refusal(agent_without_prompt, REFUSALS[agent])


@pytest.mark.parametrize("agent", ENFORCING)
def test_trust_prose_deep_in_a_transcript_is_not_a_refusal(agent):
    transcript = "Edited workspace_trust.py\n" * 200 + REFUSALS[agent]
    assert not trust.is_trust_refusal(agent, transcript)


@pytest.mark.parametrize("agent", ENFORCING)
def test_trust_refusal_is_its_own_failure_class(agent):
    assert ProviderPoolManager.classify_result(agent, failed(stderr=REFUSALS[agent])).value == "WORKSPACE_TRUST_REQUIRED"
    assert ProviderPoolManager.classify_result(agent, failed(stdout=REFUSALS[agent])).value == "WORKSPACE_TRUST_REQUIRED"


@pytest.mark.parametrize("agent", ENFORCING)
def test_session_stuck_at_a_trust_prompt_until_the_budget_is_trust_not_budget(agent):
    result = failed(stdout=REFUSALS[agent], timeout_source="harness")
    assert ProviderPoolManager.classify_result(agent, result).value == "WORKSPACE_TRUST_REQUIRED"


@pytest.mark.parametrize("agent", ENFORCING + NOT_ENFORCED)
def test_budget_timeout_semantics_are_unchanged(agent):
    result = failed(stdout="working...", timeout_source="harness")
    assert ProviderPoolManager.classify_result(agent, result).value == "EXECUTION_BUDGET_EXCEEDED"


@pytest.mark.parametrize("agent", ENFORCING)
def test_quota_and_rate_limits_are_not_mistaken_for_trust(agent):
    assert ProviderPoolManager.classify_result(agent, failed(stderr="Error: quota exhausted")).value == "QUOTA_EXHAUSTED"
    assert ProviderPoolManager.classify_result(agent, failed(stderr="HTTP 429 Too Many Requests")).value == "RATE_LIMITED"


# ---------------------------------------------------------------- CLI probe (sentinel model, no inference)
@pytest.mark.parametrize("agent,accepted", [("cursor", "Cannot use this model: howlplane-trust-probe-invalid-model"),
                                            ("devin_cli", "Error: Unknown model: 'howlplane-trust-probe-invalid-model'")])
def test_probe_reads_the_cli_verdict_without_inference(agent, accepted, tmp_path):
    seen = []

    def runner(argv, cwd):
        seen.append(argv)
        return 1, runner.output

    runner.output = REFUSALS[agent]
    assert trust.probe(agent, tmp_path, runner)["state"] == trust.TRUST_REQUIRED
    runner.output = accepted
    assert trust.probe(agent, tmp_path, runner)["state"] == trust.READY
    runner.output = "something else entirely"
    assert trust.probe(agent, tmp_path, runner)["state"] == trust.UNKNOWN
    assert all(trust.SENTINEL_MODEL in argv and "--trust" not in argv for argv in seen)


# ---------------------------------------------------------------- HowlPlane authorization
def _authorize(tmp_path):
    repo = make_git_repo(tmp_path)
    root = tmp_path / "factory-root"
    entry = trust.authorize(repo, (repo / ".git").resolve(), root, [repo, root], ["cursor", "devin_cli"], "test")
    return repo, root, entry


def test_authorization_record_is_private_and_secret_free(tmp_path):
    repo, root, entry = _authorize(tmp_path)
    path = trust.registry_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert set(entry) == {"repo_root", "common_git_dir", "factory_root", "workspaces", "authorized_at",
                          "operator_intent", "strategy", "agents", "agents_prepared", "last_verified_at"}
    text = path.read_text()
    assert not any(word in text.lower() for word in ("token", "password", "api_key", "secret", "@"))


def test_only_the_authorized_scope_is_covered(tmp_path):
    repo, root, _ = _authorize(tmp_path)
    assert trust.authorized_scope_for(repo) is not None
    assert trust.authorized_scope_for(root) is not None
    assert trust.authorized_scope_for(root / "not-created-yet" / "target") is not None
    (tmp_path / "arbitrary").mkdir()
    assert trust.authorized_scope_for(tmp_path / "arbitrary") is None
    assert trust.authorized_scope_for(repo / "subdir-not-listed") is None
    # A different repository placed inside the Factory root is not this repository's worktree.
    intruder = make_git_repo(root, name="intruder")
    assert trust.authorized_scope_for(intruder) is None
    # A genuine worktree of the authorized repository is.
    import subprocess
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(root / "slot")], check=True)
    assert trust.authorized_scope_for(root / "slot") is not None


def test_revoke_removes_only_howlplane_authorization(stores, tmp_path):
    repo, root, _ = _authorize(tmp_path)
    cursor_trust(stores, root)
    assert trust.revoke(repo) is not None
    assert trust.authorized_scope_for(repo) is None
    assert trust.check("cursor", root)["state"] == trust.READY, "vendor trust is not HowlPlane's to change"


# ---------------------------------------------------------------- preparation
def test_unauthorized_directory_is_never_prepared(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "_run", lambda *a, **k: pytest.fail("no CLI may be invoked for an unauthorized path"))
    monkeypatch.setattr(trust.subprocess, "Popen", lambda *a, **k: pytest.fail("no prompt may be opened"))
    (tmp_path / "arbitrary").mkdir()
    for agent in ENFORCING:
        with pytest.raises(trust.PreparationRefused):
            trust.prepare(agent, tmp_path / "arbitrary")
    assert trust.check("cursor", tmp_path / "arbitrary")["state"] == trust.TRUST_REQUIRED


def test_cursor_preparation_uses_the_documented_flag_for_the_authorized_path_only(stores, tmp_path, monkeypatch):
    _, root, _ = _authorize(tmp_path)
    calls = []

    def fake_cursor(argv, cwd):
        calls.append(argv)
        if "--trust" in argv:
            cursor_trust(stores, Path(argv[argv.index("--workspace") + 1]))
        return 1, "Cannot use this model: " + trust.SENTINEL_MODEL

    monkeypatch.setattr(trust, "_run", fake_cursor)
    result = trust.prepare("cursor", root)
    assert result["state"] == trust.READY
    assert calls == [["agent", "-p", "noop", "--trust", "--workspace", str(root.resolve()), "--model",
                      trust.SENTINEL_MODEL, "--output-format", "text"]]
    calls.clear()
    assert trust.prepare("cursor", root)["state"] == trust.READY
    assert calls == [], "already trusted: nothing to do"


@pytest.mark.parametrize("agent", NOT_ENFORCED)
def test_agents_without_trust_prompts_need_no_preparation(agent, tmp_path, monkeypatch):
    _, root, _ = _authorize(tmp_path)
    monkeypatch.setattr(trust, "_run", lambda *a, **k: pytest.fail("nothing to prepare"))
    assert trust.prepare(agent, root)["state"] == trust.READY


def test_operator_bootstrap_requires_a_terminal_and_never_spawns_without_one(stores, tmp_path):
    spawned = []
    result = trust.prepare_operator_prompt("devin_cli", tmp_path, ["devin"], popen=lambda *a, **k: spawned.append(a),
                                           is_tty=lambda: False)
    assert spawned == [] and result["state"] == trust.TRUST_REQUIRED
    assert "operator terminal required" in result["detail"]


class FakeSession:
    def __init__(self, on_poll=None):
        self.on_poll, self.polls, self.terminated = on_poll, 0, False

    def poll(self):
        self.polls += 1
        if self.on_poll and self.polls == 2:
            self.on_poll()
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def test_operator_bootstrap_sends_no_input_and_ends_once_trust_persists(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "_restore_terminal", lambda: None)
    session = FakeSession(on_poll=lambda: devin_trust(stores, tmp_path))
    launched = {}

    def popen(argv, **kwargs):
        launched.update(argv=argv, **kwargs)
        return session

    result = trust.prepare_operator_prompt("devin_cli", tmp_path, ["devin"], popen=popen, is_tty=lambda: True,
                                           poll_interval=0)
    assert result["state"] == trust.READY
    # The operator's own terminal: no pipes HowlPlane could type into.
    assert launched == {"argv": ["devin"], "cwd": str(tmp_path)}
    assert session.terminated


def test_operator_bootstrap_that_is_declined_reports_trust_required(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "_restore_terminal", lambda: None)
    result = trust.prepare_operator_prompt("devin_cli", tmp_path, ["devin"], popen=lambda *a, **k: FakeSession(),
                                           is_tty=lambda: True, timeout=0.05, poll_interval=0.01)
    assert result["state"] == trust.TRUST_REQUIRED and "not recorded" in result["detail"]


# ---------------------------------------------------------------- routing
def test_one_untrusted_workspace_does_not_disable_the_agent_elsewhere(stores, tmp_path):
    trusted, untrusted = tmp_path / "trusted", tmp_path / "untrusted"
    trusted.mkdir()
    untrusted.mkdir()
    trust_both(stores, trusted)
    agent_readiness.record_workspace_trust("cursor", str(untrusted.resolve()), "implementation", "orchestrate session")
    assert agent_readiness.workspace_blocked("cursor", untrusted)
    assert agent_readiness.workspace_blocked("cursor", trusted) is None
    assert "cursor" not in agent_readiness.cached_summaries() or \
        agent_readiness.cached_summaries()["cursor"]["unattended_execution"] is not False
    # A refusal the CLI returned outranks the store until preparation clears it.
    trust_both(stores, untrusted)
    assert agent_readiness.workspace_blocked("cursor", untrusted)
    agent_readiness.clear_workspace_refusal("cursor", str(untrusted.resolve()))
    assert agent_readiness.workspace_blocked("cursor", untrusted) is None


def test_trust_refusal_never_becomes_capability_or_capacity_evidence(tmp_path):
    agent_readiness.record_session_outcome("cursor", "m", "WORKSPACE_TRUST_REQUIRED", detail=CURSOR_REFUSAL)
    record = agent_readiness.load_cache().get("cursor", {})
    assert "unattended" not in record and not record.get("limits")


def test_provider_pool_keeps_availability_on_trust_refusal():
    pool = ProviderPoolManager(probe_on_start=False)
    before = pool.get_status("cursor")
    assert pool.record_result("cursor", failed(stderr=CURSOR_REFUSAL), role="implementation").value == \
        "WORKSPACE_TRUST_REQUIRED"
    state = pool._provider_states["cursor"]
    assert pool.get_status("cursor") == before and state.consecutive_failures == 0
    assert "implementation" not in state.role_exclusions and state.retry_after is None


def _task(repository):
    from howlplane.control_plane.task_spec import TaskSpec
    return TaskSpec(task_id="t", repository=str(repository), objective="o")


def _readied_pool(monkeypatch):
    from howlplane.control_plane.resource_models import ReadinessStatus
    pool = ProviderPoolManager(probe_on_start=False)
    for state in pool._provider_states.values():
        state.readiness = ReadinessStatus.READY
    return pool


def test_cursor_is_a_builtin_pool_resource_selectable_where_trusted(stores, tmp_path, monkeypatch):
    pool = _readied_pool(monkeypatch)
    assert "cursor" in {profile.resource_id for profile in pool.registry.list_resources()}
    assert "agent" not in {profile.resource_id for profile in pool.registry.list_resources()}
    trusted, untrusted = tmp_path / "trusted", tmp_path / "untrusted"
    trusted.mkdir()
    untrusted.mkdir()
    cursor_trust(stores, trusted)
    decision = pool.select_resource(_task(trusted), role="implementation", explicit_resource_id="cursor")
    assert decision.selected is not None and decision.selected.resource_id == "cursor"
    # UNKNOWN provider capacity stays eligible.
    assert pool.get_status("cursor").value in ("AVAILABLE", "UNKNOWN")
    blocked = pool.select_resource(_task(untrusted), role="implementation", explicit_resource_id="cursor")
    assert blocked.selected is None
    assert any(item.resource_id == "cursor" and item.reason == "WORKSPACE_TRUST_REQUIRED" for item in blocked.exclusions)
    # Other agents are unaffected by Cursor's untrusted folder.
    other = pool.select_resource(_task(untrusted), role="implementation")
    assert other.selected is not None and other.selected.resource_id != "cursor"


def test_orchestration_skips_trust_gated_agents_for_this_workspace_only(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestration.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(orchestration, "discover_models", lambda agent: [])
    agents = orchestration.inventory({}, tmp_path)
    assert agents["cursor"]["workspace_trust"]["state"] == trust.TRUST_REQUIRED
    assert agents["codex"]["workspace_trust"]["state"] == trust.READY
    doc = {"agents": agents, "repository": str(tmp_path), "orchestrator": "AUTO", "strategy": "BALANCED",
           "role_models": {}, "fallbacks": {}, "model_states": {}, "known_models": {}, "timed_out_assignments": [],
           "execution_budget": orchestration.default_execution_budget(), "goal": "g", "constraints": []}
    chosen = {agent for agent, _ in orchestration.candidates(doc, "implementation")}
    assert "cursor" not in chosen and "devin_cli" not in chosen and "codex" in chosen
    reason = orchestration.capability_skip_reason(doc, "cursor", "implementation")
    assert "workspace trust required" in reason
    # Preparing the workspace (e.g. before `resume`) lifts a store-based block.
    cursor_trust(stores, tmp_path)
    assert orchestration.capability_skip_reason(doc, "cursor", "implementation") is None


def test_orchestration_trust_failure_is_scoped_to_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestration.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(orchestration, "discover_models", lambda agent: [])
    agents = orchestration.inventory({}, tmp_path)
    doc = {"agents": agents, "repository": str(tmp_path), "requested_orchestrator": "AUTO"}
    assert orchestration.record_failure(doc, "cursor", "implementation", "UNKNOWN", "WORKSPACE_TRUST_REQUIRED") is None
    record = doc["agents"]["cursor"]
    assert record["capabilities"]["unattended_execution"] is not False
    assert record["capacity"] == {} and record["state"] == "AVAILABLE"
    assert record["workspace_trust"]["source"] == "orchestrate session"
    assert set(record["workspace_trust"]) == {"state", "workspace", "role", "source", "detected_at"}
    # An observed refusal is binding for the session even if the store now says trusted.
    assert "workspace trust required" in orchestration.capability_skip_reason(doc, "cursor", "review")


# ---------------------------------------------------------------- local_only consistency
def test_local_only_hides_hosted_agents_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_readiness, "hosted_probes_allowed", lambda: False)
    monkeypatch.setattr(agent_readiness, "probe_local", lambda *a, **k: pytest.fail("no hosted model listing"))
    monkeypatch.setattr(orchestration.shutil, "which", lambda binary: f"/bin/{binary}")
    assert agent_readiness.routing_models("agy") == [] and agent_readiness.routing_models("devin_cli") == []
    agents = orchestration.inventory({}, tmp_path)
    assert all(record["state"] == "UNAVAILABLE" and record["models"] == [] for record in agents.values())
    assert all("local_only" in record["unavailable_reason"] for record in agents.values())


# ---------------------------------------------------------------- factory readiness with a workspace
def _summary(agent, **overrides):
    base = {"agent": agent, "name": agent_readiness.SPECS[agent].name, "installed": True, "authenticated": True,
            "unattended_execution": True, "mutation_capable": True,
            "capacity": {"state": "UNKNOWN", "limits": []}, "live_smoke": {"status": "NOT_RUN", "last_status": "NOT_RUN"}}
    base.update(overrides)
    return base


def _workspace(states):
    return {"workspace": "/w", "authorized": True,
            "agents": {agent: {"state": state} for agent, state in states.items()}}


def test_factory_degrades_when_some_agents_are_trust_gated():
    summaries = [_summary(agent) for agent in agent_readiness.AGENT_ORDER]
    report = _workspace({"codex": "READY", "claude_code": "READY", "agy": "READY",
                         "cursor": "TRUST_REQUIRED", "devin_cli": "TRUST_REQUIRED"})
    readiness = agent_readiness.factory_readiness(summaries, report)
    assert readiness["status"] == "DEGRADED"
    assert readiness["workspace_trust_required"] == ["Cursor", "Devin"]
    assert "Cursor" not in readiness["implementation_capable"]


def test_factory_blocks_when_every_implementer_is_trust_gated():
    summaries = [_summary("cursor"), _summary("devin_cli")]
    gated = _workspace({"cursor": "TRUST_REQUIRED", "devin_cli": "TRUST_REQUIRED"})
    readiness = agent_readiness.factory_readiness(summaries, gated)
    assert readiness["status"] == "BLOCKED"


# ---------------------------------------------------------------- `howlplane factory prepare`
def _fake_cli(bin_dir, name, body):
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o700)


def _prepare_env(tmp_path, monkeypatch, stores):
    repo = make_git_repo(tmp_path)
    set_xdg_paths(monkeypatch, tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    slug = "$(printf '%s' \"$ws\" | sed -e 's/[^a-zA-Z0-9]/-/g' -e 's/--*/-/g' -e 's/^-//' -e 's/-$//')"
    _fake_cli(bin_dir, "agent", (
        'ws=""; trust=0; prev=""\nfor a in "$@"; do [ "$prev" = "--workspace" ] && ws="$a"; '
        '[ "$a" = "--trust" ] && trust=1; prev="$a"; done\n'
        f'if [ $trust = 1 ]; then d="$CURSOR_DATA_DIR/projects/{slug}"; mkdir -p "$d"; '
        'echo "{}" > "$d/.workspace-trusted"; fi\n'
        f'if [ -f "$CURSOR_DATA_DIR/projects/{slug}/.workspace-trusted" ] || [ $trust = 1 ]; then '
        'echo "Cannot use this model: x"; else echo "Workspace Trust Required"; fi\nexit 1'))
    _fake_cli(bin_dir, "devin", 'echo "Error: Refusing to run in an untrusted workspace: $PWD"; exit 1')
    for name in ("codex", "claude", "agy"):
        _fake_cli(bin_dir, name, "echo fake; exit 0")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return repo


def _prepare_args(repo, **values):
    base = dict(repo=str(repo), agent=None, yes=False, revoke=False, live=False, json=True)
    base.update(values)
    return argparse.Namespace(**base)


def test_prepare_without_confirmation_changes_nothing(stores, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane.factory import prepare
    repo = _prepare_env(tmp_path, monkeypatch, stores)
    assert prepare.command(_prepare_args(repo)) == 2
    assert trust.load_registry() == {}
    assert not (stores["cursor"] / "projects").exists()
    err = capsys.readouterr().err
    assert str(repo) in err and "Factory workspace root" in err


def test_prepare_authorizes_creates_the_worktree_and_prepares_supported_trust(stores, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane.factory import prepare
    from howlplane.control_plane.factory.campaign import discover_repository, factory_workspace_root
    repo = _prepare_env(tmp_path, monkeypatch, stores)
    assert prepare.command(_prepare_args(repo, yes=True)) == 0
    report = json.loads(capsys.readouterr().out)
    root = factory_workspace_root(discover_repository(repo))
    assert report["factory_root"] == str(root)
    assert Path(report["target"]).is_dir() and root in Path(report["target"]).parents
    assert {entry["state"] for entry in report["trust"]["cursor"]} == {"READY"}
    # Devin needs its own prompt answered at a terminal; without one it is reported, not faked.
    assert {entry["state"] for entry in report["trust"]["devin_cli"]} == {"TRUST_REQUIRED"}
    assert report["workspace"]["agents"]["cursor"]["state"] == "READY"
    assert report["workspace"]["agents"]["devin_cli"]["state"] == "TRUST_REQUIRED"
    assert report["readiness"]["status"] in ("READY", "DEGRADED")
    assert report["readiness"]["workspace_trust_required"] == ["Devin"]
    entry = trust.authorized_scope_for(repo)
    assert entry["agents_prepared"]["cursor"]["state"] == "READY"
    # A fresh canary worktree under the root needs no further preparation.
    assert trust.check("cursor", root / "canaries" / "next" / "target")["state"] == "READY"


def test_prepare_in_local_only_mode_contacts_no_vendor(stores, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane.factory import prepare
    repo = _prepare_env(tmp_path, monkeypatch, stores)
    monkeypatch.setattr(agent_readiness, "hosted_probes_allowed", lambda: False)
    monkeypatch.setattr(trust, "_run", lambda *a, **k: pytest.fail("local_only forbids vendor calls"))
    assert prepare.command(_prepare_args(repo, yes=True)) in (0, 1)
    assert not (stores["cursor"] / "projects").exists()


def test_revoke_reports_vendor_locations_without_touching_them(stores, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane.factory import prepare
    repo = _prepare_env(tmp_path, monkeypatch, stores)
    prepare.command(_prepare_args(repo, yes=True))
    capsys.readouterr()
    markers = sorted(p for p in (stores["cursor"] / "projects").rglob(".workspace-trusted"))
    assert prepare.command(_prepare_args(repo, revoke=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["revoked"] is True and "devin_cli" in out["vendor_trust_stores"]
    assert trust.authorized_scope_for(repo) is None
    assert sorted(p for p in (stores["cursor"] / "projects").rglob(".workspace-trusted")) == markers


def test_canaries_live_under_the_stable_factory_root(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import discover_repository, factory_workspace_root, resolve_campaign
    repo = make_git_repo(tmp_path)
    set_xdg_paths(monkeypatch, tmp_path)
    root = factory_workspace_root(discover_repository(repo))
    first = resolve_campaign(repo, bounded=True).target_dir
    second = resolve_campaign(repo, bounded=True).target_dir
    assert first != second and root in first.parents and root in second.parents
    assert resolve_campaign(repo).target_dir == (root / "target").resolve()


# ---------------------------------------------------------------- doctor
def test_agents_doctor_separates_global_and_workspace_readiness(stores, tmp_path, monkeypatch, capsys):
    from tests.test_agent_readiness import install
    install(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    args = argparse.Namespace(agent=None, live=False, refresh=True, smoke_timeout=10, json=False, repo=str(workspace))
    assert agent_readiness.command(args) == 0
    out = capsys.readouterr().out
    assert out.index("GLOBAL READINESS") < out.index("WORKSPACE READINESS")
    assert "Cursor: TRUST REQUIRED" in out and "Codex: READY" in out
    assert "Authorized:     NO" in out
    args.json = True
    agent_readiness.command(args)
    document = json.loads(capsys.readouterr().out)
    assert document["workspace"]["agents"]["devin_cli"]["state"] == "TRUST_REQUIRED"
    # Global readiness stays agent-wide: an untrusted folder does not make Cursor unavailable.
    cursor = next(item for item in document["agents"] if item["agent"] == "cursor")
    assert cursor["installed"] and cursor["unattended_execution"] is not False


def test_workspace_live_smoke_runs_in_that_workspace(stores, tmp_path, monkeypatch):
    from tests.test_agent_readiness import install
    install(tmp_path, monkeypatch, agents=["codex"])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen = []

    def runner(agent, timeout, at, workspace=None):
        seen.append(workspace)
        return {"observed_at": agent_readiness.iso(at), "status": "PASS", "exit_code": 0, "latency_seconds": 0.1,
                "failure_class": None, "model_reported": None, "capacity_windows": None, "error": None}

    agent_readiness.evaluate(["codex"], live=True, smoke_runner=runner, workspace=str(workspace))
    assert seen == [str(workspace)]
    status = agent_readiness.workspace_status("codex", workspace)
    assert status["live_smoke"]["status"] == "PASS"


def test_backends_never_inherit_the_operator_terminal(tmp_path, monkeypatch):
    from howlplane.control_plane import agent_execution
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        raise OSError("stop")

    monkeypatch.setattr(agent_execution.subprocess, "run", spy)
    backend = agent_execution.AgentBackendRegistry.get_backend("cursor")
    monkeypatch.setattr(backend, "is_available", lambda: True)
    with pytest.raises(OSError):
        backend.execute(_task(tmp_path), tmp_path, role="review", prompt_override="x", timeout_seconds=5)
    assert seen["stdin"] is agent_execution.subprocess.DEVNULL
    seen.clear()
    monkeypatch.setattr(agent_execution.subprocess, "Popen", spy)
    # The observed path reports a spawn failure as a result instead of raising.
    outcome = backend.execute(_task(tmp_path), tmp_path, role="review", prompt_override="x", timeout_seconds=5,
                              watchdog_callback=lambda *a: None)
    assert not outcome.success
    assert seen["stdin"] is agent_execution.subprocess.DEVNULL
    assert os.environ.get("HOWLPLANE_WORKSPACE_AUTHORIZATION_FILE")
