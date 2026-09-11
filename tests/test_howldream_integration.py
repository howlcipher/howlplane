"""
test_howldream_integration.py

Verification tests for HowlPlane native HowlDream integration:
1. Exploration policy enforcement (NEVER, MANUAL, ALLOWED).
2. Circuit breaker limits (max_depth, max_dream_runs).
3. Graceful fallback when HowlDream is unavailable.
4. Negative authority boundary: HowlDream outputs NEVER authorize execution or pass executor checks.
5. Invariant evaluation with HowlFrame and deliberate prototyping with HowlCreate.
6. CLI explore and trace invocation completeness.
"""

import json
from pathlib import Path
import pytest
from unittest.mock import patch

from src.control_plane.executor import (
    ExecutionReceipt,
    ExecutorRegistry,
    ExecutorError,
    HowlChangeOpsExecutor,
)
from src.control_plane.howldream_runner import (
    HowlDreamRunner,
    ExplorationPolicy,
    ExplorationBudget,
    CircuitBreakers,
    AuthorityEscalationError,
    assert_no_execution_authority,
    DeterministicTestExplorationProvider,
)
from src.control_plane import launcher


def test_exploration_policy_never_skips_execution(tmp_path: Path):
    """Proves that policy NEVER halts exploration before launching engine."""
    runner = HowlDreamRunner(policy=ExplorationPolicy.NEVER)
    res = runner.dispatch_exploration("Test objective", repo_dir=tmp_path)
    assert res.status == "SKIPPED"
    assert "NEVER" in (res.error_message or "")
    assert len(res.candidates) == 0


def test_circuit_breakers_halt_recursive_dreaming(tmp_path: Path):
    """Proves that max_depth and max_dream_runs circuit breakers trigger fail-closed."""
    breakers = CircuitBreakers(max_depth=1, max_dream_runs=1)
    provider = DeterministicTestExplorationProvider()
    runner = HowlDreamRunner(
        policy=ExplorationPolicy.ALLOWED, breakers=breakers, provider=provider
    )

    # Trigger via depth limit
    res_depth = runner.dispatch_exploration(
        "Explore depth", repo_dir=tmp_path, current_depth=1
    )
    assert res_depth.status == "SKIPPED"
    assert res_depth.circuit_breaker_triggered is True
    assert "depth" in (res_depth.error_message or "")

    # Trigger via runs limit: 1 run executes, 2nd run blocked
    res_first = runner.dispatch_exploration(
        "Explore run 1", repo_dir=tmp_path, current_depth=0
    )
    assert res_first.status in ("SUCCESS", "NO_CANDIDATES")

    res_second = runner.dispatch_exploration(
        "Explore run 2", repo_dir=tmp_path, current_depth=0
    )
    assert res_second.status == "SKIPPED"
    assert res_second.circuit_breaker_triggered is True
    assert "dream run count" in (res_second.error_message or "")


def test_graceful_fallback_when_howldream_unavailable(tmp_path: Path):
    """Proves that missing HowlDream engine yields UNAVAILABLE without crashing HowlPlane."""
    runner = HowlDreamRunner(policy=ExplorationPolicy.ALLOWED)
    with patch.object(HowlDreamRunner, "is_howldream_available", return_value=False):
        res = runner.dispatch_exploration("Explore fallback", repo_dir=tmp_path)
        assert res.status == "UNAVAILABLE"
        assert "not available" in (res.error_message or "")


def test_negative_authority_guard_rejects_executable_and_approvals():
    """Proves that speculative payloads claiming execution authority fail closed."""
    # 1. Authority claiming executable=True
    malicious_payload_1 = {
        "candidate": {"title": "Exploit", "status": "EXPLORED"},
        "authority": {"type": "ADVISORY", "executable": True},
    }
    with pytest.raises(AuthorityEscalationError, match="executable=True"):
        assert_no_execution_authority(malicious_payload_1)

    # 2. Authority claiming executive type
    malicious_payload_2 = {
        "candidate": {"title": "Exploit", "status": "EXPLORED"},
        "authority": {"type": "EXECUTIVE", "executable": False},
    }
    with pytest.raises(AuthorityEscalationError, match="authority type EXECUTIVE"):
        assert_no_execution_authority(malicious_payload_2)

    # 3. Payload claiming self-approval
    malicious_payload_3 = {
        "candidate": {"title": "Exploit", "approved": True},
        "authority": {"type": "ADVISORY", "executable": False},
    }
    with pytest.raises(AuthorityEscalationError, match="self-approval"):
        assert_no_execution_authority(malicious_payload_3)


