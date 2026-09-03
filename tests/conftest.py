import os
from pathlib import Path
import shutil
import tempfile
import pytest

from src.control_plane.git_env import GIT_REPOSITORY_SELECTION_ENV_VARS


# Real HowlFrame compiler integration tests deliberately exercise the genuine
# `HOWLFRAME_BIN` env override -> `command -v howlframe` -> "unavailable" discovery
# contract (see src/control_plane/howlframe_runner.find_howlframe_binary) and
# already call `pytest.skip`/assert on `HOWLFRAME_UNAVAILABLE` when no real
# compiler is provisioned. They must NOT be pointed at the synthesis fixture's
# fake compiler, or they silently exercise a compiler substitute that does not
# implement their expected fidelity (real dogfood tests are the deliberately
# unfaked real-integration layer; only unit/control-plane and synthesis tests
# get the fake compiler injected).
_REAL_COMPILER_INTEGRATION_MODULES = {
    "test_howlframe_dogfood.py",
    "test_launcher.py",
}

# The taxonomy is deliberately file-family based so it stays visible at
# collection time without hiding tests behind a changed default invocation.
# Every test gets exactly one primary tier. ``slow`` is orthogonal and contains
# families that appeared in the measured duration report, not name guesses.
_ACCEPTANCE_MODULES = {
    "test_acceptance_canary.py", "test_acceptance_runner.py",
    "test_backlog_marathon.py", "test_clean_environment_regression.py",
    "test_cli_synthesis.py", "test_closed_loop_orchestrator.py",
    "test_dogfood_crash_recovery_git.py", "test_dogfood_failure_handling.py",
    "test_dogfood_hardening.py", "test_dogfood_parking.py",
    "test_howlframe_dogfood.py", "test_marathon_dogfood.py",
    "test_product_synthesis.py", "test_reasoning_strategy_dogfooding.py",
    "test_spec_synthesizer.py",
}
_INTEGRATION_MODULES = {
    "test_ai_resource_pool.py", "test_authority_execution_gap.py",
    "test_authority_profile.py", "test_factory_self_modification.py",
    "test_git_baseline.py", "test_git_env_isolation.py",
    "test_git_integration.py", "test_go_build.py",
    "test_human_approval_lifecycle.py", "test_install_global_codex.py",
    "test_install_pre_commit_hook.py", "test_install_pre_push_hook.py",
    "test_interrupted_governance_recovery.py", "test_local_ollama_provider.py",
    "test_operational_resilience.py", "test_provider_failover.py",
    "test_provider_permissions.py", "test_provider_pool.py",
    "test_provider_preflight.py", "test_progress.py",
    "test_review_integrity.py", "test_review_runner.py",
    "test_reviewer_pool_traversal.py", "test_scratch_isolation.py",
    "test_verification_view.py",
}
# ``slow`` is orthogonal to the tier and is applied at the granularity the
# measurement actually supports. A module belongs in _SLOW_MODULES only when the
# whole family is expensive; where one or two tests dominate an otherwise cheap
# module, the individual tests are named in _SLOW_TESTS instead. Marking a whole
# module slow to buy back one expensive test is what pushed the authority,
# security, isolation, and durable-recovery suites out of `make test-fast`.
#
# Both sets are seeded from `pytest --durations=0` (see documentation/TESTING.md
# for the recorded profile). tests/test_test_taxonomy.py fails if an entry here
# stops resolving to a real module or test.
_SLOW_MODULES = {
    "test_clean_environment_regression.py",   # 8.1s / 6 tests, end-to-end
    "test_closed_loop_orchestrator.py",       # 5.0s / 7 tests, end-to-end
    "test_effective_implementer_identity.py",  # 9.2s / 6 tests, ~1.5s each
    "test_howlframe_dogfood.py",              # 5.2s / 8 tests, real-compiler dogfood
    "test_provider_failover.py",              # 87.8s / 66 tests, uniformly ~1.3s
    "test_reasoning_strategy_dogfooding.py",  # 5.8s / 8 tests, governed dogfood
    "test_scratch_isolation.py",              # 7.2s / 6 tests, uniformly ~1.2s
}
# Individually expensive tests inside otherwise fast modules. Keeping these out
# of _SLOW_MODULES returns ~148 tests to the fast gate for ~9s.
_SLOW_TESTS = {
    "test_docs.py::test_pdoc_api_generation",                                      # 19.48s
    "test_langchain_compat.py::test_langchain_pydantic_warning_is_isolated",       # 8.12s
    "test_hygiene_policy.py::test_verification_plan_executes_with_hygiene_integrity_checks",  # 6.30s
    "test_install_global_codex.py::test_posix_installer_registers_codex_globally",  # 4.26s
    "test_provider_preflight.py::test_payload_loop_skips_preflight_when_disabled",  # 3.78s
    "test_provider_preflight.py::test_payload_loop_aborts_on_failed_preflight",     # 2.77s
    "test_operational_resilience.py::test_cancel_running_process_terminates_and_preserves_code",  # 3.14s
    "test_doctor.py::test_doctor_main",                                            # 2.02s
    "test_doctor.py::test_run_diagnostics",                                         # 2.01s
    "test_git_env_isolation.py::test_suite_passes_under_a_hook_shaped_environment",  # 1.71s
    # Legacy orchestrator: the LangGraph run_loop tests dominate the module; the
    # human_proxy_intercept authority tests beside them are effectively free.
    "test_orchestrator.py::test_orchestrator_run_loop_approved_immediately",
    "test_orchestrator.py::test_orchestrator_run_loop_humanize_neutral_by_default",
    "test_orchestrator.py::test_orchestrator_run_loop_exhausts_max_iterations",
    "test_orchestrator.py::test_orchestrator_run_loop_approved_no_exhaustion_marker",
    "test_orchestrator.py::test_orchestrator_run_loop_rejected_then_approved",
    "test_orchestrator.py::test_orchestrator_run_loop_humanize_stealth_with_command",
    "test_orchestrator.py::test_orchestrator_run_loop_humanize_disabled_via_config",
    "test_orchestrator.py::test_orchestrator_run_loop_rejected_with_approved_substring",
    # Governance recovery: 8 of 41 tests carry ~15s of the module's ~16s.
    "test_interrupted_governance_recovery.py::test_resume_does_not_credit_a_producer_for_another_resources_work",
    "test_interrupted_governance_recovery.py::test_interrupted_candidate_review_resumes_and_completes",
    "test_interrupted_governance_recovery.py::test_a_parked_candidate_is_never_recorded_as_an_accepted_implementation",
    "test_interrupted_governance_recovery.py::test_resume_after_an_interrupted_promotion_governs_the_fallback",
    "test_interrupted_governance_recovery.py::test_the_final_attempt_states_why_no_provider_follows_it",
    "test_interrupted_governance_recovery.py::test_route_stays_provisional_while_the_candidate_is_still_under_review",
    "test_interrupted_governance_recovery.py::test_provider_scratch_cannot_manufacture_a_fake_attempt",
    "test_interrupted_governance_recovery.py::test_the_real_attempt_directory_is_never_swept",
    # Verification view: 3 of 22 tests carry ~5.3s of the module's ~6.4s.
    "test_verification_view.py::test_no_worktree_is_leaked_after_a_governed_run",
    "test_verification_view.py::test_isolation_can_be_disabled_for_the_previous_in_place_behaviour",
    "test_verification_view.py::test_orchestrator_verifies_in_the_view_and_records_evidence",
}


