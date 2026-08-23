#!/usr/bin/env python3
"""
tests/test_framework_gap_evidence.py

Framework-gap evidence policy (#59.1 Phase 5).

In campaign DOGFOOD-20260822-005043-16adca an agy repair-budget exhaustion was
classified as the framework gap HF_GAP_NOTES and opened the self-improvement
task ENG-NOTES-01. devin_cli then synthesized the same benchmark successfully
with no framework change at all -- proving the classification wrong.

Deciding "I must modify HowlPlane" has to clear a strictly higher evidence bar
than deciding "this generated application needs another attempt". These tests
pin that asymmetry: deterministic proof is actionable immediately, a model's
bad output never is on its own, and no quantity of model opinion substitutes.
"""


from src.control_plane.synthesis.campaign_state import DurableCampaignState
from src.control_plane.synthesis.capability_negotiator import FrameworkGap
from src.control_plane.synthesis.engine import SynthesisResult
from src.control_plane.synthesis.marathon import FrameworkGapEvidence, MarathonDogfoodEngine
from src.control_plane.synthesis.product_spec import ProductSpec
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)

ACCEPTANCE_FAILURE = (
    "Exhausted repair budget (3 cycles): Acceptance failure: server_health_probe: "
    "Server did not respond within 5.0s: <urlopen error [Errno 111] Connection refused>"
)
OTHER_ACCEPTANCE_FAILURE = (
    "Exhausted repair budget (3 cycles): Acceptance failure: crud_roundtrip: "
    "PUT /notes/1 returned 500"
)
COMPILER_FAILURE = "Compiler error: unsupported capability 'websocket_stream' at app/main.howl:12"


def _result(success=False, status="REPAIR_BUDGET_EXHAUSTED", error=None, provider="agy", gaps=None):
    return SynthesisResult(
        product_name="notes-app",
        product_spec=ProductSpec(name="notes-app", title="Notes", description="notes app"),
        success=success,
        status=status,
        implementing_provider=provider,
        framework_gaps=list(gaps or []),
        error_message=error,
    )


def _engine(tmp_path, available=("agy", "devin_cli"), synthesis_script=None, confirmations=1):
    """
    A marathon engine whose synthesis seam is scripted per provider, so a test
    can say "agy fails this way, devin_cli succeeds" and then assert what the
    real classification/falsification logic concluded.
    """
    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
    for agent_id in available:
        pool.set_status(agent_id, ProviderAvailabilityStatus.AVAILABLE)

    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns",
        target_repo=tmp_path, repo_slug="howlcipher/howlplane",
        framework_gap_confirmations_required=confirmations,
    )
    engine.synthesized_with = []
    if synthesis_script is not None:
        def _fake_synthesis(prompt, out_dir, avoid_provider, preferred_agent, iteration_idx):
            engine.synthesized_with.append(preferred_agent)
            return synthesis_script[preferred_agent]
        engine._execute_synthesis = _fake_synthesis
    return engine


def _falsify(engine, failing, signature_source):
    gap_type, _desc, _ev = engine._classify_failure(signature_source, "notes")
    return engine._falsify_framework_gap(
        prompt="Create a notes app", benchmark_key="notes",
        out_dir=engine.base_output_dir / "dogfood_notes", failing_provider=failing,
        signature=engine._failure_signature(signature_source, gap_type), iteration_idx=1,
    )


# ---------------------------------------------------------------------------
# 9. provider A exhausts its repair budget, provider B succeeds
# ---------------------------------------------------------------------------

