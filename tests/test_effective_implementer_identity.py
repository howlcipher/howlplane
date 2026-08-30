#!/usr/bin/env python3
"""
tests/test_effective_implementer_identity.py

Deterministic regression tests for the single authoritative
effective-implementer identity (issues.md #16).

HOWLFRAM-BUG-52 finished with four durable answers to one question:
`task.yaml` said `actual_agent: codex` (the test-falsifier *reviewer*),
`effective_route.json` said `selected_agent_id: claude_code` beside
`selected_agent_name: "Antigravity CLI (agy)"`, and both
`accepted_implementation_resource` and `final_implementation_resource` were
`null`. Reviewer independence happened to stay correct only because it read a
local variable that never reached disk.

These tests pin the contract: one identity, written where implementation
settles, mirrored into `actual_agent`, agreed on by task metadata, routing
evidence, reviewer independence and the final summary -- while the full
per-attempt history stays intact and no review path can overwrite it.

Every test uses fake backends. No live provider quota is consumed.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.control_plane.agent_execution import (
    AgentExecutionResult,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from src.control_plane.agent_registry import AgentRegistry
from src.control_plane.atomic_io import safe_load_json
from src.control_plane.task_spec import TaskSpec
from tests.test_provider_failover import (
    _FakeBackendResolver,
    _edit_feature_to_true,
    _init_test_repo,
    _make_registry_three_providers,
    _make_task,
    _profile,
    _run_failover_task,
    _run_slopfix07r,
)


_EXHAUSTED = {"success": False, "stderr": "Error: quota exhausted"}


def _registry_four() -> AgentRegistry:
    """Four resources, four providers, so review can stay independent.

    Extends the shared three-provider fixture rather than restating it: the
    fourth resource exists only so an independent reviewer survives after
    implementation failover has consumed the others.
    """
    profiles = list(_make_registry_three_providers().list_resources())
    profiles.append(_profile("resource_d", "Resource D", "provider_w"))
    return AgentRegistry(profiles)


def _resolver(**behaviors: Any) -> _FakeBackendResolver:
    """Fake backends keyed by resource id, exhausted unless told otherwise."""
    return _FakeBackendResolver(dict(behaviors))


def _run_one_hop_failover(repo: Path):
    """The canonical one-hop failover run these tests all interrogate.

    resource_a is exhausted before it produces anything and resource_b does the
    work, so the resource that was routed and the resource that implemented are
    different -- the condition under which HOWLFRAM-BUG-52's durable views
    disagreed. Shared by every test that needs it so the setup exists once.
    """
    return _run_failover_task(
        repo,
        _resolver(
            resource_a=_EXHAUSTED,
            resource_b={"success": True, "side_effect": _edit_feature_to_true},
        ),
        registry=_registry_four(),
    )


def _run_dir(repo: Path, task_id: str) -> Path:
    return repo / ".task_runs" / task_id


def _identity_views(repo: Path, res) -> Dict[str, Any]:
    """Collects every durable place that answers 'who implemented this'."""
    run_dir = _run_dir(repo, res.task_spec.task_id)
    effective_path = run_dir / "effective_route.json"
    effective = safe_load_json(effective_path) if effective_path.is_file() else {}
    summary_path = run_dir / "summary.md"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    return {
        "task_spec_effective": res.task_spec.effective_implementer_resource_id,
        "task_spec_actual_agent": res.task_spec.actual_agent,
        "task_yaml_actual_agent": _task_yaml_field(run_dir, "actual_agent"),
        "task_yaml_effective": _task_yaml_field(
            run_dir, "effective_implementer_resource_id"
        ),
        "effective_route_selected_agent_id": effective.get("selected_agent_id"),
        "effective_route_selected_agent_name": effective.get("selected_agent_name"),
        "routing_metadata_effective": res.routing_decision.metadata.get(
            "effective_implementer_resource_id"
        ),
        "reviewer_mapping": res.routing_decision.metadata.get(
            "reviewer_resource_mapping", {}
        ),
        "summary": summary,
        "executing_provider": res.executing_provider,
    }


def _task_yaml_field(run_dir: Path, key: str) -> Optional[str]:
    """Reads one field back off the serialized task, not out of memory."""
    task_yaml = run_dir / "task.yaml"
    if not task_yaml.is_file():
        return None
    return TaskSpec.load_from_file(str(task_yaml)).to_dict().get(key)


def _assert_single_identity(views: Dict[str, Any], expected: str) -> None:
    """Every durable view names `expected`, and none contradicts another."""
    assert views["task_spec_effective"] == expected
    assert views["task_spec_actual_agent"] == expected
    assert views["task_yaml_actual_agent"] == expected
    assert views["task_yaml_effective"] == expected
    assert views["effective_route_selected_agent_id"] == expected
    # Reviewer independence is measured against the same value.
    assert expected not in views["reviewer_mapping"].values()
    if views["summary"]:
        assert f"**Implementing Agent:** {expected}" in views["summary"]


# ---------------------------------------------------------------------------
# 1. First implementation attempt succeeds
# ---------------------------------------------------------------------------

def test_first_attempt_success_names_one_implementer(tmp_path):
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _resolver(resource_a={"success": True, "side_effect": _edit_feature_to_true}),
        registry=_registry_four(),
    )

    assert res.executing_provider == "resource_a"
    _assert_single_identity(_identity_views(repo, res), "resource_a")


# ---------------------------------------------------------------------------
# 2. First attempt fails, second succeeds
# ---------------------------------------------------------------------------

def test_failover_names_the_resource_that_actually_produced_the_work(tmp_path):
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_one_hop_failover(repo)

    views = _identity_views(repo, res)
    _assert_single_identity(views, "resource_b")
    # The initially routed resource is still recorded, just not as the
    # implementer -- attempt history is preserved, not overwritten.
    assert views["routing_metadata_effective"] == "resource_b"
    attempts = sorted(
        p.name for p in (_run_dir(repo, res.task_spec.task_id)
                         / "implementation" / "attempts").iterdir()
    )
    assert any("resource_a" in name for name in attempts)
    assert any("resource_b" in name for name in attempts)


# ---------------------------------------------------------------------------
# 3. Attempts fail but a retained candidate exists
# ---------------------------------------------------------------------------

def test_promoted_retained_candidate_is_the_effective_implementer(tmp_path):
    """The producer of a promoted candidate is the effective implementer.

    Reuses the exact SLOPFIX-07R chain (transport failure, productive timeout,
    session limit) whose promotion behaviour is already pinned in
    tests/test_provider_failover.py. There, resource_b's timed-out-but-usable
    artifact is promoted while resource_c is the resource the failover chain
    actually ended on. Promotion credits the producer; it must not rewrite
    which attempt ran last, and both facts must be durably legible.
    """
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix07r(repo)

    views = _identity_views(repo, res)
    # The producer of the governed candidate, named once and consistently --
    # HOWLFRAM-BUG-52 left this null in every durable view.
    _assert_single_identity(views, "resource_b")
    assert views["effective_route_selected_agent_name"] == "Resource B"

    # History is preserved, not rewritten: resource_c is still the last
    # resource that actually ran, and all three attempts keep their records.
    metadata = res.routing_decision.metadata
    assert metadata["last_attempted_implementation_resource"] == "resource_c"
    assert metadata["effective_implementer_resource_id"] == "resource_b"
    assert [a["resource_id"] for a in res.implementation_attempts] == [
        "resource_a", "resource_b", "resource_c",
    ]


# ---------------------------------------------------------------------------
# 4. All attempts fail with no candidate
# ---------------------------------------------------------------------------

def test_no_candidate_claims_no_implementer(tmp_path):
    """A task where nothing usable was produced must not name an implementer."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            profile.resource_id: _EXHAUSTED
            for profile in _registry_four().list_resources()
        }),
        registry=_registry_four(),
        max_attempts=4,
    )

    assert res.final_state in ("failed", "blocked", "awaiting_human")
    views = _identity_views(repo, res)
    # An effective implementer is only ever set from a resource that produced
    # work. Nothing produced work here, so the field stays empty rather than
    # inheriting whichever provider happened to be attempted last.
    assert views["task_spec_effective"] is None


