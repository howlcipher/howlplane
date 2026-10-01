"""Offline tests for the HowlInstinct System 1 resource recommendation seam.

Nothing here touches the network. The CLI boundary is exercised with a stub
``howlinstinct`` executable that replays scripted output.
"""

import json
import os
import shutil
import stat
import sys
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from howlplane.control_plane import launcher
from howlplane.control_plane.config_loader import (
    ConfigLoader,
    ProviderPolicySettings,
    SemanticRecommendationSettings,
)
from howlplane.control_plane.reasoning.execution_trajectory import ExecutionTrajectory
from howlplane.control_plane.resource_cli import render_route
from howlplane.control_plane.resource_models import EconomicClass, ResourceLocality
from howlplane.control_plane.semantic.howlinstinct import (
    HowlInstinctSemanticRecommender,
    build_request,
    parse_decide_output,
    _Rejected,
)
from howlplane.control_plane.semantic.models import (
    RecommendationContext,
    SemanticRecommendation,
    SemanticStatus,
)
from howlplane.control_plane.semantic.policy import apply_policy
from howlplane.control_plane.semantic.recommender import MockSemanticRecommender
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from tests.test_ai_resource_pool import make_pool, make_profile, make_task

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    REPO_ROOT / "contracts" / "howlinstinct" / "howlinstinct.decision_receipt.v1.schema.json"
)
OFFERED = ["claude_code", "codex", "cursor"]


# ---------------------------------------------------------------- helpers

def judged(
    choice: str,
    *,
    margin: Optional[float] = 0.9,
    confidence: Optional[float] = None,
) -> SemanticRecommendation:
    return SemanticRecommendation(
        status=SemanticStatus.JUDGED,
        attempted=True,
        selected_resource_id=choice,
        instinct_margin=margin,
        provider_confidence=confidence,
        receipt_digest="sha256:" + "ab" * 32,
        provider="mock",
    )


def failure(status: SemanticStatus) -> SemanticRecommendation:
    return SemanticRecommendation(status=status, attempted=True, reason="scripted")


def settings(mode: str = "active", **overrides) -> SemanticRecommendationSettings:
    values = {"mode": mode, "minimum_instinct_margin": 0.35}
    values.update(overrides)
    return SemanticRecommendationSettings(**values)


def pool_with(
    recommender,
    mode: str = "active",
    *,
    profiles=None,
    policy: Optional[ProviderPolicySettings] = None,
    operating_mode: str = "connected",
    **setting_overrides,
) -> ProviderPoolManager:
    profiles = profiles or [make_profile(rid) for rid in ("codex", "claude_code", "cursor")]
    pool = make_pool(
        profiles,
        policy=policy or ProviderPolicySettings(preferred_external=["codex"]),
        operating_mode=operating_mode,
    )
    pool.configure_semantic(settings(mode, **setting_overrides), recommender)
    return pool


def pick(choice: str, **kwargs) -> MockSemanticRecommender:
    return MockSemanticRecommender(lambda ids: judged(choice, **kwargs))


def eligible_ids(decision) -> List[str]:
    return [item.resource_id for item in decision.eligible_resources]


# ------------------------------------------------------------ policy / config

def test_policy_accepts_only_at_or_above_plane_minimum():
    assert apply_policy(judged("codex", margin=0.35), minimum_instinct_margin=0.35).status \
        is SemanticStatus.ACCEPTED
    low = apply_policy(judged("codex", margin=0.34), minimum_instinct_margin=0.35)
    assert low.status is SemanticStatus.LOW_CONFIDENCE
    assert not low.accepted_by_policy


def test_policy_treats_absent_margin_as_low_confidence_not_zero():
    result = apply_policy(judged("codex", margin=None), minimum_instinct_margin=0.0)
    assert result.status is SemanticStatus.LOW_CONFIDENCE
    assert result.instinct_margin is None


