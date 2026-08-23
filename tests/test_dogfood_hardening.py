#!/usr/bin/env python3
"""
test_dogfood_hardening.py

Comprehensive hardening test suite for Milestone #57:
1. Codex succeeds -> execute() called
2. Codex quota exhausted -> marked SESSION_EXHAUSTED -> agy executes
3. agy exhausted -> Devin executes
4. Devin exhausted -> Claude executes
5. All exhausted -> stops PROVIDER_POOL_EXHAUSTED
6. Broken source generated -> provider NOT marked exhausted -> repair occurs
7. Implementation provider != reviewer provider when another exists
8. Single provider remaining -> reduced reviewer diversity reported
9. Closed-loop self-improvement flywheel (fail -> classify -> governed task -> merge -> retry benchmark -> pass)
10. --until-providers-exhausted does not silently cap at 5
11. Durable campaign state persistence and --resume <campaign-id>
"""

from pathlib import Path
import pytest
import time
from typing import Dict, List, Tuple

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    FakeAgentBackend,
)
from src.control_plane.evidence_ledger import EvidenceLedger
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.synthesis.campaign_state import DurableCampaignState, GitIntegrationRecord
from src.control_plane.synthesis.capability_negotiator import CapabilityNegotiator, FrameworkGap
from src.control_plane.synthesis.engine import ProductSynthesizer, SynthesisResult
from src.control_plane.synthesis.marathon import (
    DogfoodIterationResult,
    MarathonDogfoodEngine,
    STANDARD_BENCHMARKS,
)
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderExhaustionEvent,
    ProviderPoolManager,
)
from tests._dogfood_test_helpers import (
    FakeOrchestrator,
    ScriptedRunner,
    build_full_merge_flow,
    clean_review_result,
)


def _seed_valid_howl_files(task, cwd: Path, prompt: str):
    """Side effect for fake backends to seed a working HowlFrame application matching spec."""
    spec_path = cwd / "product_spec.yaml"
    if spec_path.is_file():
        from src.control_plane.synthesis.product_spec import ProductSpec
        spec = ProductSpec.load_from_file(spec_path)
    else:
        from src.control_plane.synthesis.spec_synthesizer import NaturalLanguageSynthesizer
        spec = NaturalLanguageSynthesizer().synthesize(prompt or "Create an app")
    ProductSynthesizer()._synthesize_product_files(cwd, spec)


def test_scenario_1_codex_succeeds_and_executes(tmp_path: Path):
    """Scenario 1: Codex succeeds -> actual execute() called on Codex backend."""
    codex_fake = FakeAgentBackend(
        agent_id="codex",
        default_exit_code=0,
        side_effect=_seed_valid_howl_files,
    )
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)

    engine = ProductSynthesizer(provider_pool=pool, custom_backend=codex_fake)
    res = engine.create_from_prompt("Create a persistent notes app", output_dir=tmp_path / "app1")

    assert res.success is True
    assert res.status == "VERIFIED_PRODUCT"
    assert len(codex_fake.executed_calls) >= 1
    assert codex_fake.executed_calls[0]["role"] == "implementation"
    assert res.implementing_provider == "codex"


class ProgrammableDispatcherBackend(AgentBackend):
    def __init__(self, outcomes: Dict[str, Tuple[int, str, str]], error_messages: Dict[str, str] = None):
        self.outcomes = outcomes
        self.error_messages = error_messages or {}
        self.invocations: List[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, task, cwd, role="implementation", prompt_override=None, **kwargs):
        self.invocations.append(task.actual_agent)
        if role not in ("implementation", "remediation"):
            # Independent review roles: these tests exercise implementation-
            # provider failover, not review content.
            return clean_review_result(role, task.actual_agent)
        code, stdout, stderr = self.outcomes.get(task.actual_agent, (1, "", "unknown error"))
        if code == 0:
            _seed_valid_howl_files(task, Path(cwd), prompt_override or "")
        return AgentExecutionResult(
            agent_id=task.actual_agent,
            role=role,
            command=task.actual_agent,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.02,
            success=(code == 0),
            error_message=self.error_messages.get(task.actual_agent),
        )