# ---------------------------------------------------------------------------
# 5. Serialization / resume round-trip
# ---------------------------------------------------------------------------

def test_effective_implementer_survives_serialization(tmp_path):
    spec = _make_task("TEST-IDENTITY-SERDE")
    spec.effective_implementer_resource_id = "resource_b"
    spec.actual_agent = "resource_b"
    spec.dispatch_resource_id = "resource_d"

    path = tmp_path / "task.yaml"
    spec.save_to_file(str(path))
    restored = TaskSpec.load_from_file(str(path))

    assert restored.effective_implementer_resource_id == "resource_b"
    assert restored.actual_agent == "resource_b"
    # The transient dispatch slot round-trips as data but never becomes the
    # answer to "who implemented this".
    assert restored.dispatch_target == "resource_d"
    assert restored.effective_implementer_resource_id != restored.dispatch_target


def test_legacy_task_yaml_without_the_field_still_loads(tmp_path):
    """Tasks serialized before #16 deserialize unaffected."""
    spec = _make_task("TEST-IDENTITY-LEGACY")
    spec.actual_agent = "resource_a"
    payload = spec.to_dict()
    payload.pop("effective_implementer_resource_id", None)
    payload.pop("dispatch_resource_id", None)

    restored = TaskSpec.from_dict(payload)
    assert restored.effective_implementer_resource_id is None
    # dispatch_target falls back, so existing dispatch call sites keep working.
    assert restored.dispatch_target == "resource_a"


