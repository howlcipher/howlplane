#!/usr/bin/env python3
"""
test_local_ollama_provider.py

Milestone #58 CI-safe test suite for the local Ollama (qwen2.5-coder:7b-instruct)
AgentBackend: deterministic discovery, memory safety, single-flight concurrency,
context budget, non-exhaustion failure classification, Tier-3 eligibility gating,
bounded local-only campaign continuation, and dogfood resume/status UX.

Never installs Ollama, never downloads a model, never requires a GPU or a real
subscription. All Ollama interactions are injected fakes.
"""

from pathlib import Path
import subprocess
from typing import Dict

import pytest

from src.control_plane.git_integration import PR_MERGE_FIELDS
from src.control_plane.agent_execution import (
    AgentBackendRegistry,
    OllamaAvailabilityReason,
    OllamaDiagnostics,
    OllamaLocalBackend,
    diagnose_ollama,
)
from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.locking import LocalInferenceLock
from src.control_plane.synthesis.campaign_state import DurableCampaignState
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
    is_task_local_eligible,
)
from src.control_plane.task_spec import TaskSpec
from tests._dogfood_test_helpers import FakeOrchestrator


class _AlwaysSucceedGitRunner:
    """
    Generic real-git-shaped fake for tests that only care about the
    local-only-continuation loop's counters, not exact plumbing (#59):
    returns success with SHA-consistent stdout for every call.
    """

    def __call__(self, repo_root, args, timeout=60):
        cmd = args[0] if args else ""
        if cmd == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="fakesha1234\n", stderr="")
        if cmd == "ls-remote":
            return subprocess.CompletedProcess(args, 0, stdout="fakesha1234\trefs/heads/x\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class _AlwaysSucceedGhRunner:
    """Generic real-gh-shaped fake mirroring _AlwaysSucceedGitRunner."""

    def __call__(self, repo_root, args, timeout=120):
        joined = " ".join(args)
        if args[:2] == ["pr", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["pr", "list"]:
            return subprocess.CompletedProcess(
                args, 0, stdout='[{"number": 1, "url": "https://github.com/x/y/pull/1"}]', stderr=""
            )
        if args[:2] == ["pr", "view"] and "headRefName" in joined:
            return subprocess.CompletedProcess(args, 0, stdout='{"headRefName": "fix/x"}', stderr="")
        if args[:2] == ["pr", "view"] and PR_MERGE_FIELDS in joined:
            return subprocess.CompletedProcess(
                args, 0, stdout='{"state": "MERGED", "merged": true, "mergeCommit": {"oid": "fakemergesha"}}', stderr=""
            )
        if args[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout='{"number": 1}', stderr="")
        if args[:2] == ["pr", "checks"]:
            return subprocess.CompletedProcess(
                args, 0, stdout='[{"name": "test-python", "state": "SUCCESS", "bucket": "pass"}]', stderr=""
            )
        if args[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "api":
            return subprocess.CompletedProcess(
                args, 0, stdout='{"required_status_checks": {"contexts": ["test-python"]}}', stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _task(risk_level="low", task_class="bug_fix", skills=None, task_id="T-1"):
    return TaskSpec(
        task_id=task_id, repository="howlplane", objective="fix a thing",
        task_class=task_class, risk_level=risk_level, required_skills=skills or [],
    )


# --- Phase 2: discovery -----------------------------------------------------

def test_diagnose_ollama_not_installed():
    diag = diagnose_ollama(binary_check=lambda: False)
    assert diag.reason == OllamaAvailabilityReason.NOT_INSTALLED
    assert diag.available is False


def test_diagnose_ollama_service_unavailable():
    diag = diagnose_ollama(binary_check=lambda: True, service_check=lambda host: False)
    assert diag.reason == OllamaAvailabilityReason.SERVICE_UNAVAILABLE
    assert diag.available is False


def test_diagnose_model_not_installed():
    diag = diagnose_ollama(
        binary_check=lambda: True,
        service_check=lambda host: True,
        model_check=lambda model, host: False,
    )
    assert diag.reason == OllamaAvailabilityReason.MODEL_NOT_INSTALLED
    assert diag.available is False


def test_diagnose_available():
    diag = diagnose_ollama(
        binary_check=lambda: True,
        service_check=lambda host: True,
        model_check=lambda model, host: True,
        memory_reader=lambda: 16.0,
    )
    assert diag.reason == OllamaAvailabilityReason.AVAILABLE
    assert diag.available is True


def test_diagnose_resource_constrained():
    diag = diagnose_ollama(
        binary_check=lambda: True,
        service_check=lambda host: True,
        model_check=lambda model, host: True,
        memory_reader=lambda: 2.0,
        min_available_ram_gib=8.0,
    )
    assert diag.reason == OllamaAvailabilityReason.RESOURCE_CONSTRAINED
    assert diag.available is False
    assert diag.available_ram_gib == 2.0


# --- Phase 4/5: real backend, memory guard ----------------------------------

def _available_backend(tmp_path, http_generate=None, min_ram=8.0):
    return OllamaLocalBackend(
        repo_root=tmp_path,
        min_available_ram_gib=min_ram,
        diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True, available_ram_gib=16.0),
        http_generate=http_generate,
        memory_reader=lambda: 16.0,
    )


def test_backend_not_invoked_when_resource_constrained(tmp_path):
    calls = []
    backend = OllamaLocalBackend(
        repo_root=tmp_path,
        diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.RESOURCE_CONSTRAINED, available=False, available_ram_gib=2.0),
        http_generate=lambda payload, timeout: calls.append(payload) or {"response": "x", "done": True},
    )
    result = backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert calls == []  # inference must never be launched
    assert result.success is False
    assert result.error_message == "RESOURCE_CONSTRAINED"


def test_backend_invoked_when_available(tmp_path):
    calls = []

    def fake_generate(payload, timeout):
        calls.append(payload)
        return {"response": "LOCAL_OK", "done": True}

    backend = _available_backend(tmp_path, http_generate=fake_generate)
    result = backend.execute(_task(), tmp_path, prompt_override="Respond only with LOCAL_OK")
    assert len(calls) == 1
    assert result.success is True
    assert result.stdout == "LOCAL_OK"
    assert result.metadata["provider"] == "local_ollama"
    assert result.metadata["model"]


def test_default_keep_alive_preserves_interactive_behavior(tmp_path):
    """Default keep_alive ("5m") matches Ollama's own implicit default --
    interactive/local development behavior is unchanged by #59."""
    payloads = []
    backend = _available_backend(tmp_path, http_generate=lambda p, t: payloads.append(p) or {"response": "ok", "done": True})
    backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert payloads[0]["keep_alive"] == "5m"


def test_keep_alive_zero_sent_for_overnight_profile(tmp_path):
    """Overnight-safe campaigns construct a backend with keep_alive=0 so the
    model unloads immediately after each inference (#59 Phase 17)."""
    payloads = []
    backend = OllamaLocalBackend(
        repo_root=tmp_path, keep_alive=0,
        diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True, available_ram_gib=16.0),
        http_generate=lambda p, t: payloads.append(p) or {"response": "ok", "done": True},
        memory_reader=lambda: 16.0,
    )
    backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert payloads[0]["keep_alive"] == 0


def test_overnight_ram_threshold_9gib_blocks_below_floor():
    """The overnight-safe profile's conservative 9 GiB launch threshold
    (#59 Phase 16) blocks launch even when the interactive 8 GiB floor
    would have allowed it."""
    diag = diagnose_ollama(
        binary_check=lambda: True, service_check=lambda host: True,
        model_check=lambda model, host: True,
        min_available_ram_gib=9.0, memory_reader=lambda: 8.5,
    )
    assert diag.available is False
    assert diag.reason == OllamaAvailabilityReason.RESOURCE_CONSTRAINED


def test_ram_measured_before_after_and_after_unload(tmp_path):
    """RAM before inference, RAM immediately after response, and (when
    keep_alive=0) RAM after unload are all recorded as evidence (#59 Phase 17)."""
    readings = iter([16.0, 15.2, 15.1])  # before, after-response, after-unload
    backend = OllamaLocalBackend(
        repo_root=tmp_path, keep_alive=0,
        diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True, available_ram_gib=16.0),
        http_generate=lambda p, t: {"response": "LOCAL_OK", "done": True},
        memory_reader=lambda: next(readings),
    )
    result = backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert result.metadata["ram_before_gib"] == 16.0
    assert result.metadata["ram_after_gib"] == 15.2
    assert result.metadata["ram_after_unload_gib"] == 15.1


