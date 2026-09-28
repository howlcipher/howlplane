#!/usr/bin/env python3
"""
tests/test_factory_mission_flow.py

Continuous mission flow: Factory-native owner decisions, content-bound root
mission authorization, Factory-recorded successor provenance, and the bridge
from an orchestration human boundary back to the same WorkItem.

Every scenario here is synthetic. No provider is invoked, and no real mission
(Grocery Optimizer, Howl, HowlFrame, HowlPlane) is read or executed.
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane.factory.dispatcher import DispatchOutcome
from howlplane.control_plane.factory.mission_authorization import (
    AUTHORIZATION_ORIGIN_OWNER,
    AUTHORIZATION_ORIGIN_SUCCESSOR,
    CONTENT_CHANGED,
    SUCCESSOR_DIGEST_MISMATCH,
    SUCCESSOR_PARENT_NOT_AUTHORIZED,
    SUCCESSOR_PARENT_NOT_SHIPPED,
    TRUST_AUTHORIZED,
    TRUST_SUCCESSOR,
    UNTRUSTED_ROOT,
    MissionAuthorizationStore,
    SuccessorProvenanceStore,
    compute_mission_digest,
    evaluate_mission_trust,
    inspect_successor_missions,
    mission_number,
)
from howlplane.control_plane.factory.owner_command import (
    OwnerDecisionError,
    apply_owner_decision,
    describe_parked_orchestration,
    parked_reason_and_action,
)
from howlplane.control_plane.factory.owner_decision import OwnerDecisionStore
from howlplane.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
)
from tests._factory_test_helpers import RecordingDispatcher, make_supervisor

PRODUCT = "howlcipher/grocery-optimizer"
OTHER = "howlcipher/howlframe"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class MissionWorld:
    """A synthetic repository checkout plus a Factory supervisor discovering it."""

    def __init__(self, tmp_path: Path, repository: str = PRODUCT):
        self.repository = repository
        self.root = tmp_path / "checkout"
        self.root.mkdir()
        self.state = tmp_path / "state"
        self.missions = {}
        self.inspected = []
        self.successors_for = {}
        self.supervisor, self.clock, _ = make_supervisor(
            tmp_path, discovery=self.discover, state_dir=self.state,
        )
        self.supervisor.policy = self.supervisor.policy.__class__(product_repository=PRODUCT)
        self.auth_store = MissionAuthorizationStore(self.state / "mission_authorizations")
        self.prov_store = SuccessorProvenanceStore(self.state / "successor_provenance")
        self.decisions = OwnerDecisionStore(self.state / "owner_decisions")
        self.supervisor.mission_authorization_store = self.auth_store
        self.supervisor.successor_provenance_store = self.prov_store
        self.supervisor.successor_inspector = self.inspect
        self.supervisor.dispatcher = RecordingDispatcher([])

    # -- discovery / inspection -------------------------------------------
    def put_mission(self, rel: str, text: str) -> None:
        _write(self.root, rel, text)
        self.missions = {rel: text}

    def discover(self):
        evidence = []
        for rel in self.missions:
            path = self.root / rel
            evidence.append({
                "origin": "owner_direction",
                "repository": self.repository,
                "title": Path(rel).stem,
                "description": path.read_text(encoding="utf-8"),
                "identity_keys": [rel],
                "evidence_refs": [rel],
                "evidence_fingerprints": [f"mission:{rel}"],
                "kind": "mission",
                "trusted_provenance": False,
                "mission_path": rel,
                "mission_digest": compute_mission_digest(path),
            })
        return evidence

    def inspect(self, item, git_record):
        self.inspected.append((item.work_item_id, git_record["merge_sha"]))
        return self.successors_for.get(item.mission_path, [])

    # -- helpers ------------------------------------------------------------
    def item_for(self, rel: str) -> WorkItem:
        for item in self.supervisor.work_item_store.list_all():
            if item.mission_path == rel:
                return item
        raise AssertionError(f"no WorkItem for {rel}")

    def approve(self, item: WorkItem, **kwargs):
        return apply_owner_decision(
            work_item_store=self.supervisor.work_item_store,
            owner_decision_store=self.decisions,
            mission_authorization_store=self.auth_store,
            work_item_id=item.work_item_id,
            decision=kwargs.pop("decision", "approved"),
            mission_roots=[self.root],
            **kwargs,
        )

    def ship_next(self, successors=None, merged=True):
        """Queue a successful governed dispatch that merged, adding `successors`."""
        self.supervisor.dispatcher.outcomes.append(DispatchOutcome(
            success=True,
            work_item_id="placeholder",
            next_work_item_state=WorkItemState.SHIPPED,
            reason="governed_lifecycle_completed",
            git_record={
                "merged": merged,
                "remote_main_contains_merge": merged,
                "merge_sha": "a" * 40,
                "pr_url": "https://example.test/pr/1",
                "integration_mode": "test_fake",
            },
        ))

    def generate_successor(self, parent_rel: str, rel: str, text: str) -> None:
        """What a shipped parent's merge adds: the file plus the merged digest."""
        self.successors_for[parent_rel] = [{"mission_path": rel, "mission_digest": _digest(text)}]
        self.ship_next()