def pytest_collection_modifyitems(items):
    """Apply the canonical tier and measured-runtime markers to every test."""
    for item in items:
        existing_tiers = {
            m.name for m in item.iter_markers() if m.name in {"unit", "integration", "acceptance"}
        }
        module_name = Path(str(item.fspath)).name
        if not existing_tiers:
            if module_name in _ACCEPTANCE_MODULES:
                item.add_marker(pytest.mark.acceptance)
            elif module_name in _INTEGRATION_MODULES:
                item.add_marker(pytest.mark.integration)
            else:
                item.add_marker(pytest.mark.unit)
        if item.get_closest_marker("slow"):
            continue
        # Strip any parametrisation so a parametrised test matches by its name.
        base_name = getattr(item, "originalname", None) or item.name.split("[")[0]
        if module_name in _SLOW_MODULES or f"{module_name}::{base_name}" in _SLOW_TESTS:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_git_contamination():
    """Remove a stale temp-root .git created by earlier agent/tool runs so that
    repository-discovery tests see a clean temp namespace."""
    tmp_git = Path(tempfile.gettempdir()) / ".git"
    if tmp_git.exists():
        try:
            shutil.rmtree(tmp_git)
        except OSError:
            pass
    yield


@pytest.fixture(autouse=True)
def _scrub_inherited_git_repository_selection(monkeypatch):
    """Removes inherited Git repository-selection variables for every test.

    Defense in depth only. The real fix is that test helpers and production
    code launch git through `src.control_plane.git_env`, which sanitizes the
    environment itself -- a fresh clone must be safe even with no conftest and
    no patched hook. Tests that deliberately prove that guarantee re-set these
    variables with `monkeypatch.setenv`, which wins over this fixture.
    """
    for name in GIT_REPOSITORY_SELECTION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def setup_test_environment(request, monkeypatch):
    """
    Ensures deterministic compiler discovery and fake agent availability
    for all unit, synthesis, and CLI tests across clean CI and local environments.
    """
    repo_root = Path(__file__).resolve().parents[1]
    fake_compiler = repo_root / "tests" / "fake_compiler.py"
    module_name = Path(str(request.node.fspath)).name

    # If HOWLFRAME_BIN is not set and howlframe is not on PATH, point to fake_compiler
    # -- except for the real compiler integration tests, which need the genuine
    # discovery contract to correctly skip/degrade when no real compiler exists.
    if module_name not in _REAL_COMPILER_INTEGRATION_MODULES:
        if not os.environ.get("HOWLFRAME_BIN") and not shutil.which("howlframe"):
            if fake_compiler.is_file():
                monkeypatch.setenv("HOWLFRAME_BIN", str(fake_compiler))

    # In automated test runs, set deterministic baseline mode unless live providers requested
    if not os.environ.get("HOWLPLANE_LIVE_PROVIDERS") and not os.environ.get("HOWLPLANE_SYNTHESIS_MODE"):
        monkeypatch.setenv("HOWLPLANE_SYNTHESIS_MODE", "deterministic_baseline")


@pytest.fixture
def orchestrator_factory():
    """Factory fixture providing isolated Orchestrator instances.

    Each test can call ``orchestrator_factory()`` to create a new ``Orchestrator``.
    The legacy orchestrator's MCP configuration is a real process boundary, not
    part of its unit-level approval-loop contract.  The factory therefore
    retains the normal configuration while removing MCP servers. Tests that
    verify MCP integration construct an orchestrator with an explicit fake or
    patch the client seam themselves. All created instances are shut down after
    the test completes.
    """
    from src.core.orchestrator import Orchestrator, load_config
    created = []

    def _make(*args, **kwargs):
        config = dict(load_config())
        config["active_mcps"] = []
        config["mcp_servers"] = {}
        with pytest.MonkeyPatch.context() as isolated:
            isolated.setattr("src.core.orchestrator.load_config", lambda: config)
            instance = Orchestrator(*args, **kwargs)
        created.append(instance)
        return instance

    yield _make

    # Teardown: shut down all created orchestrators
    for orchestrator in created:
        try:
            orchestrator.shutdown()
        except Exception:
            # Suppress shutdown errors to avoid test failures
            pass