def test_ram_after_unload_not_measured_when_keep_alive_nonzero(tmp_path):
    """When keep_alive is not 0, no unload was requested, so no
    ram_after_unload_gib measurement is claimed."""
    backend = _available_backend(tmp_path, http_generate=lambda p, t: {"response": "ok", "done": True})
    result = backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert result.metadata["ram_after_unload_gib"] is None


def test_backend_engineering_failure_on_bad_output(tmp_path):
    backend = _available_backend(tmp_path, http_generate=lambda p, t: {"response": "", "done": True})
    result = backend.execute(_task(), tmp_path, prompt_override="do something")
    assert result.success is False
    assert result.error_message == "ENGINEERING_FAILURE"


def test_backend_context_budget_enforced(tmp_path):
    calls = []
    backend = OllamaLocalBackend(
        repo_root=tmp_path, context_length=8,
        diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True),
        http_generate=lambda p, t: calls.append(p) or {"response": "ok", "done": True},
    )
    huge_prompt = "x" * 1000
    result = backend.execute(_task(), tmp_path, prompt_override=huge_prompt)
    assert calls == []
    assert result.error_message == "LOCAL_CONTEXT_INSUFFICIENT"


def test_backend_provider_unavailable_on_transport_error(tmp_path):
    def boom(payload, timeout):
        raise ConnectionError("connection refused")

    backend = _available_backend(tmp_path, http_generate=boom)
    result = backend.execute(_task(), tmp_path, prompt_override="fix it")
    assert result.success is False
    assert result.error_message == "LOCAL_PROVIDER_UNAVAILABLE"