def test_9_provider_specific_failure_creates_no_self_modification_task(tmp_path):
    """
    VERIFIED_REPLAY (#59.2 Phase 20): the exact shape of the live false
    positive from campaign DOGFOOD-20260822-005043-16adca -- agy exhausts its
    repair budget (real ACCEPTANCE_FAILURE text) and devin_cli synthesizes the
    same benchmark successfully with no framework change. That is evidence
    about agy, not about HowlFrame -- no gap, no engineering task. The real
    campaign incorrectly opened self-improvement task ENG-NOTES-01 for this
    shape; the current classification must not.
    """
    engine = _engine(tmp_path, synthesis_script={
        "agy": _result(error=ACCEPTANCE_FAILURE, provider="agy"),
        "devin_cli": _result(success=True, status="VERIFIED_PRODUCT", provider="devin_cli"),
    })
    failing = _result(error=ACCEPTANCE_FAILURE, provider="agy")

    evidence, rescued, _dir, attempts = _falsify(engine, "agy", failing)

    assert evidence == FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE
    assert rescued is not None and rescued.success is True
    assert rescued.implementing_provider == "devin_cli"
    assert [a["outcome"] for a in attempts] == ["SUCCESS"]


def test_9b_provisional_failure_is_never_actionable_on_its_own(tmp_path):
    """A repair-budget exhaustion starts provisional, not as a framework gap."""
    engine = _engine(tmp_path)
    gap_type, _desc, evidence = engine._classify_failure(_result(error=ACCEPTANCE_FAILURE), "notes")

    assert gap_type == "SYNTHESIS_REPAIR_FAIL"
    assert evidence == FrameworkGapEvidence.PROVISIONAL_FAILURE


# ---------------------------------------------------------------------------
# 10. two providers, same deterministic symptom
# ---------------------------------------------------------------------------

def test_10_same_deterministic_symptom_across_providers_verifies_gap(tmp_path):
    """
    When an independent provider reproduces the same failing acceptance check,
    the framework-gap classification is confirmed and a governed engineering
    task is warranted.
    """
    engine = _engine(tmp_path, synthesis_script={
        "agy": _result(error=ACCEPTANCE_FAILURE, provider="agy"),
        "devin_cli": _result(error=ACCEPTANCE_FAILURE, provider="devin_cli"),
    })

    evidence, rescued, _dir, attempts = _falsify(engine, "agy", _result(error=ACCEPTANCE_FAILURE))

    assert evidence == FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP
    assert rescued is None
    assert [a["outcome"] for a in attempts] == ["FAILED_SAME_SYMPTOM"]


def test_10b_different_symptom_is_not_a_framework_gap(tmp_path):
    """
    Two providers failing *differently* says something about the providers,
    not about the framework. Fail closed on self-modification.
    """
    engine = _engine(tmp_path, synthesis_script={
        "agy": _result(error=ACCEPTANCE_FAILURE, provider="agy"),
        "devin_cli": _result(error=OTHER_ACCEPTANCE_FAILURE, provider="devin_cli"),
    })

    evidence, _rescued, _dir, attempts = _falsify(engine, "agy", _result(error=ACCEPTANCE_FAILURE))

    assert evidence == FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE
    assert [a["outcome"] for a in attempts] == ["FAILED_DIFFERENT_SYMPTOM"]


# ---------------------------------------------------------------------------
# 11. deterministic proof needs no provider retries at all
# ---------------------------------------------------------------------------

def test_11_deterministic_proof_is_actionable_without_wasting_retries(tmp_path):
    """
    The capability negotiator, a blocked product, and a compiler rejection each
    prove something about the framework independently of generated code, so
    they must be actionable immediately -- no falsification round, no wasted
    provider quota.
    """
    engine = _engine(tmp_path)
    negotiator_gap = FrameworkGap(
        code="HF_GAP_WEBSOCKET", required_behavior="bidirectional streaming",
        current_support="none", suggested_enhancement="add ws runtime",
    )
    cases = [
        _result(gaps=[negotiator_gap]),
        _result(status="PRODUCT_BLOCKED", error="infeasible"),
        _result(error=COMPILER_FAILURE),
    ]

    for case in cases:
        _gap_type, _desc, evidence = engine._classify_failure(case, "notes")
        assert evidence == FrameworkGapEvidence.DETERMINISTIC_FRAMEWORK_PROOF

    # None of these consulted a second provider.
    assert engine.synthesized_with == []