# ---------------------------------------------------------------------------
# Trust model (unit)
# ---------------------------------------------------------------------------

def test_mission_number_recognizes_numbered_missions_only():
    assert mission_number("docs/MISSION_002.md") == 2
    assert mission_number("docs/DOGFOOD_MISSION_010.md") == 10
    assert mission_number("documentation/DOGFOOD_MISSION_001.md") == 1
    assert mission_number("docs/MISSION_FRAMEWORK.md") is None
    assert mission_number("elsewhere/MISSION_002.md") is None


def _trust(tmp_path, digest, path="docs/MISSION_002.md", items=None):
    return evaluate_mission_trust(
        PRODUCT, path, digest,
        mission_store=MissionAuthorizationStore(tmp_path / "auth"),
        provenance_store=SuccessorProvenanceStore(tmp_path / "prov"),
        load_work_item=lambda wid: (items or {}).get(wid),
    )


def test_unknown_mission_is_untrusted_root(tmp_path):
    assert _trust(tmp_path, _digest("x")).reason == UNTRUSTED_ROOT
    assert _trust(tmp_path, "").trusted is False


def test_authorization_is_bound_to_exact_content(tmp_path):
    store = MissionAuthorizationStore(tmp_path / "auth")
    store.authorize(PRODUCT, "docs/MISSION_001.md", _digest("v1"), "WI-1", owner_decision_id="D1")
    same = _trust(tmp_path, _digest("v1"), path="docs/MISSION_001.md")
    changed = _trust(tmp_path, _digest("v2"), path="docs/MISSION_001.md")
    other_repo = evaluate_mission_trust(
        OTHER, "docs/MISSION_001.md", _digest("v1"),
        mission_store=store, provenance_store=SuccessorProvenanceStore(tmp_path / "prov"),
        load_work_item=lambda wid: None,
    )
    assert (same.trusted, same.reason) == (True, TRUST_AUTHORIZED)
    assert (changed.trusted, changed.reason) == (False, CONTENT_CHANGED)
    assert (other_repo.trusted, other_repo.reason) == (False, UNTRUSTED_ROOT)


def test_authorize_refuses_an_empty_digest(tmp_path):
    with pytest.raises(ValueError):
        MissionAuthorizationStore(tmp_path / "auth").authorize(PRODUCT, "docs/MISSION_001.md", "", "WI-1")


def _shipped_parent(digest, state=WorkItemState.SHIPPED):
    parent = WorkItem.create(
        origin=WorkItemOrigin.OWNER_DIRECTION, repository=PRODUCT, title="M1",
        identity_keys=["docs/MISSION_001.md"], mission_path="docs/MISSION_001.md",
        mission_digest=digest,
    )
    parent.state = state
    return parent


def _record(tmp_path, parent, child_text):
    SuccessorProvenanceStore(tmp_path / "prov").record(
        repository=PRODUCT,
        parent_work_item_id=parent.work_item_id,
        parent_mission_path="docs/MISSION_001.md",
        parent_mission_digest=parent.mission_digest,
        parent_terminal_state="shipped",
        successor_mission_path="docs/MISSION_002.md",
        successor_mission_digest=_digest(child_text),
    )