# --- Phase 7: concurrency ----------------------------------------------------

def test_single_local_inference_allowed(tmp_path):
    lock = LocalInferenceLock(tmp_path, "task-a")
    assert lock.acquire() is True
    lock.release()


def test_second_concurrent_local_inference_fails_safely(tmp_path):
    lock1 = LocalInferenceLock(tmp_path, "task-a")
    lock1.acquire()
    try:
        calls = []
        backend = _available_backend(tmp_path, http_generate=lambda p, t: calls.append(p) or {"response": "ok", "done": True})
        result = backend.execute(_task(task_id="task-b"), tmp_path, prompt_override="fix it")
        assert calls == []
        assert result.success is False
        assert result.error_message == "LOCAL_PROVIDER_BUSY"
    finally:
        lock1.release()


# --- Phase 9/11: eligibility gating and non-exhaustion classification -------

def test_high_risk_task_never_local_eligible():
    assert is_task_local_eligible(_task(risk_level="high")) is False
    assert is_task_local_eligible(_task(risk_level="critical")) is False
    assert is_task_local_eligible(_task(risk_level="medium")) is False
    assert is_task_local_eligible(None) is False


def test_security_skill_never_local_eligible():
    assert is_task_local_eligible(_task(risk_level="low", skills=["cyber_security"])) is False


def test_low_risk_bug_fix_is_local_eligible():
    assert is_task_local_eligible(_task(risk_level="low", task_class="bug_fix")) is True