def test_negative_authority_executor_boundary(tmp_path: Path):
    """
    Proves that HowlDream outputs cannot be registered as authority executors,
    and execution receipts claiming 'howldream' or 'howlcreate' fail closed.
    """
    # 1. Attempting to register howldream as an executor fails
    class MockDreamExecutor:
        name = "howldream"

    with pytest.raises(ExecutorError, match="Speculative exploration system"):
        ExecutorRegistry.register(MockDreamExecutor())  # type: ignore

    # 2. Attempting to verify an ExecutionReceipt claiming howldream is rejected
    receipt = ExecutionReceipt(
        task_id="TASK-101",
        executor="howldream",
        executor_version="0.4.0",
        decision_id="DEC-01",
        action_type="deploy",
        repository=tmp_path.name,
        commit_sha="abcdef123456",
        status="success",
        executed_at="2026-09-11T12:00:00Z",
    )
    hco_exec = HowlChangeOpsExecutor()
    is_valid, reason = hco_exec.verify_receipt(
        receipt=receipt,
        expected_action="deploy",
        expected_repo=tmp_path.name,
    )
    assert is_valid is False
    assert "zero execution authority" in (reason or "")


def test_end_to_end_exploration_howlframe_howlcreate_pipeline(tmp_path: Path):
    """
    Executes an end-to-end exploration dispatch:
    HowlPlane -> HowlDream (explore) -> HowlFrame (assessment) -> HowlCreate (deliberate prototype).
    """
    provider = DeterministicTestExplorationProvider()
    runner = HowlDreamRunner(policy=ExplorationPolicy.ALLOWED, provider=provider)
    budget = ExplorationBudget(max_candidates=2, max_trials=1, max_tokens=1000)

    res = runner.dispatch_exploration(
        objective="Investigate intermittent deployment timeout in Howl pipeline",
        budget=budget,
        repo_dir=tmp_path,
        run_howlframe=True,
        run_howlcreate=True,
    )

    assert res.status == "SUCCESS"
    assert len(res.candidates) > 0
    assert len(res.assessments) == len(res.candidates)
    assert res.lineage_trace is not None

    # Verify that every assessment is non-executable advisory
    for assess in res.assessments:
        assert assess.get("authority", {}).get("executable") is False
        assert assess.get("authority", {}).get("type") == "ADVISORY"

    # Verify that any developed candidate has zero execution authority
    for dev in res.development_results:
        assert dev.get("execution_authority") == "NONE"
        assert dev.get("authority", {}).get("executable") is False