def test_policy_leaves_failures_untouched():
    failed = failure(SemanticStatus.TIMEOUT)
    assert apply_policy(failed, minimum_instinct_margin=0.1) is failed


def test_semantic_settings_default_off_and_require_explicit_margin():
    assert SemanticRecommendationSettings().mode == "off"
    with pytest.raises(ValidationError):
        SemanticRecommendationSettings(mode="shadow")
    with pytest.raises(ValidationError):
        SemanticRecommendationSettings(mode="active", minimum_instinct_margin=1.5)
    with pytest.raises(ValidationError):
        SemanticRecommendationSettings(mode="off", surprise=True)


def test_operator_toml_enables_semantic_recommendation(tmp_path: Path):
    local = tmp_path / "config.toml"
    local.write_text(
        '[ai_resources]\noperating_mode = "connected"\n'
        '[ai_resources.semantic_recommendation]\n'
        'mode = "shadow"\nminimum_instinct_margin = 0.4\ntimeout_seconds = 2\n',
        encoding="utf-8",
    )
    loaded = ConfigLoader(
        config_path=str(tmp_path / "missing.yaml"), local_config_path=str(local)
    ).settings
    assert loaded.semantic_recommendation.mode == "shadow"
    assert loaded.semantic_recommendation.minimum_instinct_margin == 0.4


def test_default_pool_is_semantic_off():
    pool = make_pool([make_profile("codex"), make_profile("claude_code")])
    assert pool.semantic_settings.mode == "off"


def test_vendored_receipt_contract_is_a_valid_schema_with_source():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "howlinstinct.decision_receipt/v1"
    assert "Pinned commit" in (SCHEMA_PATH.parent / "SOURCE.md").read_text(encoding="utf-8")


# ---------------------------------------------------- pool: modes and invariants

def test_off_mode_never_calls_recommender_and_omits_semantic_block():
    recommender = pick("claude_code")
    pool = pool_with(recommender, mode="off", minimum_instinct_margin=None)
    decision = pool.select_resource(make_task(), role="implementation")
    assert recommender.calls == []
    assert "semantic_recommendation" not in decision.to_dict()
    assert decision.selected.resource_id == "codex"


def test_disabled_evidence_is_identical_to_a_pool_without_the_feature():
    plain = make_pool(
        [make_profile(r) for r in ("codex", "claude_code", "cursor")],
        policy=ProviderPolicySettings(preferred_external=["codex"]),
    ).select_resource(make_task(), role="implementation").to_dict()
    off = pool_with(pick("claude_code"), mode="off", minimum_instinct_margin=None) \
        .select_resource(make_task(), role="implementation").to_dict()
    assert plain == off


def test_shadow_records_judgment_but_keeps_deterministic_choice():
    recommender = pick("claude_code")
    pool = pool_with(recommender, mode="shadow")
    decision = pool.select_resource(make_task(), role="implementation")
    block = decision.semantic_recommendation
    assert decision.selected.resource_id == "codex"
    assert block["status"] == "ACCEPTED"
    assert block["accepted_by_policy"] is True
    assert block["applied"] is False
    assert block["deterministic_recommendation"] == "codex"
    assert block["final_selection"] == "codex"
    assert block["disagreement"] is True


def test_active_accepted_judgment_reorders_eligible_candidates():
    pool = pool_with(pick("claude_code"), mode="active")
    decision = pool.select_resource(make_task(), role="implementation")
    assert decision.selected.resource_id == "claude_code"
    assert decision.semantic_recommendation["applied"] is True
    assert decision.semantic_recommendation["disagreement"] is True  # still visible
    assert decision.semantic_recommendation["final_selection"] == "claude_code"
    assert decision.cognitive_recommendation.resource_id == "codex"  # deterministic kept
    assert eligible_ids(decision)[0] == "claude_code"