def test_trusted_successor_requires_every_link(tmp_path):
    parent = _shipped_parent(_digest("m1"))
    _record(tmp_path, parent, "m2")
    items = {parent.work_item_id: parent}

    # Parent content never authorized: not trusted.
    assert _trust(tmp_path, _digest("m2"), items=items).reason == SUCCESSOR_PARENT_NOT_AUTHORIZED

    MissionAuthorizationStore(tmp_path / "auth").authorize(
        PRODUCT, "docs/MISSION_001.md", _digest("m1"), parent.work_item_id,
    )
    ok = _trust(tmp_path, _digest("m2"), items=items)
    assert (ok.trusted, ok.reason) == (True, TRUST_SUCCESSOR)
    assert ok.provenance_id

    # Tampered successor content.
    assert _trust(tmp_path, _digest("m2 tampered"), items=items).reason == SUCCESSOR_DIGEST_MISMATCH

    # Parent that did not ship.
    failed = {parent.work_item_id: _shipped_parent(_digest("m1"), state=WorkItemState.FAILED)}
    assert _trust(tmp_path, _digest("m2"), items=failed).reason == SUCCESSOR_PARENT_NOT_SHIPPED


# ---------------------------------------------------------------------------
# Item 9: untrusted root mission -> AWAITING_OWNER -> approve -> READY -> selected
# ---------------------------------------------------------------------------

def test_root_mission_awaits_owner_then_one_approval_makes_it_run(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "# Mission 001\nsynthetic\n")

    result = world.supervisor.tick()
    item = world.item_for("docs/MISSION_001.md")
    assert result.selected_work_item_id is None
    assert item.state == WorkItemState.AWAITING_OWNER
    assert item.admission_blocked_reason == UNTRUSTED_ROOT
    assert item.mission_digest == _digest("# Mission 001\nsynthetic\n")
    reason, action = parked_reason_and_action(item)
    assert reason == UNTRUSTED_ROOT
    assert action == f"howlplane factory approve {item.work_item_id}"

    summary = world.approve(item, reason="reviewed synthetic mission")
    assert summary["state"] == "ready"
    item = world.item_for("docs/MISSION_001.md")
    assert item.trusted_provenance is True and item.owner_decision_id == summary["owner_decision_id"]

    decision = world.decisions.load(summary["owner_decision_id"])
    assert decision.decision == "approved"
    assert decision.repository == PRODUCT
    assert decision.mission_digest == item.mission_digest
    assert decision.reason == "reviewed synthetic mission"
    assert decision.prior_state == "awaiting_owner"
    [auth] = world.auth_store.list_all()
    assert (auth.origin, auth.mission_digest, auth.owner_decision_id) == (
        AUTHORIZATION_ORIGIN_OWNER, item.mission_digest, decision.decision_id,
    )

    world.ship_next()
    result = world.supervisor.tick()
    assert result.selected_work_item_id == item.work_item_id
    assert world.supervisor.dispatcher.calls == [item.work_item_id]


def test_approval_refuses_content_the_owner_did_not_see(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "original\n")
    world.supervisor.tick()
    item = world.item_for("docs/MISSION_001.md")

    _write(world.root, "docs/MISSION_001.md", "silently replaced\n")
    with pytest.raises(OwnerDecisionError, match=CONTENT_CHANGED):
        world.approve(item)
    assert world.item_for("docs/MISSION_001.md").state == WorkItemState.AWAITING_OWNER
    assert world.decisions.list_all() == [] and world.auth_store.list_all() == []


def test_mission_changed_after_approval_requires_reapproval(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "v1\n")
    world.supervisor.tick()
    world.approve(world.item_for("docs/MISSION_001.md"))

    world.put_mission("docs/MISSION_001.md", "v2 with new scope\n")
    world.supervisor.tick()
    item = world.item_for("docs/MISSION_001.md")
    assert item.state == WorkItemState.AWAITING_OWNER
    assert item.admission_blocked_reason == CONTENT_CHANGED
    assert item.trusted_provenance is False
    assert world.supervisor.dispatcher.calls == []