def test_11b_independent_provider_hitting_deterministic_wall_verifies_gap(tmp_path):
    """
    If the confirming provider hits a *deterministic* framework wall, that is
    stronger than matching an ambiguous symptom and verifies the gap.
    """
    engine = _engine(tmp_path, synthesis_script={
        "agy": _result(error=ACCEPTANCE_FAILURE, provider="agy"),
        "devin_cli": _result(error=COMPILER_FAILURE, provider="devin_cli"),
    })

    evidence, _rescued, _dir, _attempts = _falsify(engine, "agy", _result(error=ACCEPTANCE_FAILURE))

    assert evidence == FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP


# ---------------------------------------------------------------------------
# 12/13. model opinion is not evidence
# ---------------------------------------------------------------------------

def test_12_reviewer_claiming_the_framework_is_broken_is_insufficient(tmp_path):
    """
    A reviewer asserting in prose that HowlFrame is at fault does not change
    the evidence class: classification reads structural facts only.
    """
    engine = _engine(tmp_path)
    claim = _result(error=(
        "Acceptance failure: server_health_probe: reviewer analysis concludes "
        "the HowlFrame runtime is fundamentally broken and the framework must "
        "be modified to support this product"
    ))

    _gap_type, _desc, evidence = engine._classify_failure(claim, "notes")

    assert evidence == FrameworkGapEvidence.PROVISIONAL_FAILURE
    assert evidence != FrameworkGapEvidence.DETERMINISTIC_FRAMEWORK_PROOF


def test_13_model_consensus_alone_cannot_trigger_self_modification(tmp_path):
    """
    Several providers all *claiming* a framework defect, while failing in
    materially different ways, must not verify a gap. Only a reproduced
    deterministic symptom counts -- agreement is not evidence.
    """
    consensus = "the HowlFrame framework must be modified to support this"
    engine = _engine(tmp_path, synthesis_script={
        "agy": _result(error=f"Acceptance failure: probe_a: {consensus}", provider="agy"),
        "devin_cli": _result(error=f"Acceptance failure: probe_b: {consensus}", provider="devin_cli"),
    })

    evidence, _rescued, _dir, _attempts = _falsify(
        engine, "agy", _result(error=f"Acceptance failure: probe_a: {consensus}"),
    )

    assert evidence != FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP
    assert evidence == FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE


def test_no_independent_provider_leaves_failure_provisional(tmp_path):
    """
    With nothing to falsify against, the failure stays provisional -- which is
    still not enough to modify HowlPlane.
    """
    engine = _engine(tmp_path, available=("agy",), synthesis_script={})

    evidence, rescued, _dir, attempts = _falsify(engine, "agy", _result(error=ACCEPTANCE_FAILURE))

    assert evidence == FrameworkGapEvidence.PROVISIONAL_FAILURE
    assert rescued is None
    assert attempts == []


def test_failure_signature_ignores_model_wording(tmp_path):
    """
    Signatures compare the failing check's identity, not free text, so wording
    differences between two models do not read as different symptoms.
    """
    engine = _engine(tmp_path)
    a = _result(error="Acceptance failure: server_health_probe: connection refused after 5s")
    b = _result(error="Acceptance failure: server_health_probe: no response, timed out")

    assert engine._failure_signature(a, "SYNTHESIS_REPAIR_FAIL") == \
        engine._failure_signature(b, "SYNTHESIS_REPAIR_FAIL")


def test_provider_specific_failure_is_recorded_but_creates_no_gap():
    """Durable state must distinguish 'provider failed' from 'framework gap'."""
    state = DurableCampaignState(campaign_id="DOGFOOD-EVIDENCE")
    state.record_provider_specific_failure({
        "benchmark": "notes", "gap_type": "SYNTHESIS_REPAIR_FAIL",
        "evidence": FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE.value,
        "failing_provider": "agy",
    })

    assert state.framework_gaps == []
    assert len(state.provider_specific_failures) == 1
