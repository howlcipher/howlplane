"""
test_route_evidence.py

Tests for truthful routing evidence persistence across failover and
three-state HowlFrame audit rendering.
"""

import json
from pathlib import Path
import pytest

from src.control_plane.launcher import _print_orchestration_summary
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
    RoutingDecision,
)
from src.control_plane.project_adapter import ProjectContext
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from src.control_plane.task_spec import TaskSpec
from tests._git_test_helpers import init_git_repo


def test_recompute_reviewers_persists_effective_and_initial_route(tmp_path):
    repo = init_git_repo(tmp_path / "route_repo", files={"README.md": "hello"})
    pool = ProviderPoolManager(operating_mode="connected")
    config = OrchestrationConfig(provider_pool=pool)
    orch = GovernedTaskOrchestrator(target_repo=repo, config=config)

    task = TaskSpec(
        task_id="TASK-ROUTE-01",
        repository="route_repo",
        objective="Refactor duplication",
        task_class="bug_fix",
    )

    _, routing, _, run_dir, _ = orch.prepare_task_plan(task)

    initial_route_file = run_dir / "initial_route.json"
    route_file = run_dir / "route.json"
    effective_route_file = run_dir / "effective_route.json"

    assert initial_route_file.exists()
    assert route_file.exists()
    assert not effective_route_file.exists()

    initial_agent = routing.selected_agent_id
    final_impl = "codex" if initial_agent != "codex" else "agy"

    orch._persist_effective_route(
        routing, task, final_impl, "SUPERSEDED_BY_FAILOVER", accepted=True
    )

    assert effective_route_file.exists()

    route_data = json.loads(route_file.read_text(encoding="utf-8"))
    eff_data = json.loads(effective_route_file.read_text(encoding="utf-8"))
    init_data = json.loads(initial_route_file.read_text(encoding="utf-8"))

    assert route_data["metadata"]["route_status"] == "SUPERSEDED_BY_FAILOVER"
    assert route_data["metadata"]["initial_route"]["selected_agent_id"] == initial_agent
    assert route_data["metadata"]["final_route"]["selected_agent_id"] == final_impl
    assert route_data["metadata"]["accepted_implementation_resource"] == final_impl
    assert route_data["metadata"]["reviewer_mapping_status"] == "CONFIRMED"

    assert eff_data["selected_agent_id"] == final_impl
    if eff_data["metadata"].get("review_diversity_achieved"):
        assert final_impl not in eff_data["metadata"]["reviewer_resource_mapping"].values()
    else:
        assert eff_data["metadata"].get("review_diversity_achieved") is False

    assert init_data["selected_agent_id"] == initial_agent


def test_unaccepted_route_never_claims_an_accepted_implementer(tmp_path):
    """A resource that is only *attempting* implementation has not been
    accepted, and the reviewer mapping chosen for it is provisional until it
    is. Recording it as final would repeat the failure it is meant to fix, in
    the opposite direction (HOWLFRAM-SLOPFIX-05)."""
    repo = init_git_repo(tmp_path / "route_repo_hop", files={"README.md": "hello"})
    pool = ProviderPoolManager(operating_mode="connected")
    orch = GovernedTaskOrchestrator(
        target_repo=repo, config=OrchestrationConfig(provider_pool=pool)
    )
    task = TaskSpec(
        task_id="TASK-ROUTE-02",
        repository="route_repo_hop",
        objective="Refactor duplication",
        task_class="bug_fix",
    )
    _, routing, _, run_dir, _ = orch.prepare_task_plan(task)
    initial_agent = routing.selected_agent_id
    attempted = "codex" if initial_agent != "codex" else "agy"

    orch._persist_effective_route(
        routing, task, attempted, "IMPLEMENTATION_FAILED", accepted=False
    )

    eff = json.loads((run_dir / "effective_route.json").read_text(encoding="utf-8"))
    init = json.loads((run_dir / "initial_route.json").read_text(encoding="utf-8"))

    # The last resource actually attempted is named, and named as attempted.
    assert eff["selected_agent_id"] == attempted
    assert eff["metadata"]["route_status"] == "IMPLEMENTATION_FAILED"
    assert eff["metadata"]["last_attempted_implementation_resource"] == attempted
    assert eff["metadata"]["accepted_implementation_resource"] is None
    assert eff["metadata"]["final_implementation_resource"] is None
    assert eff["metadata"]["reviewer_mapping_status"] == "PROVISIONAL"
    # And the initially routed provider is not left looking like the implementer.
    assert eff["metadata"]["initial_route"]["selected_agent_id"] == initial_agent
    assert init["selected_agent_id"] == initial_agent


def _render_summary(result: OrchestrationResult, capsys) -> str:
    ctx = ProjectContext(project_root=".", name="test_repo")
    task = TaskSpec(task_id=result.task_id, repository="test_repo", objective="Audit test")
    decision = RoutingDecision(
        selected_agent_id="codex",
        selected_agent_name="Codex",
        recommended_reviewers=[],
        reasoning_tier="tier_1",
        rationale="test",
    )
    _print_orchestration_summary(result, ctx, task, decision)
    return capsys.readouterr().out


@pytest.mark.parametrize(
    "audit_status,audit_match,expected_str,unexpected_str",
    [
        (None, True, "PASS / MATCH (shadow)", "MISMATCH"),
        (None, False, "MISMATCH", "NOT COMPUTED"),
        (None, None, "NOT COMPUTED", "MISMATCH"),
        ("PASS", None, "PASS", "MISMATCH"),
    ],
)
def test_howlframe_audit_status_rendering(
    audit_status, audit_match, expected_str, unexpected_str, capsys
):
    res = OrchestrationResult(
        task_id="TASK-HF-01",
        task_spec=TaskSpec(task_id="TASK-HF-01", repository="test_repo", objective="Audit test"),
        final_state="complete",
        exit_code=0,
        howlframe_audit_status=audit_status,
        howlframe_audit_match=audit_match,
    )
    out = _render_summary(res, capsys)
    assert f"HowlFrame:       {expected_str}" in out
    if unexpected_str:
        assert f"HowlFrame:       {unexpected_str}" not in out


def test_howlframe_persisted_audit_file_preferred(tmp_path, capsys):
    run_dir = tmp_path / "task_runs" / "TASK-HF-02"
    run_dir.mkdir(parents=True)
    (run_dir / "howlframe_audit.json").write_text(
        json.dumps({"status": "MATCH", "audit_status": "PASS"}),
        encoding="utf-8",
    )
    res = OrchestrationResult(
        task_id="TASK-HF-02",
        task_spec=TaskSpec(task_id="TASK-HF-02", repository="test_repo", objective="Audit test 2"),
        final_state="awaiting_human",
        exit_code=2,
        run_dir=run_dir,
        howlframe_audit_status=None,
        howlframe_audit_match=None,
    )
    out = _render_summary(res, capsys)
    assert "HowlFrame:       PASS" in out
    assert "MISMATCH" not in out