def test_owner_decisions_validate_state_and_touch_only_the_named_item(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "m\n")
    world.supervisor.tick()
    item = world.item_for("docs/MISSION_001.md")
    bystander = WorkItem.create(
        origin=WorkItemOrigin.INFERRED_IMPROVEMENT, repository=OTHER, title="other",
        identity_keys=["other"],
    )
    bystander.transition_to(WorkItemState.AWAITING_OWNER)
    world.supervisor.work_item_store.save_object(bystander)

    with pytest.raises(OwnerDecisionError, match="not found"):
        world.approve(WorkItem.create(origin="owner_direction", repository=PRODUCT, title="x", identity_keys=["x"]))
    summary = world.approve(item, decision="rejected", reason="not now")
    assert summary["state"] == "rejected"
    with pytest.raises(OwnerDecisionError, match="requires awaiting_owner"):
        world.approve(item)
    assert world.supervisor.work_item_store.load(bystander.work_item_id).state == WorkItemState.AWAITING_OWNER
    history = world.supervisor.work_item_store.load(item.work_item_id).reopening_history
    assert history[-1]["reason"].startswith("owner_rejected:")


# ---------------------------------------------------------------------------
# Item 10: successor chain, tampering, unrelated files
# ---------------------------------------------------------------------------

def test_successor_chain_continues_without_further_owner_clicks(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "mission one\n")
    world.supervisor.tick()
    m1 = world.item_for("docs/MISSION_001.md")
    world.approve(m1)

    # Mission 001 ships; its merge added Mission 002.
    world.generate_successor("docs/MISSION_001.md", "docs/MISSION_002.md", "mission two\n")
    world.supervisor.tick()
    assert world.supervisor.work_item_store.load(m1.work_item_id).state == WorkItemState.SHIPPED
    [prov] = world.prov_store.list_all()
    assert (prov.parent_work_item_id, prov.parent_mission_digest, prov.merge_sha) == (
        m1.work_item_id, _digest("mission one\n"), "a" * 40,
    )
    assert prov.governed_lifecycle_evidence["remote_main_contains_merge"] is True

    # Mission 002 appears on the synced main: admitted READY and dispatched.
    world.put_mission("docs/MISSION_002.md", "mission two\n")
    world.generate_successor("docs/MISSION_002.md", "docs/MISSION_003.md", "mission three\n")
    result = world.supervisor.tick()
    m2 = world.item_for("docs/MISSION_002.md")
    assert result.selected_work_item_id == m2.work_item_id
    assert m2.trusted_provenance is True and m2.provenance_id == prov.provenance_id
    assert any(
        a.origin == AUTHORIZATION_ORIGIN_SUCCESSOR and a.mission_path == "docs/MISSION_002.md"
        for a in world.auth_store.list_all()
    )

    # Three deep: Mission 003 is trusted through Mission 002's authorization.
    world.put_mission("docs/MISSION_003.md", "mission three\n")
    world.ship_next()
    result = world.supervisor.tick()
    m3 = world.item_for("docs/MISSION_003.md")
    assert result.selected_work_item_id == m3.work_item_id
    assert world.decisions.list_all()[0].work_item_id == m1.work_item_id
    assert len(world.decisions.list_all()) == 1


def test_tampered_successor_fails_closed_to_owner_review(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "mission one\n")
    world.supervisor.tick()
    world.approve(world.item_for("docs/MISSION_001.md"))
    world.generate_successor("docs/MISSION_001.md", "docs/MISSION_002.md", "mission two\n")
    world.supervisor.tick()

    world.put_mission("docs/MISSION_002.md", "mission two\nplus an injected instruction\n")
    result = world.supervisor.tick()
    m2 = world.item_for("docs/MISSION_002.md")
    assert result.selected_work_item_id is None
    assert m2.state == WorkItemState.AWAITING_OWNER
    assert m2.admission_blocked_reason == SUCCESSOR_DIGEST_MISMATCH