def test_active_low_confidence_falls_back_to_deterministic():
    pool = pool_with(pick("claude_code", margin=0.1), mode="active")
    decision = pool.select_resource(make_task(), role="implementation")
    assert decision.selected.resource_id == "codex"
    assert decision.semantic_recommendation["status"] == "LOW_CONFIDENCE"
    assert decision.semantic_recommendation["applied"] is False


def test_missing_provider_confidence_stays_absent_in_evidence():
    pool = pool_with(pick("claude_code", confidence=None), mode="active")
    block = pool.select_resource(make_task(), role="implementation").semantic_recommendation
    assert block["provider_confidence"] is None
    assert block["instinct_margin"] == 0.9


@pytest.mark.parametrize("status", [
    SemanticStatus.UNAVAILABLE,
    SemanticStatus.TIMEOUT,
    SemanticStatus.MALFORMED_RESPONSE,
    SemanticStatus.INVALID_RECEIPT,
    SemanticStatus.UNKNOWN_RESOURCE,
])
def test_failures_fall_back_to_deterministic_selection(status):
    pool = pool_with(MockSemanticRecommender(lambda ids: failure(status)))
    decision = pool.select_resource(make_task(), role="implementation")
    assert decision.selected.resource_id == "codex"
    assert decision.semantic_recommendation["status"] == status.value
    assert decision.semantic_recommendation["selected_resource_id"] is None


def test_strict_policy_blocks_on_integrity_failure_only():
    strict_bad = pool_with(
        MockSemanticRecommender(lambda ids: failure(SemanticStatus.INVALID_RECEIPT)),
        strict=True,
    ).select_resource(make_task(), role="implementation")
    assert strict_bad.selected is None
    assert strict_bad.blocked_reason == "SEMANTIC_RECOMMENDATION_FAILED"
    strict_down = pool_with(
        MockSemanticRecommender(lambda ids: failure(SemanticStatus.UNAVAILABLE)),
        strict=True,
    ).select_resource(make_task(), role="implementation")
    assert strict_down.selected.resource_id == "codex"


def test_unknown_resource_is_rejected_even_from_a_buggy_recommender():
    recommender = pick("evil-provider")
    decision = pool_with(recommender).select_resource(make_task(), role="implementation")
    assert decision.semantic_recommendation["status"] == "UNKNOWN_RESOURCE"
    assert decision.selected.resource_id == "codex"
    assert "evil-provider" not in eligible_ids(decision)


def test_excluded_resource_cannot_be_resurrected():
    profiles = [make_profile(r) for r in ("codex", "claude_code", "cursor", "devin_cli")]
    pool = make_pool(
        profiles,
        enabled={"codex": True, "claude_code": True, "cursor": True, "devin_cli": False},
        policy=ProviderPolicySettings(preferred_external=["codex"]),
    )
    recommender = pick("devin_cli")
    pool.configure_semantic(settings("active"), recommender)
    decision = pool.select_resource(make_task(), role="implementation")
    assert recommender.calls[0]["candidate_ids"] == ["claude_code", "codex", "cursor"]
    assert decision.semantic_recommendation["status"] == "UNKNOWN_RESOURCE"
    assert "devin_cli" not in eligible_ids(decision)


def test_exhausted_capacity_stays_excluded_even_if_recommended():
    pool = pool_with(pick("claude_code"))
    pool.set_status("claude_code", ProviderAvailabilityStatus.QUOTA_EXHAUSTED)
    recommender = pool._semantic_recommender
    decision = pool.select_resource(make_task(), role="implementation")
    assert recommender.calls[0]["candidate_ids"] == ["codex", "cursor"]
    assert decision.selected.resource_id == "codex"
    assert "claude_code" not in eligible_ids(decision)


def test_paid_api_stays_forbidden_and_unoffered():
    profiles = [
        make_profile("codex"),
        make_profile("cursor"),
        make_profile("paid_api", economics=EconomicClass.METERED_API),
    ]
    pool = pool_with(pick("paid_api"), profiles=profiles)
    decision = pool.select_resource(make_task(), role="implementation")
    assert pool._semantic_recommender.calls[0]["candidate_ids"] == ["codex", "cursor"]
    assert decision.semantic_recommendation["status"] == "UNKNOWN_RESOURCE"
    assert decision.exclusion_for("paid_api").reason == "PAID_API_FORBIDDEN"


