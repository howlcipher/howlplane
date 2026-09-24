"""Deterministic coverage for Cursor backend and orchestration rerouting fixes."""

import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import (
    AgentExecutionResult,
    AgentBackendRegistry,
    CursorBackend,
)
from tests.test_orchestration import arguments, enable_fake_codex, repository, result


pytestmark = pytest.mark.contract


def fake_binary_which(name: str) -> str | None:
    """Pretend Claude, Codex, and Cursor (binary 'agent') are installed for orchestration tests."""
    return f"/fake/{name}" if name in {"claude", "codex", "agent"} else None


def install_fake_agent(tmp_path: Path, script: str, name: str = "agent") -> Path:
    """Place a fake executable on a temporary PATH so shutil.which finds it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / name
    binary.write_text(f"#!/bin/sh\n{script}")
    binary.chmod(0o700)
    return bin_dir


def enable_fake_cursor_with_script(tmp_path, monkeypatch, script, binary_name="agent"):
    """Make only the Cursor agent binary available on PATH with the given script."""
    bin_dir = install_fake_agent(tmp_path, script, binary_name)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    return bin_dir


def _cursor_backend(tmp_path, monkeypatch, script="printf '%s\\n' \"$@\"\n"):
    """Return a CursorBackend backed by a fake ``agent`` executable."""
    bin_dir = enable_fake_cursor_with_script(tmp_path, monkeypatch, script)
    return CursorBackend(), bin_dir


def _task(tmp_path, objective):
    """Build a minimal TaskSpec for backend-level tests."""
    return module.TaskSpec(task_id="T", repository=str(tmp_path), objective=objective)


def _run_auto(repo, tmp_path, monkeypatch, execute, **overrides):
    """Run a hermetic AUTO ECONOMY session with Codex available and Cursor discoverable."""
    enable_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(module.shutil, "which", fake_binary_which)
    monkeypatch.setattr(module, "execute_assignment", execute)
    args = arguments(repo, orchestrator="AUTO", strategy="ECONOMY", **overrides)
    assert module.command(args) == 0


def _save_session(doc) -> Path:
    """Persist a hand-built orchestration document with a locked save; return its root."""
    lease = doc["lease"]
    lease["pid"] = 999999
    root = module.state_root()
    with module.locked(root):
        path = module.path_for(root, doc["id"])
        module.save(path, doc, token=lease["token"])
    return root


def _cursor_only_execute(cursor_result: AgentExecutionResult, calls: list | None = None):
    """Return an execute_assignment stub that returns cursor_result for Cursor and success otherwise."""
    def execute(document, role, agent, model, cwd):
        if calls is not None:
            calls.append((agent, model))
        if agent == "cursor":
            return cursor_result
        return result(agent, role)
    return execute


def test_cursor_backend_command_uses_agent_executable_and_print_mode(tmp_path, monkeypatch):
    """Cursor must invoke the installed ``agent`` CLI with -p, output-format, and workspace."""
    backend, bin_dir = _cursor_backend(tmp_path, monkeypatch)
    old_cursor = tmp_path / "cursor-agent"
    old_cursor.write_text("#!/bin/sh\necho 'wrong binary'\n")
    old_cursor.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert backend.binary_name == "agent"
    assert backend.is_available()

    outcome = backend.execute(_task(tmp_path, "Plan a fix"), tmp_path, role="planning", model_id="fast-model")
    assert outcome.success
    assert "agent" in outcome.command
    assert "cursor-agent" not in outcome.command
    assert "-p" in outcome.stdout
    assert "--output-format" in outcome.stdout
    assert "text" in outcome.stdout
    assert "--workspace" in outcome.stdout
    assert "--mode" in outcome.stdout
    assert "plan" in outcome.stdout


def test_cursor_backend_readonly_role_uses_plan_mode(tmp_path, monkeypatch):
    """Implementation uses the default editing mode; planning stays read-only."""
    backend, _ = _cursor_backend(tmp_path, monkeypatch)

    impl = backend.execute(_task(tmp_path, "Implement"), tmp_path, role="implementation")
    plan = backend.execute(_task(tmp_path, "Plan"), tmp_path, role="planning")
    assert "--mode" not in impl.stdout
    assert "--mode\nplan" in plan.stdout


def test_cursor_backend_no_unsafe_shell_concatenation(tmp_path, monkeypatch):
    """Arguments must be passed as discrete argv values, never a shell string."""
    script = """for a in "$0" "$@"; do printf '%s\\n' "$a"; done"""
    backend, _ = _cursor_backend(tmp_path, monkeypatch, script)

    outcome = backend.execute(_task(tmp_path, "Implement feature"), tmp_path, role="implementation", model_id="m")
    assert outcome.success
    lines = outcome.stdout.strip().splitlines()
    assert lines == [
        str(tmp_path / "bin" / "agent"),
        "--model", "m", "-p", "Execute task T: Implement feature",
        "--output-format", "text", "--workspace", str(tmp_path),
    ]


def test_cursor_backend_classifies_missing_executable_as_backend_unavailable(tmp_path, monkeypatch):
    """If ``agent`` is absent the backend reports MISSING_EXECUTABLE, not ENGINEERING_FAILURE."""
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    backend = CursorBackend()
    assert not backend.is_available()

    outcome = backend.execute(_task(tmp_path, "Plan"), tmp_path, role="planning")
    assert not outcome.success
    assert outcome.exit_code == 127
    assert outcome.metadata.get("launch_outcome") == "not_installed"

    classified = module.ProviderPoolManager.classify_result("cursor", outcome)
    assert classified.value == "MISSING_EXECUTABLE"


def test_cursor_backend_classifies_workspace_trust_block_as_permission_failure(tmp_path, monkeypatch):
    """A workspace-trust prompt must be classified as an unattended-execution limitation."""
    script = (
        "echo 'Workspace Trust Required' >&2; "
        "echo 'Pass --trust, --yolo, or -f if you trust this directory' >&2; "
        "exit 1"
    )
    backend, _ = _cursor_backend(tmp_path, monkeypatch, script)

    outcome = backend.execute(_task(tmp_path, "Plan"), tmp_path, role="implementation")
    assert not outcome.success
    assert "Workspace Trust Required" in (outcome.stderr or "")

    classified = module.ProviderPoolManager.classify_result("cursor", outcome)
    assert classified.value == "EXECUTION_PERMISSION_REQUIRED"


def test_cursor_backend_classifies_spawn_failure_as_missing_executable(tmp_path, monkeypatch):
    """An OS spawn refusal (e.g. path present but not executable) must retain launch diagnostics."""
    bin_dir = install_fake_agent(tmp_path, "echo ok", "agent")
    monkeypatch.setenv("PATH", str(bin_dir))
    backend = CursorBackend()
    assert backend.is_available()

    def raise_permission(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(bin_dir / "agent"))

    monkeypatch.setattr(module.subprocess, "Popen", raise_permission)

    outcome = backend.execute(
        _task(tmp_path, "Plan"), tmp_path, role="planning", watchdog_callback=lambda *a, **k: None
    )
    assert not outcome.success
    assert outcome.metadata.get("launch_outcome") == "spawn_failed"
    classified = module.ProviderPoolManager.classify_result("cursor", outcome)
    assert classified.value == "MISSING_EXECUTABLE"


def test_cursor_hard_failure_degrades_session_auto_eligibility(tmp_path, monkeypatch, capsys):
    """A hard Cursor engineering failure must make Cursor ineligible for later AUTO routing."""
    repo = repository(tmp_path)
    cursor_result = result("cursor", "planning", False, "engine failure")
    _run_auto(repo, tmp_path, monkeypatch, _cursor_only_execute(cursor_result))

    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    cursor = doc["agents"]["cursor"]
    assert cursor["state"] == "DEGRADED"
    assert cursor["capabilities"]["unattended_execution"] is False
    assert cursor["capabilities"]["reason"] == "ENGINEERING_FAILURE"


def test_identical_cursor_hard_failure_not_immediately_retried(tmp_path, monkeypatch, capsys):
    """Cursor failing hard once must not be selected again in the same stage loop."""
    repo = repository(tmp_path)
    calls = []
    cursor_result = result("cursor", "planning", False, "engine failure")
    _run_auto(repo, tmp_path, monkeypatch, _cursor_only_execute(cursor_result, calls=calls))
    assert calls.count(("cursor", "UNKNOWN")) <= 1
    output = capsys.readouterr().err
    assert "REROUTE" in output
    assert "Cursor" in output


def test_cursor_transient_failure_allows_one_retry_then_stops(tmp_path, monkeypatch, capsys):
    """TRANSPORT_UNAVAILABLE gets exactly one bounded retry, then Cursor is skipped."""
    repo = repository(tmp_path)
    calls = []

    def execute(document, role, agent, model, cwd):
        calls.append((agent, model))
        if agent == "cursor":
            err = "connection refused" if calls.count(("cursor", "UNKNOWN")) <= 1 else "engine failure"
            return result(agent, role, False, err)
        return result(agent, role)

    _run_auto(repo, tmp_path, monkeypatch, execute)
    cursor_calls = [c for c in calls if c[0] == "cursor"]
    assert len(cursor_calls) == 2


def test_reroute_target_equals_subsequent_assignment_target(tmp_path, monkeypatch, capsys):
    """The REROUTE event must name the same agent as the following ASSIGN event."""
    repo = repository(tmp_path)

    def execute(document, role, agent, model, cwd):
        return result(agent, role, agent == "codex", "quota exhausted" if agent == "claude_code" else "")

    _run_auto(repo, tmp_path, monkeypatch, execute)
    output = capsys.readouterr().err.splitlines()
    reroute_idx = next(
        i for i, line in enumerate(output) if "] REROUTE" in line and "Claude" in line and "Codex" in line
    )
    assign_idx = next(i for i, line in enumerate(output) if i > reroute_idx and "] ASSIGN" in line)
    reroute_target = output[reroute_idx].split("→")[-1].split()[0]
    assign_target = output[assign_idx].split("ASSIGN")[-1].split()[0]
    assert reroute_target == assign_target == "Codex"


def test_reroute_reports_actual_target_when_planned_candidate_unavailable(tmp_path, monkeypatch, capsys):
    """If an intermediate candidate becomes unavailable, REROUTE names the actual replacement."""
    repo = repository(tmp_path)

    def execute(document, role, agent, model, cwd):
        if agent == "claude_code":
            return result(agent, role, False, "execution permission required")
        if agent == "cursor":
            return result(agent, role, False, "engine failure")
        return result(agent, role)

    _run_auto(repo, tmp_path, monkeypatch, execute)
    output = capsys.readouterr().err.splitlines()
    reroutes = [(i, line) for i, line in enumerate(output) if "] REROUTE" in line and "→" in line]
    assigns = [(i, line) for i, line in enumerate(output) if "] ASSIGN" in line]
    for ri, rline in reroutes:
        target = rline.split("→")[-1].split()[0]
        next_assign = next((aline for ai, aline in assigns if ai > ri), "")
        assert target in next_assign, f"REROUTE target {target!r} not followed by matching ASSIGN"


def test_reserved_still_overrides_fallback(tmp_path, monkeypatch, capsys):
    """A RESERVED agent must never be emitted as a REROUTE target or selected as fallback."""
    repo = repository(tmp_path)
    cursor_result = result("cursor", "planning", False, "quota exhausted")

    def execute(document, role, agent, model, cwd):
        if agent == "cursor":
            return cursor_result
        return result(agent, role, agent == "codex", "")

    _run_auto(repo, tmp_path, monkeypatch, execute, claude_code="RESERVED")
    output = capsys.readouterr().err.splitlines()
    reroute_lines = [line for line in output if "] REROUTE" in line and "→" in line]
    assert len(reroute_lines) == 1
    assert "Claude" not in reroute_lines[0]
    assert "Codex" in reroute_lines[0]


def test_no_repository_change_includes_diagnostics(tmp_path, monkeypatch, capsys):
    """An implementation that produces no repository delta must report diagnostic detail."""
    repo = repository(tmp_path)

    def execute(document, role, agent, model, cwd):
        return result(agent, role, True, "")

    enable_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(module.shutil, "which", fake_binary_which)
    monkeypatch.setattr(module, "execute_assignment", execute)
    args = arguments(repo, policy="PLAN + EXECUTE")
    assert module.command(args) == 2
    output = capsys.readouterr().err
    assert "NO_REPOSITORY_CHANGE" in output
    assert "no repository delta detected" in output
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    failures = [a for a in doc["attempts"] if a.get("failure")]
    assert failures[0]["failure"] == "NO_REPOSITORY_CHANGE"


def test_redaction_of_cursor_subprocess_diagnostics(tmp_path, monkeypatch):
    """Cursor launch diagnostics must not leak credentials or secrets."""
    backend, _ = _cursor_backend(tmp_path, monkeypatch)

    task = module.TaskSpec(task_id="T", repository=str(tmp_path), objective="Use token=supersecret")
    outcome = backend.execute(task, tmp_path, role="implementation")
    assert outcome.success
    assert "supersecret" not in outcome.command
    assert "<redacted>" in outcome.command


def test_reroute_state_survives_checkpoint_and_resume(tmp_path, monkeypatch, capsys):
    """A reroute decision must be persisted and respected on resume."""
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    # Restrict availability to Cursor (binary 'agent') and Codex only.
    monkeypatch.setattr(
        module.shutil, "which",
        lambda name: f"/fake/{name}" if name in {"agent", "codex"} else None,
    )

    args = arguments(repo, orchestrator="AUTO", strategy="ECONOMY", policy="PLAN ONLY")
    doc = module.setup(args, repo)
    doc["retain_report"] = True
    doc["status"] = "PLANNING"
    doc["stage"] = "planning"
    doc["reroutes"].append({"from": "cursor:UNKNOWN", "stage": "planning", "reason": "ENGINEERING_FAILURE"})
    doc["agents"]["cursor"]["state"] = "DEGRADED"
    doc["agents"]["cursor"]["capabilities"] = {"unattended_execution": False, "reason": "ENGINEERING_FAILURE"}
    doc["attempts"].append(
        {"task_id": doc["id"][:8], "stage": "planning", "agent": "cursor", "model": "UNKNOWN",
         "state": "REVOKED", "failure": "ENGINEERING_FAILURE",
         "started_at": module.now(), "finished_at": module.now()}
    )
    root = _save_session(doc)

    monkeypatch.setattr(module, "execute_assignment", lambda d, r, a, m, c: result(a, r, a == "codex", ""))
    resume_args = arguments(repo, input="resume", orchestrator="AUTO", strategy="ECONOMY", policy="PLAN ONLY")
    assert module.command(resume_args) == 0

    resumed = module.active_sessions(root, repo, include_terminal=True)[0]
    assert resumed["status"] == "COMPLETE"
    assert any(r["from"].startswith("cursor") for r in resumed.get("reroutes", []))
    cursor_attempts = [a for a in resumed["attempts"] if a["agent"] == "cursor"]
    assert len(cursor_attempts) == 1


def test_cursor_backend_registry_exposes_agent_binary():
    """The provider registry exposes the Cursor backend bound to the ``agent`` binary."""
    backend = AgentBackendRegistry.get_backend("cursor")
    assert isinstance(backend, CursorBackend)
    assert backend.binary_name == "agent"
