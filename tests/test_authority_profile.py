#!/usr/bin/env python3
"""
tests/test_authority_profile.py

Phase 21 authority profile test matrix (#59): 17 required scenarios proving
delegated overnight authority is deterministic, tamper-evident, expiring,
non-self-expandable, and that AI recommendations carry zero authority.
Modeled on the numbered-scenario convention of test_human_approval_lifecycle.py.
"""

from datetime import datetime, timedelta, timezone
import inspect
import json

import pytest

from src.control_plane.authority_envelope import (
    AuthorityDecision,
    ENVELOPE_FILENAME,
    EnvelopeAlreadyExistsError,
    TamperedEnvelopeError,
    create_envelope,
    evaluate_action_against_envelope,
    is_expired,
    load_envelope,
    save_envelope,
)
from src.control_plane.authority_profile import (
    CANONICAL_PROFILES,
    HOWLFRAME_OVERNIGHT_PROFILE,
    OVERNIGHT_SAFE_PROFILE,
    STRICT_PROFILE,
    UnknownProfileError,
    get_profile,
)
from src.control_plane.decision_queue import (
    ParkedTaskRecord,
    compute_blocks_other_work,
    request_ai_recommendation,
)
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.human_boundary import HumanBoundaryGate
from src.control_plane.synthesis.campaign_state import DurableCampaignState, GitIntegrationRecord
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import ProviderAvailabilityStatus, ProviderPoolManager
from src.control_plane.task_spec import TaskSpec
from tests._dogfood_test_helpers import FakeOrchestrator, ScriptedRunner, build_full_merge_flow

REPO_SLUG = "howlcipher/howlplane"


def _fresh_envelope(now=None, ttl_hours=12.0, campaign_id="DOGFOOD-AUTH-TEST"):
    profile = OVERNIGHT_SAFE_PROFILE
    if ttl_hours != profile.ttl_hours:
        from dataclasses import replace
        profile = replace(profile, ttl_hours=ttl_hours)
    return create_envelope(profile, campaign_id, "cli:test@host", now=now)


def _task(risk_level="low"):
    return TaskSpec(
        task_id="T-1", repository="howlplane", objective="fix a thing",
        task_class="bug_fix", risk_level=risk_level,
    )


# --- 1/2: green vs failed CI gates merge -------------------------------------

def _marathon_engine_for_git_flow(tmp_path, ci_green: bool, envelope):
    task_id = "ENG-X-01"
    git = ScriptedRunner()
    gh = ScriptedRunner()
    build_full_merge_flow(
        git, gh, task_id=task_id, repo_slug=REPO_SLUG, pr_number=9,
        commit_message="fix(x): resolve X_GAP found during marathon dogfooding",
        pr_title="fix(x): X_GAP", pr_body="Automated marathon dogfooding fix.\n\nGap: X_GAP\ndesc",
        merge_sha="msha9", ci_green=ci_green,
    )

    engine = MarathonDogfoodEngine(
        provider_pool=_pool_with_codex(),
        base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns",
        target_repo=tmp_path, repo_slug=REPO_SLUG,
        orchestrator_factory=lambda config: FakeOrchestrator(tmp_path / "run", "src/x.py"),
        git_executor_factory=lambda env, merges: GitIntegrationExecutor(
            tmp_path, REPO_SLUG, env, git_runner=git, gh_runner=gh, merges_so_far=merges,
        ),
    )
    engine.authority_envelope = envelope
    engine.git_executor = engine._git_executor_factory(envelope, 0)
    return engine, task_id


def _pool_with_codex():
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    return pool


def test_scenario_1_safe_fix_with_green_ci_delegated_authority_permits_merge(tmp_path):
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S1")
    engine, task_id = _marathon_engine_for_git_flow(tmp_path, ci_green=True, envelope=envelope)
    ok, rec = engine._execute_governed_engineering_improvement(
        task_id=task_id, benchmark_key="x", gap_type="X_GAP", gap_desc="desc",
    )
    assert ok is True
    assert GitIntegrationRecord.from_dict(rec).is_fully_integrated() is True