def test_semantic_judgment_cannot_outrank_economics():
    profiles = [make_profile("codex"), make_profile("metered", economics=EconomicClass.METERED_API)]
    pool = pool_with(
        pick("metered"),
        profiles=profiles,
        policy=ProviderPolicySettings(allow_paid_api=True, preferred_external=["codex"]),
    )
    decision = pool.select_resource(make_task(), role="implementation")
    assert decision.semantic_recommendation["status"] == "ACCEPTED"
    assert decision.selected.resource_id == "codex"  # subscription outranks metered


def local_planning_decision(recommender, *, operating_mode, no_egress=False, **overrides):
    """Selects for planning over two local resources; shared by the egress tests."""
    local = [
        make_profile(rid, locality=ResourceLocality.LOCAL, economics=EconomicClass.LOCAL)
        for rid in ("local_a", "local_b")
    ]
    pool = pool_with(
        recommender, profiles=local, operating_mode=operating_mode,
        decision_roles=["planning"], policy=ProviderPolicySettings(), **overrides,
    )
    return pool.select_resource(make_task(no_egress=no_egress), role="planning")


@pytest.mark.parametrize("operating_mode, no_egress", [
    ("local_only", False),
    ("connected", True),
])
def test_egress_policy_blocks_semantic_call(operating_mode, no_egress):
    recommender = pick("local_b")
    decision = local_planning_decision(
        recommender, operating_mode=operating_mode, no_egress=no_egress
    )
    assert recommender.calls == []
    assert decision.semantic_recommendation["status"] == "NOT_APPLICABLE"


def test_local_only_operator_may_explicitly_allow_the_call():
    recommender = pick("local_b")
    decision = local_planning_decision(
        recommender, operating_mode="local_only", allow_in_local_only=True
    )
    assert len(recommender.calls) == 1
    assert decision.selected.resource_id == "local_b"


def test_explicit_override_is_authoritative_and_skips_judgment():
    recommender = pick("claude_code")
    pool = pool_with(recommender)
    decision = pool.select_resource(make_task(preferred="cursor"), role="implementation")
    assert recommender.calls == []
    assert decision.selected.resource_id == "cursor"
    assert decision.semantic_recommendation["status"] == "NOT_APPLICABLE"


def test_single_candidate_and_non_decision_roles_are_not_applicable():
    recommender = pick("codex")
    one = pool_with(recommender, profiles=[make_profile("codex")])
    assert one.select_resource(make_task(), role="implementation") \
        .semantic_recommendation["status"] == "NOT_APPLICABLE"
    many = pool_with(recommender)
    assert many.select_resource(make_task(), role="review") \
        .semantic_recommendation["status"] == "NOT_APPLICABLE"
    assert recommender.calls == []


def test_reviewer_selection_is_unchanged_by_active_semantic_mode():
    profiles = [make_profile("codex", provider_id="a"), make_profile("claude_code", provider_id="b"),
                make_profile("cursor", provider_id="c")]
    baseline = make_pool(profiles, policy=ProviderPolicySettings(preferred_external=["codex"]))
    active = pool_with(pick("claude_code"), profiles=profiles)
    roles = ["correctness-reviewer", "test-falsifier"]
    task = make_task()
    assert baseline.select_reviewers("codex", roles, task=task) == \
        active.select_reviewers("codex", roles, task=task)


def test_identical_receipt_gives_identical_final_ranking():
    results = [
        eligible_ids(pool_with(pick("cursor")).select_resource(make_task(), role="implementation"))
        for _ in range(3)
    ]
    assert results[0] == results[1] == results[2]
    assert results[0][0] == "cursor"
    assert results[0][1:] == sorted(results[0][1:])  # stable lexical tie break