def test_high_risk_task_excluded_from_candidates_even_when_local_available():
    pool = _cloud_exhausted_local_available_pool()
    candidates = pool.select_candidates(task_category="code_heavy", task=_task(risk_level="high"))
    assert "local_ollama" not in candidates
    assert candidates == []


def test_low_risk_task_includes_local_when_cloud_exhausted():
    pool = _cloud_exhausted_local_available_pool()
    candidates = pool.select_candidates(task_category="code_heavy", task=_task(risk_level="low"))
    assert candidates == ["local_ollama"]


def test_engineering_failure_is_not_provider_exhaustion():
    from src.control_plane.agent_execution import AgentExecutionResult
    pool = ProviderPoolManager()
    pool.set_status("local_ollama", ProviderAvailabilityStatus.AVAILABLE)
    bad_result = AgentExecutionResult(
        agent_id="local_ollama", role="implementation", command="ollama_local", exit_code=1,
        stdout="", stderr="wrong answer", duration_seconds=1.0, success=False,
        error_message="ENGINEERING_FAILURE",
    )
    event = pool.detect_exhaustion("local_ollama", bad_result)
    assert event is None
    assert pool.get_status("local_ollama") == ProviderAvailabilityStatus.AVAILABLE


def test_resource_constrained_is_not_session_exhausted():
    from src.control_plane.agent_execution import AgentExecutionResult
    pool = ProviderPoolManager()
    pool.set_status("local_ollama", ProviderAvailabilityStatus.AVAILABLE)
    constrained = AgentExecutionResult(
        agent_id="local_ollama", role="implementation", command="ollama_local", exit_code=-2,
        stdout="", stderr="not enough RAM", duration_seconds=0.0, success=False,
        error_message="RESOURCE_CONSTRAINED",
    )
    event = pool.detect_exhaustion("local_ollama", constrained)
    assert event is not None
    assert pool.get_status("local_ollama") == ProviderAvailabilityStatus.RESOURCE_CONSTRAINED
    assert not pool.is_all_exhausted()  # RESOURCE_CONSTRAINED is not SESSION_EXHAUSTED/RATE_LIMITED


def test_alias_ollama_local_resolves_to_local_ollama():
    assert AgentBackendRegistry.normalize_agent_id("ollama_local") == "local_ollama"
    backend = AgentBackendRegistry.get_backend("ollama_local")
    assert isinstance(backend, OllamaLocalBackend)


# --- Phase 1: resume scope preservation + read-only status ------------------

def test_resume_preserves_persisted_benchmark_scope(tmp_path):
    ledger_dir = tmp_path / "dogfood"
    engine = MarathonDogfoodEngine(base_output_dir=ledger_dir, campaign_dir=tmp_path / "runs")
    first = engine.run_marathon(benchmarks=["notes"], max_iterations=1)
    assert first.benchmark_results[0].benchmark_id == "notes"

    # Resume WITHOUT --benchmarks: must not silently expand to the full default suite.
    second_engine = MarathonDogfoodEngine(base_output_dir=ledger_dir, campaign_dir=tmp_path / "runs")
    second = second_engine.run_marathon(resume_campaign_id=first.campaign_id, max_iterations=5)
    # "notes" already succeeded; scope was only ever ["notes"], so nothing left to do.
    assert second.iterations_attempted == 0
    assert second.stopped_reason == "completed_all_benchmarks"

    state = DurableCampaignState.load(tmp_path / "runs" / first.campaign_id)
    assert state.requested_benchmarks == ["notes"]


def test_resume_with_explicit_benchmarks_extends_scope_not_overrides(tmp_path):
    engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "dogfood", campaign_dir=tmp_path / "runs")
    first = engine.run_marathon(benchmarks=["notes"], max_iterations=1)

    second_engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "dogfood", campaign_dir=tmp_path / "runs")
    second = second_engine.run_marathon(
        resume_campaign_id=first.campaign_id, benchmarks=["todo"], max_iterations=5,
    )
    assert second.benchmark_results[0].benchmark_id == "todo"

    state = DurableCampaignState.load(tmp_path / "runs" / first.campaign_id)
    assert state.requested_benchmarks == ["notes", "todo"]
    assert state.scope_extensions and state.scope_extensions[0]["added"] == ["todo"]