def test_scenario_2_safe_fix_with_failed_ci_merge_denied(tmp_path):
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S2")
    engine, task_id = _marathon_engine_for_git_flow(tmp_path, ci_green=False, envelope=envelope)
    ok, rec = engine._execute_governed_engineering_improvement(
        task_id=task_id, benchmark_key="x", gap_type="X_GAP", gap_desc="desc",
    )
    assert ok is False
    assert rec["merged"] is False
    assert rec["required_checks_green"] is False


# --- 3/4/5/6: never-delegatable / explicit human_approval_requirements -------

def test_scenario_3_force_push_proposal_denied_or_parked():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S3")
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(), ["git push --force origin main"], envelope, "howlplane", repo_slug=REPO_SLUG,
    )
    assert result.requires_human_approval is True
    assert "force_push" in result.triggered_boundaries


def test_scenario_4_production_deployment_parked_awaiting_human():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S4")
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(), ["deploy to prod"], envelope, "howlplane", repo_slug=REPO_SLUG,
    )
    assert result.requires_human_approval is True
    assert "production_deployment" in result.triggered_boundaries


def test_scenario_5_credential_creation_parked_awaiting_human():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S5")
    task = TaskSpec(
        task_id="T-5", repository="howlplane", objective="rotate a token",
        task_class="bug_fix", risk_level="low", human_approval_requirements=["credential_provisioning"],
    )
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        task, [], envelope, "howlplane", repo_slug=REPO_SLUG,
    )
    assert result.requires_human_approval is True
    assert "credential_provisioning" in result.triggered_boundaries


def test_scenario_6_new_external_dependency_parked_by_default():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S6")
    task = TaskSpec(
        task_id="T-6", repository="howlplane", objective="add a new dependency",
        task_class="feature", risk_level="low", human_approval_requirements=["external_dependency_addition"],
    )
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        task, [], envelope, "howlplane", repo_slug=REPO_SLUG,
    )
    assert result.requires_human_approval is True
    assert "external_dependency_addition" in result.triggered_boundaries


# --- 7: recommendation has zero authority effect ------------------------------

def test_scenario_7_ai_approve_recommendation_has_zero_authority_effect():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S7")
    # Structural proof: the envelope decision function has no parameter through
    # which a recommendation could even be threaded.
    assert "recommendation" not in inspect.signature(evaluate_action_against_envelope).parameters
    assert "recommended_action" not in inspect.signature(evaluate_action_against_envelope).parameters

    decision_before, _ = evaluate_action_against_envelope(envelope, "production_deployment", REPO_SLUG)
    assert decision_before == AuthorityDecision.DENIED_BY_ENVELOPE

    # An AI "APPROVE" recommendation is only ever stored as data on a
    # ParkedTaskRecord -- it cannot influence the envelope decision above.
    recommendation, providers = request_ai_recommendation(
        "deploy to prod", "production_deployment", recommend_fn=lambda obj, b: ("APPROVE", ["claude_code"]),
    )
    parked = ParkedTaskRecord(
        task_id="T-7", objective="deploy to prod", boundary_type="production_deployment",
        requested_action="production_deployment", repository=REPO_SLUG,
        recommended_action=recommendation, recommendation_providers=providers,
    )
    assert parked.recommended_action == "APPROVE"

    decision_after, _ = evaluate_action_against_envelope(envelope, "production_deployment", REPO_SLUG)
    assert decision_after == AuthorityDecision.DENIED_BY_ENVELOPE
    assert decision_after == decision_before


# --- 8/9: park-and-continue vs all-parked stop --------------------------------

def test_scenario_8_one_task_parks_independent_safe_task_exists_continues():
    blocks = compute_blocks_other_work(
        parked_task_id="ENG-A-01",
        framework_gaps=[{"code": "GAP_A"}, {"code": "GAP_B"}],
        requested_benchmarks=["notes", "todo"],
        already_succeeded_benchmarks=[],
        resolved_gap_codes=[],
        parked_tasks=[],
    )
    assert blocks is False


def test_scenario_9_all_remaining_useful_tasks_parked_stops_awaiting_human():
    blocks = compute_blocks_other_work(
        parked_task_id="ENG-A-01",
        framework_gaps=[{"code": "GAP_A"}],
        requested_benchmarks=["notes"],
        already_succeeded_benchmarks=["notes"],
        resolved_gap_codes=[],
        parked_tasks=[],
        current_gap_code="GAP_A",
    )
    assert blocks is True