def test_repeat_selection_reuses_judgment_and_failures_enter_cooldown():
    recommender = pick("claude_code")
    pool = pool_with(recommender)
    pool.select_resource(make_task(), role="implementation")
    pool.select_resource(make_task(), role="implementation")
    assert len(recommender.calls) == 1

    flaky = MockSemanticRecommender(lambda ids: failure(SemanticStatus.TIMEOUT))
    pool2 = pool_with(flaky)
    pool2.select_resource(make_task("A"), role="implementation")
    second = pool2.select_resource(make_task("B"), role="implementation")
    assert len(flaky.calls) == 1
    assert second.semantic_recommendation["status"] == "UNAVAILABLE"
    assert second.selected.resource_id == "codex"


def test_select_candidates_used_by_factory_reaches_semantic_selection():
    recommender = pick("claude_code")
    pool = pool_with(recommender)
    ordered = pool.select_candidates(task=make_task(), role="implementation")
    assert ordered[0] == "claude_code"
    assert len(recommender.calls) == 1
    marathon = (REPO_ROOT / "src/howlplane/control_plane/synthesis/marathon.py").read_text("utf-8")
    assert "provider_pool.select_candidates(" in marathon and "task=gap_probe" in marathon


# ------------------------------------------------------------ evidence / CLI

def test_semantic_block_survives_trajectory_round_trip_with_valid_digest():
    pool = pool_with(pick("claude_code"), mode="shadow")
    selection = pool.select_resource(make_task(), role="implementation").to_dict()
    trajectory = ExecutionTrajectory(
        trajectory_id="traj-semantic", task_id="POOL-TEST", resource_selection=selection
    )
    loaded = ExecutionTrajectory.from_dict(trajectory.to_dict())
    assert loaded.verify_digest()
    assert loaded.resource_selection["semantic_recommendation"]["receipt_digest"].startswith("sha256:")


def test_render_route_explains_each_semantic_outcome():
    accepted = pool_with(pick("claude_code")).select_resource(make_task(), role="implementation")
    text = render_route(accepted)
    assert "HowlInstinct (active mode):" in text
    assert "selected: claude_code" in text
    assert "provider confidence: not reported" in text
    assert "applied to ranking: yes" in text
    down = pool_with(
        MockSemanticRecommender(lambda ids: failure(SemanticStatus.UNAVAILABLE))
    ).select_resource(make_task(), role="implementation")
    down_text = render_route(down)
    assert "unavailable" in down_text and "fallback: deterministic" in down_text
    assert "HowlInstinct" not in render_route(
        make_pool([make_profile("codex")]).select_resource(make_task(), role="implementation")
    )