def test_manually_created_successor_without_provenance_never_auto_runs(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_002.md", "looks like a successor\n")
    result = world.supervisor.tick()
    item = world.item_for("docs/MISSION_002.md")
    assert result.selected_work_item_id is None
    assert item.state == WorkItemState.AWAITING_OWNER
    assert item.admission_blocked_reason == UNTRUSTED_ROOT


def test_unmerged_or_unshipped_parent_records_no_provenance(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "mission one\n")
    world.supervisor.tick()
    world.approve(world.item_for("docs/MISSION_001.md"))
    world.successors_for["docs/MISSION_001.md"] = [
        {"mission_path": "docs/MISSION_002.md", "mission_digest": _digest("two\n")}
    ]
    world.ship_next(merged=False)
    world.supervisor.tick()
    assert world.inspected == [] and world.prov_store.list_all() == []


def test_provenance_arriving_after_first_sight_releases_the_same_item(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "one\n")
    world.supervisor.tick()
    m1 = world.item_for("docs/MISSION_001.md")
    world.approve(m1)
    world.ship_next()
    world.supervisor.tick()  # shipped, no successor reported yet

    world.put_mission("docs/MISSION_002.md", "two\n")
    world.supervisor.tick()
    assert world.item_for("docs/MISSION_002.md").state == WorkItemState.AWAITING_OWNER

    world.prov_store.record(
        repository=PRODUCT, parent_work_item_id=m1.work_item_id,
        parent_mission_path="docs/MISSION_001.md", parent_mission_digest=_digest("one\n"),
        parent_terminal_state="shipped", successor_mission_path="docs/MISSION_002.md",
        successor_mission_digest=_digest("two\n"),
    )
    world.ship_next()
    result = world.supervisor.tick()
    assert result.selected_work_item_id == world.item_for("docs/MISSION_002.md").work_item_id


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_inspector_reads_only_files_the_merge_added(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.test")
    _git(repo, "config", "user.name", "T")
    _write(repo, "docs/MISSION_001.md", "one\n")
    _write(repo, "docs/MISSION_FRAMEWORK.md", "framework\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _write(repo, "docs/MISSION_001.md", "one, done\n")
    _write(repo, "docs/MISSION_002.md", "two\n")
    _write(repo, "docs/NOTES.md", "not a mission\n")
    _write(repo, ".dogfood/mission_state.json", json.dumps({"next_mission": "docs/MISSION_002.md"}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "mission 001 complete")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    _write(repo, "docs/MISSION_002.md", "two, edited on disk later\n")

    found = inspect_successor_missions(repo, merge_sha, "docs/MISSION_001.md")
    assert found == [{"mission_path": "docs/MISSION_002.md", "mission_digest": _digest("two\n")}]


# ---------------------------------------------------------------------------
# Items 7/8: orchestration human boundary -> precise reason -> approve -> resume
# ---------------------------------------------------------------------------

def _replicate_parked_run(target_repo: Path, task_id: str, *, empty_diff: bool) -> Path:
    """The artifact shape WI-howlplane-6a668797bb32f9a2 left behind."""
    run = target_repo / ".task_runs" / task_id
    _write(run, "decision_packet.md", (
        f"# Human Authority Decision Packet: Task `{task_id}`\n\n"
        "## Boundary Triggers (Human Authorization Required)\n"
        "- ⚠️ **independent_review_unavailable**: independent_review_unavailable\n\n"
        "## Recommended Action\n"
        "Authorize manual review or configure an independent provider family.\n"
    ))
    _write(run, "implementation/diff.patch", "" if empty_diff else "diff --git a/x b/x\n+y\n")
    _write(run, "implementation/result.json", json.dumps({
        "agent_id": "codex",
        "stdout": "I couldn't apply the requested edits. `.agents` is mounted read-only.\nmore",
    }))
    _write(run, "reviews/test-falsifier/attempts/01-devin_cli/result.json", json.dumps({"outcome": "output_invalid"}))
    _write(run, "reviews/test-falsifier/attempts/02-claude_code/result.json", json.dumps({
        "outcome": "reviewer_failure", "failure_class": "EXECUTION_PERMISSION_REQUIRED",
    }))
    _write(run, "reviews/correctness-reviewer/attempts/01-claude_code/result.json", json.dumps({"outcome": "completed"}))
    return run


def test_describe_parked_orchestration_names_the_real_boundary(tmp_path):
    run = _replicate_parked_run(tmp_path / "target", "WI-howlplane-6a668797bb32f9a2", empty_diff=True)
    described = describe_parked_orchestration(run)
    assert described["triggers"] == ["independent_review_unavailable"]
    assert described["implementation_no_changes"] is True
    assert described["implementer"] == "codex"
    assert "read-only" in described["implementer_note"]
    assert described["reviewer_failures"] == [
        "test-falsifier: devin_cli output_invalid, claude_code EXECUTION_PERMISSION_REQUIRED"
    ]
    assert described["target_repo"] == str(tmp_path / "target")
    assert describe_parked_orchestration(tmp_path / "missing") is None


def test_legacy_empty_park_is_explained_and_steered_to_retry_not_approve(tmp_path):
    run = _replicate_parked_run(tmp_path / "target", "WI-legacy", empty_diff=True)
    item = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG, repository=OTHER, title="Encode TIA", identity_keys=["61"],
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    item.transition_to(WorkItemState.IN_PROGRESS)
    item.transition_to(WorkItemState.AWAITING_OWNER, reason="orchestrator_final_state:awaiting_human")
    item.admission_blocked_reason = "orchestrator_final_state:awaiting_human"

    reason, action = parked_reason_and_action(item, describe_parked_orchestration(run))
    assert reason.startswith("human_boundary:independent_review_unavailable")
    assert "implementation_no_changes (codex)" in reason
    assert "test-falsifier" in reason and "run_dir" in reason
    assert action == f"howlplane factory retry {item.work_item_id}"


class FakeLifecycle:
    def __init__(self, fail=None):
        self.approvals = []
        self.fail = fail

    def approve(self, target_repo, task_id, reason=None, operator_source="cli"):
        if self.fail:
            raise self.fail
        self.approvals.append((str(target_repo), task_id, operator_source))
        return type("Decision", (), {"decision": "approved"})()


def _boundary_world(tmp_path, *, empty_diff=False):
    world = MissionWorld(tmp_path, repository=OTHER)
    target = tmp_path / "managed" / "target"
    item = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG, repository=OTHER, title="backlog 61", identity_keys=["61"],
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    world.supervisor.work_item_store.save_object(item)
    run = _replicate_parked_run(target, item.work_item_id, empty_diff=empty_diff)
    world.supervisor.dispatcher.outcomes.append(DispatchOutcome(
        success=False,
        work_item_id=item.work_item_id,
        next_work_item_state=WorkItemState.AWAITING_OWNER,
        reason="orchestrator_awaiting_human:independent_review_unavailable",
        blocker="authority_boundary",
        requires_authority=True,
        failure_class="AUTHORITY_BLOCKED",
        human_boundary=describe_parked_orchestration(run),
    ))
    world.supervisor.tick()
    return world, world.supervisor.work_item_store.load(item.work_item_id), target


def test_parked_boundary_is_recorded_precisely_on_the_work_item(tmp_path):
    world, item, target = _boundary_world(tmp_path)
    assert item.state == WorkItemState.AWAITING_OWNER
    assert item.admission_blocked_reason == "human_boundary:independent_review_unavailable"
    assert item.human_boundary["orchestration_task_id"] == item.work_item_id
    reason, action = parked_reason_and_action(item)
    assert "orchestrator_final_state" not in reason
    assert f"task {item.work_item_id}" in reason
    assert action == f"howlplane factory approve {item.work_item_id}"


def test_approved_boundary_resumes_the_same_run_without_duplication(tmp_path):
    world, item, target = _boundary_world(tmp_path)
    lifecycle = FakeLifecycle()
    summary = world.approve(item, reason="manual review done", lifecycle=lifecycle)
    assert lifecycle.approvals == [(str(target), item.work_item_id, "factory_owner_approval:cli")]
    assert summary["resume_orchestration_task_id"] == item.work_item_id
    assert summary["state"] == "ready"
    decision = world.decisions.load(summary["owner_decision_id"])
    assert decision.orchestration_task_id == item.work_item_id

    seen = {}

    class ResumeAwareDispatcher(RecordingDispatcher):
        def dispatch(self, work_item, dispatch_id, task_id):
            seen["resume"] = work_item.resume_orchestration_task_id
            return super().dispatch(work_item, dispatch_id, task_id)

    world.supervisor.dispatcher = ResumeAwareDispatcher([DispatchOutcome(
        success=True, work_item_id=item.work_item_id,
        next_work_item_state=WorkItemState.SHIPPED, reason="governed_lifecycle_completed",
    )])
    result = world.supervisor.tick()
    assert result.selected_work_item_id == item.work_item_id
    assert seen["resume"] == item.work_item_id

    final = world.supervisor.work_item_store.load(item.work_item_id)
    assert final.state == WorkItemState.SHIPPED
    assert final.resume_orchestration_task_id is None and final.human_boundary is None
    assert final.task_ids == [f"FACTORY-{item.work_item_id}"]
    assert final.attempts == 2
    history = [d for d in world.supervisor.state_record.dispatch_history if d.get("work_item_id") == item.work_item_id]
    assert len(history) == 2
    assert len(world.supervisor.work_item_store.list_all()) == 1


def test_orchestration_approval_failure_is_surfaced_and_changes_nothing(tmp_path):
    world, item, _ = _boundary_world(tmp_path)
    with pytest.raises(OwnerDecisionError, match="StaleApprovalError"):
        world.approve(item, lifecycle=FakeLifecycle(fail=type("StaleApprovalError", (Exception,), {})("drift")))
    unchanged = world.supervisor.work_item_store.load(item.work_item_id)
    assert unchanged.state == WorkItemState.AWAITING_OWNER
    assert unchanged.resume_orchestration_task_id is None
    assert world.decisions.list_all() == []


def test_empty_implementation_park_cannot_be_approved_but_can_be_retried(tmp_path):
    world, item, _ = _boundary_world(tmp_path, empty_diff=True)
    with pytest.raises(OwnerDecisionError, match="factory retry"):
        world.approve(item, lifecycle=FakeLifecycle())
    summary = world.approve(item, decision="retry", reason="re-run with a writable implementer")
    retried = world.supervisor.work_item_store.load(item.work_item_id)
    assert summary["state"] == "ready"
    assert retried.human_boundary is None and retried.resume_orchestration_task_id is None
    assert world.decisions.load(summary["owner_decision_id"]).decision == "retry"


# ---------------------------------------------------------------------------
# Item 11: portfolio with the product mission awaiting the owner
# ---------------------------------------------------------------------------

def test_product_mission_awaiting_owner_does_not_freeze_the_portfolio(tmp_path):
    world = MissionWorld(tmp_path)
    world.put_mission("docs/MISSION_001.md", "grocery mission\n")
    for i in range(6):
        world.supervisor.state_record.record_dispatch(
            f"D-{i}", f"WI-old-{i}", f"T-{i}", "2026-08-30T00:00:00+00:00",
            origin="existing_backlog", repository=OTHER,
        )
    for i in range(3):
        backlog = WorkItem.create(
            origin=WorkItemOrigin.EXISTING_BACKLOG, repository=OTHER, title=f"b{i}",
            identity_keys=[f"b{i}"], source_rank=i,
        )
        backlog.transition_to(WorkItemState.ADMITTED)
        backlog.transition_to(WorkItemState.READY)
        world.supervisor.work_item_store.save_object(backlog)
        world.ship_next()

    for _ in range(3):
        assert world.supervisor.tick().selected_work_item_id is not None
    for _ in range(20):
        world.supervisor.tick()
    assert world.supervisor.state_record.active_alerts() == []
    assert world.supervisor.state_record.consecutive_capped_ticks == 0

    mission = world.item_for("docs/MISSION_001.md")
    assert mission.state == WorkItemState.AWAITING_OWNER
    world.approve(mission)
    world.ship_next()
    assert world.supervisor.tick().selected_work_item_id == mission.work_item_id