# --- 10/11: TTL and merge budget ---------------------------------------------

def test_scenario_10_ttl_expires_no_further_delegated_writes():
    now = datetime.now(timezone.utc)
    envelope = _fresh_envelope(now=now - timedelta(hours=13), ttl_hours=12.0, campaign_id="DOGFOOD-S10")
    assert is_expired(envelope, now=now) is True
    decision, _ = evaluate_action_against_envelope(envelope, "merge_pull_request", REPO_SLUG, now=now)
    assert decision == AuthorityDecision.ENVELOPE_EXPIRED


def test_scenario_11_merge_budget_reached_requires_human_or_new_authority():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S11")
    decision, reason = evaluate_action_against_envelope(
        envelope, "merge_pull_request", REPO_SLUG, merges_so_far=envelope.max_merges,
    )
    assert decision == AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE
    assert "merge_budget_reached" in reason


# --- 12/13: self-modification always human -----------------------------------

def test_scenario_12_ai_modifying_own_authority_profile_requires_human():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S12")
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(), [], envelope, "howlplane", repo_slug=REPO_SLUG,
        files_changed=["src/control_plane/authority_profile.py"],
    )
    assert result.requires_human_approval is True
    assert "authority_enforcement_modification" in result.triggered_boundaries


def test_scenario_13_ai_modifying_human_boundary_gate_semantics_requires_human():
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S13")
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(), [], envelope, "howlplane", repo_slug=REPO_SLUG,
        files_changed=["src/control_plane/human_boundary.py"],
    )
    assert result.requires_human_approval is True
    assert "authority_enforcement_modification" in result.triggered_boundaries


# --- 14/15: resume semantics ---------------------------------------------------

def test_scenario_14_resume_within_valid_envelope_allowed(tmp_path):
    engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns")
    state_dir = tmp_path / "campaigns" / "DOGFOOD-S14"
    state_dir.mkdir(parents=True)
    envelope = engine._bind_authority_envelope(state_dir, "DOGFOOD-S14", "overnight-safe", is_resume=False)
    assert envelope is not None

    resumed = engine._bind_authority_envelope(state_dir, "DOGFOOD-S14", None, is_resume=True)
    assert resumed is not None
    assert resumed.campaign_id == envelope.campaign_id
    assert resumed.policy_digest == envelope.policy_digest


def test_scenario_15_resume_after_expiration_requires_explicit_reauthorization(tmp_path):
    engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns")
    state_dir = tmp_path / "campaigns" / "DOGFOOD-S15"
    state_dir.mkdir(parents=True)

    expired = create_envelope(
        OVERNIGHT_SAFE_PROFILE, "DOGFOOD-S15", "cli:test@host",
        now=datetime.now(timezone.utc) - timedelta(hours=13),
    )
    save_envelope(expired, state_dir)

    # No explicit reauthorization on this resume call: never silently renew.
    silent = engine._bind_authority_envelope(state_dir, "DOGFOOD-S15", None, is_resume=True)
    assert silent is None

    # Explicit --authority-profile on resume creates a fresh authorization period.
    (state_dir / ENVELOPE_FILENAME).unlink()
    save_envelope(expired, state_dir)
    reauthorized = engine._bind_authority_envelope(state_dir, "DOGFOOD-S15", "overnight-safe", is_resume=True)
    assert reauthorized is not None
    assert is_expired(reauthorized) is False