def test_route_cli_json_includes_versioned_semantic_block(monkeypatch, capsys, tmp_path):
    pool = pool_with(pick("claude_code"), mode="shadow")
    monkeypatch.setattr(launcher.ProviderPoolManager, "from_config",
                        classmethod(lambda cls, **kwargs: pool))
    from howlplane.control_plane import cli
    monkeypatch.setattr(cli.ProviderPoolManager, "from_config",
                        classmethod(lambda cls, **kwargs: pool))
    args = Namespace(
        objective="Implement a bounded repository change", task_id=None, risk="medium",
        tier=None, agent=None, role="implementation", json=True, semantic="active",
        repo=str(tmp_path), target_repo=str(tmp_path),
    )
    monkeypatch.setattr(cli, "_resolve_repo", lambda a: tmp_path)
    cli.cmd_route(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["semantic_recommendation"]["schema"] == "howlplane.semantic_recommendation/v1"
    assert payload["semantic_recommendation"]["mode"] == "active"


# ------------------------------------------- the CLI boundary (stub executable)

def receipt_for(choice: str, *, margin=0.6, confidence=None, probabilities=None) -> Dict:
    receipt = {
        "schema": "howlinstinct.decision_receipt/v1", "receipt_version": 1,
        "decision_id": "dec_" + "0" * 32, "batch_id": "batch_1",
        "timestamp": "2026-10-01T00:00:00Z", "question_id": "resource_selection",
        "decision_type": "choice", "state_hash": "sha256:" + "a" * 64,
        "question_hash": "sha256:" + "b" * 64, "outcome": "ACCEPTABLE_CONFIDENCE",
        "needs_escalation": False, "provider": "mock", "latency_ms": 1, "choice": choice,
        "probabilities": probabilities or {"claude_code": 0.7, "codex": 0.1, "cursor": 0.2},
    }
    if margin is not None:
        receipt["instinct_margin"] = margin
    if confidence is not None:
        receipt["provider_confidence"] = confidence
    return receipt


def output_for(choice="claude_code", *, receipt=None, **judgment_overrides) -> Dict:
    receipt = receipt if receipt is not None else receipt_for(choice)
    judgment = {
        "question_id": "resource_selection", "type": "choice",
        "outcome": "ACCEPTABLE_CONFIDENCE", "choice": choice,
        "probabilities": receipt.get("probabilities"), "needs_escalation": False,
    }
    for key in ("instinct_margin", "provider_confidence"):
        if key in receipt:
            judgment[key] = receipt[key]
    judgment.update(judgment_overrides)
    return {
        "schema": "howlinstinct.decide_output/v1", "batch_id": "batch_1", "provider": "mock",
        "model": "mock-lexical-baseline-v1", "latency_ms": 1,
        "judgments": {"resource_selection": judgment}, "receipts": [receipt],
    }


def parse(document, offered: Sequence[str] = OFFERED):
    return parse_decide_output(json.dumps(document), offered=offered)


def test_parse_valid_output_yields_judged_recommendation_with_absent_confidence():
    result = parse(output_for("claude_code"))
    assert result.status is SemanticStatus.JUDGED
    assert result.selected_resource_id == "claude_code"
    assert result.provider_confidence is None
    assert result.instinct_margin == 0.6
    assert result.receipt_digest.startswith("sha256:")
    assert "state" not in result.receipt


def test_parse_drops_retained_state_from_the_stored_receipt():
    receipt = receipt_for("codex")
    receipt["state"] = "raw secret state"
    assert "state" not in parse(output_for("codex", receipt=receipt)).receipt


@pytest.mark.parametrize("mutate, status", [
    (lambda d: d.update(schema="other/v1"), SemanticStatus.MALFORMED_RESPONSE),
    (lambda d: d["judgments"]["resource_selection"].update(choice="evil-provider"),
     SemanticStatus.UNKNOWN_RESOURCE),
    (lambda d: d["judgments"]["resource_selection"].update(
        probabilities={"evil-provider": 1.0}), SemanticStatus.UNKNOWN_RESOURCE),
    (lambda d: d["judgments"]["resource_selection"].update(
        probabilities={"codex": 0.2, "claude_code": 0.2}), SemanticStatus.MALFORMED_RESPONSE),
    (lambda d: d["judgments"]["resource_selection"].update(instinct_margin=1.7),
     SemanticStatus.MALFORMED_RESPONSE),
    (lambda d: d["judgments"]["resource_selection"].update(provider_confidence="high"),
     SemanticStatus.MALFORMED_RESPONSE),
    (lambda d: d["judgments"]["resource_selection"].update(outcome="UNAVAILABLE"),
     SemanticStatus.UNAVAILABLE),
    (lambda d: d["receipts"][0].pop("state_hash"), SemanticStatus.INVALID_RECEIPT),
    (lambda d: d["receipts"][0].update(unexpected_field=1), SemanticStatus.INVALID_RECEIPT),
    (lambda d: d["receipts"][0].update(choice="codex"), SemanticStatus.INVALID_RECEIPT),
    (lambda d: d.update(receipts=[]), SemanticStatus.INVALID_RECEIPT),
])
def test_parse_rejects_untrusted_output(mutate, status):
    document = output_for("claude_code")
    mutate(document)
    with pytest.raises(_Rejected) as caught:
        parse(document)
    assert caught.value.status is status


def test_missing_vendored_schema_fails_closed_without_raising(monkeypatch):
    from howlplane.control_plane.semantic import howlinstinct as module

    def broken():
        raise FileNotFoundError("schema missing")

    monkeypatch.setattr(module, "_receipt_validator", broken)
    with pytest.raises(_Rejected) as caught:
        parse(output_for("claude_code"))
    assert caught.value.status is SemanticStatus.CONFIG_ERROR


def test_parse_rejects_non_json_and_non_finite_numbers():
    with pytest.raises(_Rejected) as bad:
        parse_decide_output("not json", offered=OFFERED)
    assert bad.value.status is SemanticStatus.MALFORMED_RESPONSE
    raw = json.dumps(output_for("claude_code")).replace('"instinct_margin": 0.6', '"instinct_margin": NaN')
    with pytest.raises(_Rejected) as nan:
        parse_decide_output(raw, offered=OFFERED)
    assert nan.value.status is SemanticStatus.MALFORMED_RESPONSE


def write_stub(tmp_path: Path, *, stdout: str = "", exit_code: int = 0, sleep: float = 0.0,
               stderr: str = "") -> Path:
    log = tmp_path / "invocations.jsonl"
    script = tmp_path / "howlinstinct"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "stdin = sys.stdin.read()\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': stdin}}) + '\\n')\n"
        f"time.sleep({sleep})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def invoke(script: Path, task=None, *, timeout: float = 5.0, secret_objective: bool = False):
    profiles = [make_profile(rid) for rid in ("codex", "claude_code", "cursor")]
    task = task or make_task()
    if secret_objective:
        task.objective = "deploy with api_key=sk-abcdefghijklmnopqrstuvwx and mail me@example.com"
    recommender = HowlInstinctSemanticRecommender(command=str(script), mode="shadow")
    return recommender.recommend(
        task=task, candidates=profiles, role="implementation",
        context=RecommendationContext(capacity={"codex": "AVAILABLE"}, timeout_seconds=timeout),
    )


def test_cli_adapter_success_sends_bounded_redacted_request_without_task_text_in_argv(tmp_path):
    script = write_stub(tmp_path, stdout=json.dumps(output_for("claude_code")))
    result = invoke(script, secret_objective=True)
    assert result.status is SemanticStatus.JUDGED
    call = json.loads((tmp_path / "invocations.jsonl").read_text().splitlines()[0])
    assert call["argv"] == ["decide", "--input", "-", "--json"]
    request = json.loads(call["stdin"])
    options = [o["name"] for o in request["questions"]["resource_selection"]["options"]]
    assert options == ["claude_code", "codex", "cursor"]
    assert "sk-abcdefghij" not in call["stdin"] and "me@example.com" not in call["stdin"]
    assert "api_key=[REDACTED]" in call["stdin"] or "[REDACTED" in call["stdin"]
    assert "retain_state" not in request and "escalation" not in request


@pytest.mark.parametrize("exit_code, status", [
    (4, SemanticStatus.UNAVAILABLE),
    (6, SemanticStatus.TIMEOUT),
    (5, SemanticStatus.MALFORMED_RESPONSE),
    (3, SemanticStatus.CONFIG_ERROR),
    (1, SemanticStatus.UNAVAILABLE),
])
def test_cli_adapter_maps_exit_codes(tmp_path, exit_code, status):
    result = invoke(write_stub(tmp_path, exit_code=exit_code, stderr="boom"))
    assert result.status is status
    assert result.selected_resource_id is None


def test_cli_adapter_times_out_and_reports_missing_executable(tmp_path):
    assert invoke(write_stub(tmp_path, sleep=3.0), timeout=0.3).status is SemanticStatus.TIMEOUT
    assert invoke(tmp_path / "does-not-exist").status is SemanticStatus.UNAVAILABLE


def test_cli_adapter_flags_malformed_and_invalid_receipt(tmp_path):
    assert invoke(write_stub(tmp_path, stdout="{broken")).status is SemanticStatus.MALFORMED_RESPONSE
    bad = output_for("claude_code")
    bad["receipts"][0].pop("decision_id")
    assert invoke(write_stub(tmp_path, stdout=json.dumps(bad))).status is SemanticStatus.INVALID_RECEIPT


def test_cli_adapter_error_text_is_redacted_and_bounded(tmp_path):
    stub = write_stub(tmp_path, exit_code=4, stderr="token=abcdefghijklmnop failed " + "x" * 500)
    result = invoke(stub)
    assert "abcdefghijklmnop" not in result.reason and len(result.reason) <= 200


def test_build_request_is_deterministic_and_uses_exact_offered_set():
    profiles = [make_profile(r) for r in ("cursor", "codex", "claude_code")]
    first = build_request(make_task(), profiles, "implementation", RecommendationContext())
    second = build_request(make_task(), list(reversed(profiles)), "implementation", RecommendationContext())
    assert first == second
    assert [o["name"] for o in first["questions"]["resource_selection"]["options"]] == OFFERED


@pytest.mark.skipif(
    not (os.environ.get("HOWLINSTINCT_BIN") or shutil.which("howlinstinct")),
    reason="howlinstinct binary not available",
)
def test_real_howlinstinct_binary_with_mock_provider_is_plumbing_compatible(monkeypatch):
    """Plumbing evidence only: the mock provider is lexical, not semantic."""
    command = os.environ.get("HOWLINSTINCT_BIN") or shutil.which("howlinstinct")
    monkeypatch.setenv("HOWLINSTINCT_PROVIDER", "mock")
    monkeypatch.setenv("HOME", str(Path(command).parent))  # no operator config
    monkeypatch.delenv("HOWLINSTINCT_LOCAL_CONFIG", raising=False)
    profiles = [make_profile(rid) for rid in ("codex", "claude_code", "cursor")]
    result = HowlInstinctSemanticRecommender(command=command, mode="shadow").recommend(
        task=make_task(), candidates=profiles, role="implementation",
        context=RecommendationContext(timeout_seconds=15.0),
    )
    assert result.status is SemanticStatus.JUDGED, result.reason
    assert result.selected_resource_id in OFFERED
    assert result.receipt_digest.startswith("sha256:")


# ------------------------------------------------------- evaluation collection

def test_routing_eval_cases_cover_required_classes_and_claim_no_ground_truth():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import evaluate_semantic_routing as evaluation

    cases = evaluation.load_cases()
    assert len(cases) == 9
    assert all(case["label"] is None for case in cases)
    assert {"security_patch", "docs", "infrastructure", "refactor"} <= {c["task_class"] for c in cases}


def test_observation_records_leave_unmeasured_outcomes_null(tmp_path):
    import evaluate_semantic_routing as evaluation

    cases = evaluation.load_cases()[:2]
    recommender = pick("cursor")
    original = evaluation.build_pool

    def build(candidates, semantic):
        pool = original(candidates, semantic)
        pool.configure_semantic(semantic, recommender)
        return pool

    evaluation.build_pool = build
    try:
        semantic = settings("shadow", allow_in_local_only=True)
        records = evaluation.observe(
            cases, semantic, {cases[0]["id"]: {"resource_used": "cursor"}}
        )
    finally:
        evaluation.build_pool = original
    first, second = records
    assert first["schema"] == "howlplane.routing_observation/v1"
    assert first["resource_used"] == "cursor"
    assert second["resource_used"] is None and second["execution_outcome"] is None
    assert first["semantic_recommendation"] == "cursor"
    assert first["final_selection"] == first["deterministic_recommendation"]  # shadow
    assert first["provider_confidence"] is None