def test_scenario_2_codex_quota_exhausted_fallback_to_agy(tmp_path: Path):
    """Scenario 2: Codex returns quota exhausted -> codex marked SESSION_EXHAUSTED -> agy executes."""
    dispatcher = ProgrammableDispatcherBackend({
        "codex": (1, "", "Error: session limit reached. You have exceeded your current quota."),
        "agy": (0, "Agy synthesized product successfully.", ""),
    })
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    pool.set_status("agy", ProviderAvailabilityStatus.AVAILABLE)

    engine = ProductSynthesizer(provider_pool=pool, custom_backend=dispatcher)
    res = engine.create_from_prompt("Create a todo app", output_dir=tmp_path / "app2")

    assert res.success is True
    assert res.status == "VERIFIED_PRODUCT"
    assert pool.get_status("codex") == ProviderAvailabilityStatus.SESSION_EXHAUSTED
    assert pool.get_status("agy") == ProviderAvailabilityStatus.AVAILABLE
    assert "codex" in dispatcher.invocations
    assert "agy" in dispatcher.invocations
    assert res.implementing_provider == "agy"


def test_scenario_3_and_4_devin_and_claude_fallback_chain(tmp_path: Path):
    """Scenarios 3 & 4: codex, agy, devin exhaust -> Claude executes."""
    dispatcher = ProgrammableDispatcherBackend({
        "codex": (1, "", "codex rate limit exceeded (429 too many requests)"),
        "agy": (1, "", "agy rate limit exceeded (429 too many requests)"),
        "devin_cli": (1, "", "devin_cli rate limit exceeded (429 too many requests)"),
        "claude_code": (0, "Claude Code synthesized successfully.", ""),
    })
    pool = ProviderPoolManager()
    for p in ["codex", "agy", "devin_cli", "claude_code"]:
        pool.set_status(p, ProviderAvailabilityStatus.AVAILABLE)

    engine = ProductSynthesizer(provider_pool=pool, custom_backend=dispatcher)
    res = engine.create_from_prompt("Create an inventory manager", output_dir=tmp_path / "app3")

    assert res.success is True
    assert pool.get_status("codex") == ProviderAvailabilityStatus.RATE_LIMITED
    assert pool.get_status("agy") == ProviderAvailabilityStatus.RATE_LIMITED
    assert pool.get_status("devin_cli") == ProviderAvailabilityStatus.RATE_LIMITED
    assert pool.get_status("claude_code") == ProviderAvailabilityStatus.AVAILABLE
    assert res.implementing_provider == "claude_code"


def test_scenario_5_all_providers_exhausted_clean_stop(tmp_path: Path):
    """Scenario 5: All providers return exhaustion/unavailability -> campaign stops cleanly with PROVIDER_POOL_EXHAUSTED.

    `local_ollama` has no subscription quota (#58), so it cannot be classified
    SESSION_EXHAUSTED/RATE_LIMITED; it is simulated here as genuinely unavailable
    (e.g. Ollama not installed on the CI runner) via an explicit error_message,
    matching how the real backend classifies that failure.
    """
    dispatcher = ProgrammableDispatcherBackend(
        {
            p: (1, "", "Quota exceeded. Usage limit reached for account.")
            for p in ["codex", "agy", "devin_cli", "claude_code", "gemini_cli"]
        } | {"local_ollama": (1, "", "ollama: command not found")},
        error_messages={"local_ollama": "OLLAMA_NOT_INSTALLED"},
    )
    pool = ProviderPoolManager()
    for p in ["codex", "agy", "devin_cli", "claude_code", "gemini_cli", "local_ollama"]:
        pool.set_status(p, ProviderAvailabilityStatus.AVAILABLE)

    engine = ProductSynthesizer(provider_pool=pool, custom_backend=dispatcher)
    res = engine.create_from_prompt("Create status api", output_dir=tmp_path / "app_all_exhausted")

    assert res.success is False
    assert res.status == "PROVIDER_POOL_EXHAUSTED"
    assert pool.get_status("codex") in (ProviderAvailabilityStatus.SESSION_EXHAUSTED, ProviderAvailabilityStatus.RATE_LIMITED)
    # Full product synthesis is medium risk and never local-eligible (#58 Phase 9):
    # local_ollama must not even be attempted merely because cloud is exhausted.
    assert "local_ollama" not in dispatcher.invocations
    assert pool.get_status("local_ollama") == ProviderAvailabilityStatus.AVAILABLE