# ---------------------------------------------------------------------------
# 6. Reviewer independence reads the same identity
# ---------------------------------------------------------------------------

def test_reviewer_independence_uses_the_effective_implementer(tmp_path):
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_one_hop_failover(repo)

    mapping = res.routing_decision.metadata["reviewer_resource_mapping"]
    assert mapping
    assert res.task_spec.effective_implementer_resource_id == "resource_b"
    assert "resource_b" not in mapping.values()
    assert res.routing_decision.metadata["review_diversity_achieved"] is True


# ---------------------------------------------------------------------------
# 7. A reviewer invocation must never rewrite the implementer
# ---------------------------------------------------------------------------

def test_reviewer_dispatch_does_not_overwrite_actual_agent():
    """The exact HOWLFRAM-BUG-52 mechanism, isolated.

    `invoke_reviewer_with_failover` tells each backend which candidate it
    represents. Writing that to `actual_agent` is what left task.yaml naming
    the test-falsifier reviewer as the implementing agent.
    """
    from src.control_plane.review_runner import invoke_reviewer_with_failover

    task = _make_task("TEST-IDENTITY-REVIEW-DISPATCH")
    task.effective_implementer_resource_id = "resource_b"
    task.actual_agent = "resource_b"

    seen: List[str] = []

    class _RecordingBackend:
        def __init__(self, resource_id: str) -> None:
            self.resource_id = resource_id

        def is_available(self) -> bool:
            return True

        def execute(self, task, cwd, role, prompt_override=None, **kwargs):
            # A dispatcher backend answers per-candidate off the dispatch slot.
            seen.append(task.dispatch_target)
            return AgentExecutionResult(
                agent_id=task.dispatch_target,
                role=role,
                command=task.dispatch_target,
                exit_code=0,
                stdout="findings: []\n",
                stderr="",
                duration_seconds=0.01,
                success=True,
            )

    winner, _result, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["resource_c", "resource_d"],
        task=task,
        cwd=".",
        prompt_override="review this",
        backend_lookup=_RecordingBackend,
    )

    assert winner == "resource_c"
    assert seen == ["resource_c"]
    assert attempts and attempts[0]["provider"] == "resource_c"
    # The durable identity is untouched by the review.
    assert task.actual_agent == "resource_b"
    assert task.effective_implementer_resource_id == "resource_b"


# ---------------------------------------------------------------------------
# 8. effective_route.json's name follows its id
# ---------------------------------------------------------------------------

def test_effective_route_name_follows_the_id(tmp_path):
    """BUG-52 wrote `claude_code` beside `"Antigravity CLI (agy)"`."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_one_hop_failover(repo)

    views = _identity_views(repo, res)
    assert views["effective_route_selected_agent_id"] == "resource_b"
    assert views["effective_route_selected_agent_name"] == "Resource B"