def test_cli_explore_and_trace_subcommands(tmp_path: Path, capsys, monkeypatch):
    """Proves that explore and trace subcommands function via launcher CLI."""
    monkeypatch.setenv("HOWLDREAM_PROVIDER", "test")

    # Test explore help
    with pytest.raises(SystemExit) as exc:
        launcher.main(["explore", "--help"])
    assert exc.value.code == 0
    capsys.readouterr()

    # Test trace help
    with pytest.raises(SystemExit) as exc:
        launcher.main(["trace", "--help"])
    assert exc.value.code == 0
    capsys.readouterr()

    from tests._git_test_helpers import init_git_repo
    init_git_repo(tmp_path, files={"README.md": "# Test\n"})

    # Test explore execution with JSON flag
    exit_code = launcher.main(
        [
            "explore",
            "Identify architectural bottlenecks in queue ingestion",
            "-R",
            str(tmp_path),
            "--budget-candidates",
            "1",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0, f"stdout: {captured.out}\nstderr: {captured.err}"
    data = json.loads(captured.out)
    assert data["status"] in ("SUCCESS", "NO_CANDIDATES")
    assert "candidates" in data

    if data["candidates"]:
        cand_id = data["candidates"][0]["candidate_id"]
        trace_code = launcher.main(["trace", cand_id, "-R", str(tmp_path), "--json"])
        trace_cap = capsys.readouterr()
        assert trace_code == 0, f"trace stdout: {trace_cap.out}\nstderr: {trace_cap.err}"
        trace_data = json.loads(trace_cap.out)
        assert len(trace_data) > 0


def test_resolve_candidate_evaluator_bc_no_workstation_paths():
    """Proves that resolve_candidate_evaluator_bc does not contain workstation paths."""
    import inspect

    src = inspect.getsource(HowlDreamRunner.resolve_candidate_evaluator_bc)
    assert "tallgeese" not in src
    assert "/run/media" not in src
    assert "/var/home" not in src


def test_exploration_policies_suggest_and_automatic(tmp_path: Path):
    """Proves that SUGGEST and AUTOMATIC_WITH_BUDGET policies operate within bounds."""
    provider = DeterministicTestExplorationProvider()

    # SUGGEST suggests exploration without automatically dispatching engine
    runner_suggest = HowlDreamRunner(
        policy=ExplorationPolicy.SUGGEST, provider=provider
    )
    res_suggest = runner_suggest.dispatch_exploration(
        "Suggest optimizations", repo_dir=tmp_path
    )
    assert res_suggest.status == "SKIPPED"
    assert "SUGGEST" in (res_suggest.error_message or "")

    # AUTOMATIC_WITH_BUDGET executes within bounds
    runner_auto = HowlDreamRunner(
        policy=ExplorationPolicy.AUTOMATIC_WITH_BUDGET, provider=provider
    )
    budget = ExplorationBudget(max_candidates=1, max_trials=1, max_tokens=500)
    res_auto = runner_auto.dispatch_exploration(
        "Auto with budget",
        budget=budget,
        repo_dir=tmp_path,
        run_howlframe=True,
        run_howlcreate=True,
    )
    assert res_auto.status == "SUCCESS"
    assert len(res_auto.candidates) > 0

    # AUTOMATIC_WITH_BUDGET skips when budget exceeds safe ceiling
    excessive_budget = ExplorationBudget(
        max_candidates=15, max_trials=1, max_tokens=5000
    )
    res_excessive = runner_auto.dispatch_exploration(
        "Auto with excessive budget",
        budget=excessive_budget,
        repo_dir=tmp_path,
    )
    assert res_excessive.status == "SKIPPED"
    assert "budget ceiling" in (res_excessive.error_message or "")


def test_trace_missing_node_and_text_output(tmp_path: Path, capsys, monkeypatch):
    """Proves that cmd_trace handles missing nodes gracefully and supports text output."""
    monkeypatch.setenv("HOWLDREAM_PROVIDER", "test")
    from tests._git_test_helpers import init_git_repo

    init_git_repo(tmp_path, files={"README.md": "# Test\n"})

    launcher.main(
        [
            "explore",
            "Lineage test objective",
            "-R",
            str(tmp_path),
            "--budget-candidates",
            "1",
            "--json",
        ]
    )
    capsys.readouterr()

    # Query nonexistent node
    exit_code = launcher.main(["trace", "nonexistent-node-id", "-R", str(tmp_path)])
    cap = capsys.readouterr()
    assert exit_code == 1
    assert "not found in lineage DAGs" in cap.out

    # Query existing node in text mode
    for env in tmp_path.glob("**/exploration_envelope.json"):
        d = json.loads(env.read_text())
        if d.get("candidates"):
            cid = d["candidates"][0]["candidate_id"]
            code = launcher.main(["trace", cid, "-R", str(tmp_path)])
            out = capsys.readouterr().out
            assert code == 0
            assert f"Lineage trace for '{cid}'" in out
            break