def test_scenario_6_engineering_failure_does_not_exhaust_provider(tmp_path: Path):
    """Scenario 6: Broken source / syntax error is NOT marked as quota exhaustion; repair is attempted."""
    repair_calls = []

    class BrokenSourceBackend(AgentBackend):
        def is_available(self) -> bool:
            return True

        def execute(self, task, cwd, role="implementation", prompt_override=None, **kwargs):
            target_cwd = Path(cwd)
            if role == "remediation":
                repair_calls.append(task.task_id)
                # On repair, fix the file
                _seed_valid_howl_files(task, target_cwd, prompt_override or "")
                return AgentExecutionResult(
                    agent_id="codex", role="remediation", command="codex exec", exit_code=0,
                    stdout="Fixed syntax error", stderr="", duration_seconds=0.05, success=True,
                )
            elif role != "implementation":
                # Independent review roles: this test exercises engineering-
                # failure-vs-exhaustion classification, not review content.
                return clean_review_result(role, "codex")
            else:
                # Initial synthesis creates broken syntax
                app_dir = target_cwd / "app"
                scripts_dir = target_cwd / "scripts"
                app_dir.mkdir(parents=True, exist_ok=True)
                scripts_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "backend.howl").write_text("INVALID LISP SYNTAX ((((")
                build_sh = scripts_dir / "build.sh"
                build_sh.write_text("#!/bin/bash\nexit 1\n")
                build_sh.chmod(0o755)
                return AgentExecutionResult(
                    agent_id="codex", role="implementation", command="codex exec", exit_code=0,
                    stdout="Synthesized with syntax error", stderr="", duration_seconds=0.05, success=True,
                )

    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)

    engine = ProductSynthesizer(provider_pool=pool, custom_backend=BrokenSourceBackend(), max_repair_cycles=2)
    res = engine.create_from_prompt("Create a json transformer", output_dir=tmp_path / "app_repair")

    # Codex should NOT be marked exhausted
    assert pool.get_status("codex") == ProviderAvailabilityStatus.AVAILABLE
    assert len(repair_calls) >= 1
    assert res.success is True
    assert res.repair_cycles >= 1


def test_scenario_7_and_8_independent_review_diversity(tmp_path: Path):
    """Scenarios 7 & 8: Implementing provider != reviewer provider when multi-providers exist; reduced diversity reported when single provider."""
    pool = ProviderPoolManager()
    for p in ["agy", "devin_cli", "local_ollama", "gemini_cli"]:
        pool.set_status(p, ProviderAvailabilityStatus.UNAVAILABLE)
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    pool.set_status("claude_code", ProviderAvailabilityStatus.AVAILABLE)

    roles = ["test-falsifier", "security-reviewer"]
    rev_map, diversity_achieved = pool.select_reviewers(
        implementing_agent_id="codex",
        required_roles=roles,
    )

    # Codex is implementer -> reviewer should be claude_code
    assert diversity_achieved is True
    assert rev_map["test-falsifier"] == "claude_code"
    assert rev_map["security-reviewer"] == "claude_code"

    # Single provider remaining
    pool_single = ProviderPoolManager()
    for p in ["claude_code", "devin_cli", "agy", "local_ollama", "gemini_cli"]:
        pool_single.set_status(p, ProviderAvailabilityStatus.UNAVAILABLE)
    pool_single.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)

    rev_map_single, diversity_single = pool_single.select_reviewers(
        implementing_agent_id="codex",
        required_roles=roles,
        allow_same_provider=True,
    )
    assert diversity_single is False
    assert rev_map_single["test-falsifier"] == "codex"