def test_dogfood_status_is_read_only(tmp_path, monkeypatch, capsys):
    from src.control_plane import cli

    engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "dogfood", campaign_dir=tmp_path / "runs")
    report = engine.run_marathon(benchmarks=["notes"], max_iterations=1)

    # Ensure the status path never touches provider probing: monkeypatch
    # ProviderPoolManager.__init__ to fail loudly if constructed.
    def _boom(*args, **kwargs):
        raise AssertionError("cmd_dogfood_status must never construct a ProviderPoolManager")

    monkeypatch.setattr(ProviderPoolManager, "__init__", _boom)

    rc = cli.main([
        "dogfood", "--status", report.campaign_id, "--campaign-dir", str(tmp_path / "runs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "notes" in out
    assert "Status Inspection" in out


def test_dogfood_status_unknown_campaign_fails_closed(tmp_path, capsys):
    from src.control_plane import cli
    rc = cli.main(["dogfood", "--status", "DOES-NOT-EXIST", "--campaign-dir", str(tmp_path / "runs")])
    assert rc == 1


# --- Phase 3: `ai local setup` -----------------------------------------------

def test_local_setup_reports_fail_when_ollama_missing(monkeypatch, capsys):
    from src.control_plane import cli
    import shutil as real_shutil

    monkeypatch.delenv("HOWLPLANE_LOCAL_AUTO_INSTALL", raising=False)
    monkeypatch.setattr(real_shutil, "which", lambda name: None)

    rc = cli.main(["local", "setup"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


# --- Phase 16: resume rechecks cloud availability ---------------------------

def test_resume_rechecks_cloud_provider_availability(tmp_path, monkeypatch):
    # `reset_transient_exhaustion()` re-probes live binary presence on resume;
    # simulate codex's CLI becoming reachable again (e.g. re-authenticated)
    # independent of whether it happens to be on THIS machine's PATH.
    import shutil as real_shutil
    real_which = real_shutil.which
    monkeypatch.setattr(real_shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else real_which(name))

    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.SESSION_EXHAUSTED)
    engine = MarathonDogfoodEngine(provider_pool=pool, base_output_dir=tmp_path / "dogfood", campaign_dir=tmp_path / "runs")
    first = engine.run_marathon(benchmarks=["notes"], max_iterations=1)

    # Quota reset between sessions: resume must re-probe live provider
    # availability (#58 Phase 16), not stay pinned to the prior exhausted state.
    second = engine.run_marathon(resume_campaign_id=first.campaign_id, benchmarks=["todo"], max_iterations=5)
    assert pool.get_status("codex") == ProviderAvailabilityStatus.AVAILABLE
    assert second.benchmark_results[0].benchmark_id == "todo"


# --- Phase 13/14: bounded local-only continuation ---------------------------

class _AlwaysAvailableLocalBackend(OllamaLocalBackend):
    def __init__(self, *, succeed: bool):
        super().__init__(
            diagnostics_fn=lambda: OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True, available_ram_gib=16.0),
            http_generate=lambda p, t: {"response": "fix applied" if succeed else "", "done": True},
            memory_reader=lambda: 16.0,
        )


def _cloud_exhausted_local_available_pool() -> ProviderPoolManager:
    pool = ProviderPoolManager()
    for p in ["codex", "agy", "devin_cli", "claude_code", "gemini_cli"]:
        pool.set_status(p, ProviderAvailabilityStatus.SESSION_EXHAUSTED)
    pool.set_status("local_ollama", ProviderAvailabilityStatus.AVAILABLE)
    return pool


def _patch_local_backend_always_available(monkeypatch, *, succeed: bool) -> None:
    def fake_get_backend(cls, agent_id, custom_backend=None):
        nid = cls.normalize_agent_id(agent_id)
        if nid == "local_ollama":
            return _AlwaysAvailableLocalBackend(succeed=succeed)
        return cls._instances.get(nid)

    monkeypatch.setattr(AgentBackendRegistry, "get_backend", classmethod(fake_get_backend))


def _gap_record(code: str, behavior: str) -> Dict[str, str]:
    return {"code": code, "target_component": "howlplane_synthesis", "required_behavior": behavior, "impact": "blocks_product_synthesis"}


def _local_only_engine(tmp_path, monkeypatch, *, succeed: bool = True) -> MarathonDogfoodEngine:
    _patch_local_backend_always_available(monkeypatch, succeed=succeed)
    pool = _cloud_exhausted_local_available_pool()
    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        base_output_dir=tmp_path / "dogfood",
        campaign_dir=tmp_path / "runs",
        target_repo=tmp_path,
        repo_slug="howlcipher/howlplane",
        orchestrator_factory=lambda config: FakeOrchestrator(tmp_path / "fake_run", "src/control_plane/howlframe_runtime.py"),
    )
    # These tests call _run_local_only_continuation() directly, bypassing
    # run_marathon()'s envelope-binding/git-executor construction step, so
    # both must be set up explicitly here rather than via the constructor
    # injection seams (which only take effect when run_marathon() runs).
    engine.authority_envelope = create_envelope(
        get_profile("overnight-safe"), "TEST-LOCAL-ONLY-CAMPAIGN", "cli:test@host",
    )
    engine.git_executor = GitIntegrationExecutor(
        tmp_path, "howlcipher/howlplane", engine.authority_envelope,
        git_runner=_AlwaysSucceedGitRunner(), gh_runner=_AlwaysSucceedGhRunner(),
    )
    return engine


def test_local_only_continuation_resolves_pending_gap_and_bounds_iterations(tmp_path, monkeypatch):
    engine = _local_only_engine(tmp_path, monkeypatch)
    state = DurableCampaignState(campaign_id="TEST-LOCAL-ONLY")
    state.record_framework_gap(_gap_record("HF_GAP_TODO", "fix the todo benchmark"))

    stop_reason = engine._run_local_only_continuation(state, avoid_provider=None)
    assert stop_reason == "all_providers_exhausted"
    assert state.local_model["success_count"] == 1
    assert any(t.get("provider") == "local_ollama" for t in state.completed_tasks)


def test_local_only_continuation_budget_is_finite(tmp_path, monkeypatch):
    engine = _local_only_engine(tmp_path, monkeypatch)
    # Prevent ambient low-RAM conditions from short-circuiting the budget test.
    monkeypatch.setattr(
        "src.control_plane.synthesis.marathon.read_available_memory_gib", lambda: 64.0
    )
    state = DurableCampaignState(campaign_id="TEST-LOCAL-ONLY-BUDGET")
    state.ensure_local_model_defaults()
    state.local_model["local_only_iteration_limit"] = 2
    for i in range(5):
        state.record_framework_gap(_gap_record(f"HF_GAP_{i}", f"fix gap {i}"))

    stop_reason = engine._run_local_only_continuation(state, avoid_provider=None)
    assert stop_reason == "local_only_budget_reached"
    # Never more than the configured local-only budget of consecutive iterations.
    assert state.local_model["local_only_iterations"] <= 2


def test_local_only_continuation_never_engages_for_high_risk_gap(tmp_path, monkeypatch):
    """Even inside LOCAL_ONLY_CONTINUATION, ineligible work never routes to local."""
    _local_only_engine(tmp_path, monkeypatch)

    # Directly exercise the eligibility gate the continuation path relies on:
    # with every cloud provider exhausted and only local_ollama AVAILABLE, a
    # high-risk probe still yields zero candidates.
    candidates = _cloud_exhausted_local_available_pool().select_candidates(
        task_category="code_heavy", task=_task(risk_level="high"),
    )
    assert candidates == []