def test_scenario_16_tampered_authorization_artifact_fails_closed(tmp_path):
    envelope = _fresh_envelope(campaign_id="DOGFOOD-S16")
    save_envelope(envelope, tmp_path)

    # Tamper with the persisted artifact directly on disk.
    path = tmp_path / ENVELOPE_FILENAME
    data = json.loads(path.read_text())
    data["max_merges"] = 999999
    path.write_text(json.dumps(data))

    with pytest.raises(TamperedEnvelopeError):
        load_envelope(tmp_path)

    engine = MarathonDogfoodEngine(base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns")
    result = engine._bind_authority_envelope(tmp_path, "DOGFOOD-S16", None, is_resume=True)
    assert result is None  # fail closed -- tampered envelope grants nothing


def test_scenario_17_read_only_dogfood_status_cannot_mutate_or_authorize(monkeypatch, tmp_path):
    from src.control_plane import cli as cli_module

    def _explode(*args, **kwargs):
        raise AssertionError("read-only status must never construct a provider pool")

    monkeypatch.setattr(ProviderPoolManager, "__init__", _explode)

    state_dir = tmp_path / "campaigns" / "DOGFOOD-S17"
    state_dir.mkdir(parents=True)
    DurableCampaignState(campaign_id="DOGFOOD-S17").save(state_dir)
    assert not (state_dir / ENVELOPE_FILENAME).is_file()

    import argparse
    args = argparse.Namespace(status="DOGFOOD-S17", campaign_dir=str(tmp_path / "campaigns"), json=False)
    rc = cli_module.cmd_dogfood_status(args)
    assert rc == 0
    # Status inspection must not create or touch an authority envelope.
    assert not (state_dir / ENVELOPE_FILENAME).is_file()


def test_unknown_profile_id_raises():
    with pytest.raises(UnknownProfileError):
        get_profile("god-mode")


def test_profiles_are_frozen_and_every_canonical_entry_is_a_module_constant():
    """The mechanical guarantee that a campaign cannot invent its own authority.

    This test is a tripwire, and adding a profile is supposed to trip it. The
    count is not the guarantee -- identity is. Every entry in
    CANONICAL_PROFILES must BE one of the module-level constants, so a profile
    cannot be constructed at runtime and registered, and the constants
    themselves must be frozen so an existing grant cannot be widened in place.

    A new profile therefore has to be added here deliberately, by a reviewed
    change, which is the point. `howlframe-overnight` was added for the first
    HowlFrame backlog marathon (issues.md marathon-readiness work).
    """
    for profile in (STRICT_PROFILE, OVERNIGHT_SAFE_PROFILE, HOWLFRAME_OVERNIGHT_PROFILE):
        with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
            profile.max_merges = 999999

    assert set(CANONICAL_PROFILES.keys()) == {
        "strict", "overnight-safe", "howlframe-overnight",
    }
    assert CANONICAL_PROFILES["strict"] is STRICT_PROFILE
    assert CANONICAL_PROFILES["overnight-safe"] is OVERNIGHT_SAFE_PROFILE
    assert CANONICAL_PROFILES["howlframe-overnight"] is HOWLFRAME_OVERNIGHT_PROFILE

    # No entry may be anything other than one of those module-level constants.
    module_constants = {
        id(STRICT_PROFILE), id(OVERNIGHT_SAFE_PROFILE), id(HOWLFRAME_OVERNIGHT_PROFILE),
    }
    for profile_id, profile in CANONICAL_PROFILES.items():
        assert id(profile) in module_constants, (
            f"'{profile_id}' is not one of the module-level profile constants, so "
            f"it was constructed somewhere other than authority_profile.py"
        )


def test_adding_a_profile_did_not_widen_an_existing_grant():
    """A new repository authorization must be a new profile, not an edit.

    Extending OVERNIGHT_SAFE_PROFILE's authorized_repositories would silently
    widen the blast radius of every invocation that already uses it, including
    ones authorized before the change. The HowlFrame grant is a separate,
    explicitly selected profile for exactly that reason.
    """
    assert OVERNIGHT_SAFE_PROFILE.authorized_repositories == ["howlcipher/howlplane"]
    assert HOWLFRAME_OVERNIGHT_PROFILE.authorized_repositories == ["howlcipher/howlframe"]
    assert not set(OVERNIGHT_SAFE_PROFILE.authorized_repositories) & set(
        HOWLFRAME_OVERNIGHT_PROFILE.authorized_repositories
    )
    # And the new grant is strictly narrower on the axis that matters most.
    assert HOWLFRAME_OVERNIGHT_PROFILE.max_merges == 0 < OVERNIGHT_SAFE_PROFILE.max_merges
    assert set(HOWLFRAME_OVERNIGHT_PROFILE.allowed_action_classes) < set(
        OVERNIGHT_SAFE_PROFILE.allowed_action_classes
    )


def test_save_envelope_refuses_to_overwrite_existing(tmp_path):
    envelope = _fresh_envelope(campaign_id="DOGFOOD-WRITE-ONCE")
    save_envelope(envelope, tmp_path)
    with pytest.raises(EnvelopeAlreadyExistsError):
        save_envelope(envelope, tmp_path)