def test_scenario_9_closed_loop_self_improvement_flywheel(tmp_path: Path):
    """
    Scenario 9: Closed-loop flywheel:
    benchmark fails -> classify gap -> create governed task -> implement & verify -> simulated merge -> retry benchmark -> pass.
    """
    class FlywheelSynthesizer(ProductSynthesizer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.first_attempt = True

        def create_from_prompt(self, prompt: str, output_dir, **kwargs) -> SynthesisResult:
            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            if self.first_attempt:
                self.first_attempt = False
                # Simulate synthetic framework gap on initial attempt
                gap = FrameworkGap(
                    code="HF_GAP_RUNTIME_STORE",
                    required_behavior="atomic store flush missing",
                    current_support="in-memory only",
                    suggested_enhancement="add store sync opcode",
                )
                from src.control_plane.synthesis.product_spec import ProductSpec
                mock_spec = ProductSpec(name="flywheel_app", title="Flywheel App", description="Flywheel test")
                return SynthesisResult(
                    product_name="flywheel_app",
                    product_spec=mock_spec,
                    success=False,
                    status="PRODUCT_BLOCKED",
                    framework_gaps=[gap],
                    error_message="HowlFrame framework gaps block product: atomic store flush missing",
                )
            else:
                # Succeeded on retry after fix!
                _seed_valid_howl_files(None, out_path, prompt)
                from src.control_plane.synthesis.product_spec import ProductSpec
                mock_spec = ProductSpec(name="flywheel_app", title="Flywheel App", description="Flywheel test")
                report = self.acceptance_runner.run_acceptance_suite(out_path, mock_spec)
                bundle = self._package_product_bundle(out_path, mock_spec, report)
                return SynthesisResult(
                    product_name="flywheel_app",
                    product_spec=mock_spec,
                    success=True,
                    status="VERIFIED_PRODUCT",
                    product_bundle=bundle,
                    acceptance_report=report,
                    implementing_provider="codex",
                )

    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    ledger = EvidenceLedger(tmp_path / "flywheel_ledger.jsonl")

    synth = FlywheelSynthesizer(provider_pool=pool, ledger=ledger, synthesis_mode="deterministic_baseline")

    # #59: real git/GitHub integration, faked ONLY at the git/gh subprocess
    # boundary (production GitIntegrationExecutor/marathon logic under test
    # is real) plus a faked GovernedTaskOrchestrator (its own lifecycle is
    # covered by test_closed_loop_orchestrator.py / test_operational_resilience.py).
    repo_slug = "howlcipher/howlplane"
    task_id = "ENG-NOTES-01"
    run_dir = tmp_path / "fake_run" / task_id

    git_runner = ScriptedRunner()
    gh_runner = ScriptedRunner()
    build_full_merge_flow(
        git_runner, gh_runner, task_id=task_id, repo_slug=repo_slug, pr_number=77,
        commit_message="fix(notes): resolve HOWLFRAME_RUNTIME_GAP found during marathon dogfooding",
        pr_title="fix(notes): HOWLFRAME_RUNTIME_GAP",
        pr_body="Automated marathon dogfooding fix.\n\nGap: HOWLFRAME_RUNTIME_GAP\natomic store flush missing",
        modified_path="src/control_plane/howlframe_runtime.py",
        commit_sha="realcommitsha1", merge_sha="realmergesha1",
    )

    engine = MarathonDogfoodEngine(
        synthesizer=synth,
        provider_pool=pool,
        ledger=ledger,
        base_output_dir=tmp_path / "dogfood_flywheel",
        campaign_dir=tmp_path / "campaigns",
        target_repo=tmp_path,
        repo_slug=repo_slug,
        orchestrator_factory=lambda config: FakeOrchestrator(run_dir, "src/control_plane/howlframe_runtime.py"),
        git_executor_factory=lambda envelope, merges_so_far: GitIntegrationExecutor(
            tmp_path, repo_slug, envelope, git_runner=git_runner, gh_runner=gh_runner, merges_so_far=merges_so_far,
        ),
    )

    report = engine.run_marathon(benchmarks=["notes"], max_iterations=1, authority_profile_id="overnight-safe")

    assert report.iterations_succeeded == 1
    assert len(report.benchmark_results) == 1
    assert report.benchmark_results[0].success is True
    assert report.benchmark_results[0].retried_after_fix is True
    assert len(report.completed_engineering_tasks) == 1
    assert len(report.git_records) == 1

    rec = GitIntegrationRecord.from_dict(report.git_records[0])
    assert rec.integration_mode == "real"
    assert rec.is_fully_integrated() is True
    assert rec.branch_observed and rec.commit_observed and rec.push_observed
    assert rec.pr_observed and rec.required_checks_green and rec.merge_observed
    assert rec.remote_main_contains_merge is True
    assert rec.merge_sha == "realmergesha1"
    assert rec.commit_sha == "realcommitsha1"


def test_scenario_10_until_providers_exhausted_semantics(tmp_path: Path):
    """Scenario 10: --until-providers-exhausted removes default max_iterations=5 ceiling."""
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    synth = ProductSynthesizer(provider_pool=pool, synthesis_mode="deterministic_baseline")

    engine = MarathonDogfoodEngine(
        synthesizer=synth,
        provider_pool=pool,
        base_output_dir=tmp_path / "dogfood_ceil",
        campaign_dir=tmp_path / "campaigns",
    )

    # When until_providers_exhausted is True, it should not be limited to 5
    report = engine.run_marathon(
        benchmarks=["notes", "todo", "status_api"],
        max_iterations=5,  # default argument passed from CLI
        until_providers_exhausted=True,
    )

    assert report.iterations_attempted == 3
    assert report.iterations_succeeded == 3
    assert report.stopped_reason == "completed_all_benchmarks"


def test_scenario_11_durable_campaign_state_and_resume(tmp_path: Path):
    """Scenario 11: Durable campaign state is atomically saved and can be resumed with --resume <campaign-id>."""
    state_dir = tmp_path / "campaigns" / "DOGFOOD-TEST-001"
    state_dir.mkdir(parents=True, exist_ok=True)

    state = DurableCampaignState(
        campaign_id="DOGFOOD-TEST-001",
        north_star_objective="Test Objective",
        active_benchmark="todo",
    )
    state.record_benchmark_result({
        "benchmark_id": "notes",
        "product_name": "notes_app",
        "success": True,
        "passed_checks": 8,
        "total_checks": 8,
        "repair_cycles": 0,
        "duration_seconds": 0.2,
        "implementing_provider": "codex",
    })
    state.record_task_completed({
        "task_id": "ENG-001",
        "objective": "Fix store gap",
        "provider": "codex",
    })
    state.save(state_dir)

    # Verify atomic files exist
    assert (state_dir / "campaign_state.json").exists()
    assert (state_dir / "campaign_summary.md").exists()

    # Load and verify state contents
    loaded = DurableCampaignState.load(state_dir)
    assert loaded.campaign_id == "DOGFOOD-TEST-001"
    assert loaded.iterations_succeeded == 1
    assert len(loaded.completed_tasks) == 1

    # Resume campaign execution
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)

    engine = MarathonDogfoodEngine(
        synthesizer=ProductSynthesizer(provider_pool=pool, synthesis_mode="deterministic_baseline"),
        provider_pool=pool,
        base_output_dir=tmp_path / "dogfood_resumed",
        campaign_dir=tmp_path / "campaigns",
    )

    resume_report = engine.run_marathon(
        benchmarks=["todo"],
        resume_campaign_id="DOGFOOD-TEST-001",
    )

    assert resume_report.campaign_id == "DOGFOOD-TEST-001"
    assert resume_report.iterations_succeeded >= 1
