"""Deterministic acceptance tests for Milestone #61 resource selection."""

import json
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from howlplane.control_plane.agent_execution import (
    AgentExecutionResult,
    BackendReadiness,
    FakeAgentBackend,
)
from howlplane.control_plane.agent_registry import AgentProfile, AgentRegistry
from howlplane.control_plane.resource_models import (
    AuthenticationStatus,
    EconomicClass,
    ProviderFailureClass,
    ReadinessStatus,
    ResourceLocality,
    ResourceSelectionStatus,
)
from howlplane.control_plane.orchestrator import (
    FAILURE_CLASS_NO_ELIGIBLE_RESOURCE,
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
)
from howlplane.control_plane import launcher
from howlplane.control_plane.resource_cli import (
    inventory_document,
    render_inventory,
    render_route,
    resource_diagnostic_rows,
)
from howlplane.control_plane.reasoning.execution_trajectory import (
    EXECUTION_TRAJECTORY_SCHEMA_VERSION,
    EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1,
    ExecutionTrajectory,
    ExecutionTrajectoryBuilder,
)
from howlplane.control_plane.router import TaskRouter
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderConfigurationError,
    ProviderPoolManager,
    ProviderStatus,
)
from howlplane.control_plane.task_spec import TaskSpec
from howlplane.control_plane.config_loader import (
    AppSettings,
    ProviderPolicySettings,
    ProviderResourceSettings,
)
from tests._git_test_helpers import init_git_repo


class CountingBackend(FakeAgentBackend):
    """Fake adapter exposing non-generative readiness call counts."""

    def __init__(
        self,
        resource_id: str,
        readiness: ReadinessStatus = ReadinessStatus.READY,
    ):
        super().__init__(agent_id=resource_id)
        self.readiness_status = readiness
        self.probe_calls = 0

    def probe_readiness(self) -> BackendReadiness:
        self.probe_calls += 1
        return BackendReadiness(
            status=self.readiness_status,
            installed=self.readiness_status != ReadinessStatus.MISSING_EXECUTABLE,
            reachable=self.readiness_status == ReadinessStatus.READY,
            authentication=AuthenticationStatus.UNKNOWN,
            reason=None if self.readiness_status == ReadinessStatus.READY else self.readiness_status.value,
        )


def make_profile(
    resource_id: str,
    *,
    provider_id: str | None = None,
    locality: ResourceLocality = ResourceLocality.HOSTED,
    economics: EconomicClass = EconomicClass.SUBSCRIPTION,
    roles: list[str] | None = None,
    repository_access: bool = True,
    model_id: str | None = None,
) -> AgentProfile:
    return AgentProfile(
        agent_id=resource_id,
        name=resource_id.replace("_", " ").title(),
        provider=provider_id or resource_id,
        interface="cli" if locality == ResourceLocality.HOSTED else "local_runtime",
        provider_id=provider_id or resource_id,
        interface_id=f"{resource_id}_interface",
        resource_id=resource_id,
        model_id=model_id,
        locality=locality.value,
        economic_class=economics.value,
        roles=roles or ["planning", "implementation", "remediation", "review"],
        capabilities=["code_generation", "file_editing", "code_review"],
        supports_repository_access=repository_access,
        supports_command_execution=repository_access,
    )


def make_task(
    task_id: str = "POOL-TEST",
    *,
    risk: str = "medium",
    preferred: str | None = None,
    no_egress: bool = False,
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository="fixture",
        objective="Implement a bounded repository change",
        task_class="feature",
        risk_level=risk,
        preferred_agent=preferred,
        metadata={"allow_egress": False} if no_egress else {},
    )


def make_pool(
    profiles: list[AgentProfile],
    *,
    enabled: dict[str, bool] | None = None,
    backends: dict[str, CountingBackend] | None = None,
    policy: ProviderPolicySettings | None = None,
    operating_mode: str = "connected",
    state_path: Path | None = None,
    probe_on_start: bool = True,
    role_exclusion_cooldown_seconds: float | None = None,
) -> ProviderPoolManager:
    registry = AgentRegistry(agents=profiles)
    configured = enabled or {p.resource_id: True for p in profiles}
    resources = {
        resource_id: ProviderResourceSettings(enabled=is_enabled)
        for resource_id, is_enabled in configured.items()
    }
    backend_map = backends or {
        p.resource_id: CountingBackend(p.resource_id) for p in profiles
    }
    return ProviderPoolManager(
        registry=registry,
        resources=resources,
        policy=policy or ProviderPolicySettings(),
        operating_mode=operating_mode,
        backend_resolver=lambda resource_id: backend_map[resource_id],
        state_path=state_path,
        probe_on_start=probe_on_start,
        **({"role_exclusion_cooldown_seconds": role_exclusion_cooldown_seconds}
           if role_exclusion_cooldown_seconds is not None else {}),
    )


def failed_result(resource_id: str, stderr: str, *, exit_code: int = 1) -> AgentExecutionResult:
    return AgentExecutionResult(
        agent_id=resource_id,
        role="implementation",
        command=resource_id,
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_seconds=0.1,
        success=False,
    )


def test_router_uses_shared_pool_and_records_review_resource_identities():
    implementer = make_profile("codex", provider_id="openai")
    reviewer = make_profile("claude_code", provider_id="anthropic")
    pool = make_pool([implementer, reviewer])

    route = TaskRouter(resource_pool=pool).route(make_task(preferred="codex"))

    assert route.selected_agent_id == "codex"
    assert route.metadata["resource_selection"]["selected"]["provider_id"] == "openai"
    assert route.metadata["review_diversity_achieved"] is True
    identities = route.metadata["reviewer_resource_identities"]
    assert all(item["provider_id"] == "anthropic" for item in identities.values())


def test_governed_orchestrator_blocks_structurally_before_provider_execution(tmp_path: Path):
    init_git_repo(tmp_path, initial_commit=False)
    profile = make_profile("disabled")
    backend = CountingBackend("disabled")
    pool = make_pool(
        [profile], enabled={"disabled": False}, backends={"disabled": backend}
    )
    orchestrator = GovernedTaskOrchestrator(
        target_repo=tmp_path,
        config=OrchestrationConfig(
            provider_pool=pool,
            enable_howlframe_audit=False,
            record_evidence=False,
            record_trajectory=False,
            acquire_locks=False,
        ),
    )

    result = orchestrator.run(make_task(task_id="POOL-BLOCKED"))

    assert result.final_state == "blocked"
    assert result.failure_class == FAILURE_CLASS_NO_ELIGIBLE_RESOURCE
    assert json.loads(result.error_message)["reason"] == "NO_ELIGIBLE_AI_RESOURCE"
    assert backend.executed_calls == []


def test_legacy_trajectory_schema_and_digest_remain_valid():
    legacy = ExecutionTrajectory(
        trajectory_id="legacy-trajectory",
        task_id="legacy-task",
        schema_version=EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1,
    )
    payload = legacy.to_dict()

    loaded = ExecutionTrajectory.from_dict(payload)

    assert loaded.schema_version == EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1
    assert loaded.verify_digest()
    assert EXECUTION_TRAJECTORY_SCHEMA_VERSION.endswith("/v2")


def test_ai_providers_inventory_has_stable_versioned_json():
    pool = make_pool([make_profile("claude_code", provider_id="anthropic")])

    document = inventory_document(pool)

    assert document["schema"] == "howlplane.ai_resources/v1"
    assert document["resources"][0]["identity"] == {
        "provider_id": "anthropic",
        "interface_id": "claude_code_interface",
        "resource_id": "claude_code",
        "model_id": None,
    }
    assert json.loads(json.dumps(document)) == document
    assert "AI RESOURCE POOL" in render_inventory(pool)


def test_ai_route_renderer_explains_capacity_and_selection():
    pool = make_pool([make_profile("codex")])
    decision = pool.select_resource(make_task(), role="implementation")

    rendered = render_route(decision)

    assert "Task class: feature" in rendered
    assert "Eligible:" in rendered
    assert "Likely selected: codex / codex_interface / unknown" in rendered


def test_ai_providers_cli_json_and_targeted_reset_are_audited(
    monkeypatch, capsys
):
    pool = make_pool([make_profile("codex")])
    entries = []

    class RecordingLedger:
        def append_entry(self, entry):
            entries.append(entry)

    monkeypatch.setattr(
        launcher.ProviderPoolManager,
        "from_config",
        classmethod(lambda cls, **kwargs: pool),
    )
    monkeypatch.setattr(launcher, "EvidenceLedger", RecordingLedger)

    assert launcher.cmd_providers(Namespace(
        provider_action=None, resource_id=None, json=True
    )) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == "howlplane.ai_resources/v1"

    assert launcher.cmd_providers(Namespace(
        provider_action="reset", resource_id="codex", json=False
    )) == 0
    assert entries[0].action == "provider_capacity_reset"
    assert entries[0].metadata["resource_id"] == "codex"


def test_resource_doctor_never_consumes_generation():
    backend = CountingBackend("codex")
    pool = make_pool(
        [make_profile("codex")], backends={"codex": backend}
    )

    rows = resource_diagnostic_rows(pool)

    assert rows[0]["status"] == "ok"
    assert backend.executed_calls == []


def test_resource_identity_keeps_provider_interface_resource_and_model_distinct():
    profile = make_profile(
        "claude_code",
        provider_id="anthropic",
        model_id=None,
    )

    identity = profile.resource_identity()

    assert identity.provider_id == "anthropic"
    assert identity.interface_id == "claude_code_interface"
    assert identity.resource_id == "claude_code"
    assert identity.model_id is None


def test_unknown_configured_resource_fails_validation():
    profile = make_profile("known")

    with pytest.raises(ProviderConfigurationError, match="unknown_resource"):
        make_pool([profile], enabled={"unknown_resource": True})


def test_disabled_resource_is_neither_probed_nor_selected():
    enabled_profile = make_profile("enabled")
    disabled_profile = make_profile("disabled")
    backends = {
        "enabled": CountingBackend("enabled"),
        "disabled": CountingBackend("disabled"),
    }
    pool = make_pool(
        [enabled_profile, disabled_profile],
        enabled={"enabled": True, "disabled": False},
        backends=backends,
    )

    decision = pool.select_resource(make_task(), role="implementation")

    assert backends["disabled"].probe_calls == 0
    assert decision.selected.resource_id == "enabled"
    assert decision.exclusion_for("disabled").reason == "OPERATOR_DISABLED"


def test_persisted_disabled_state_does_not_outlive_enabling_configuration(tmp_path):
    """A resource disabled by an earlier config must not stay parked after it is enabled."""
    state_path = tmp_path / "capacity.json"
    profile = make_profile("sample")
    make_pool([profile], enabled={"sample": False}, state_path=state_path)
    assert json.loads(state_path.read_text())["resources"]["sample"]["status"] == "DISABLED"

    pool = make_pool(
        [profile],
        enabled={"sample": True},
        state_path=state_path,
        probe_on_start=False,
    )

    assert pool.get_status("sample") == ProviderAvailabilityStatus.UNKNOWN
    assert pool.has_available_providers()


def test_configured_missing_resource_is_skipped_honestly():
    missing = make_profile("missing")
    ready = make_profile("ready")
    backends = {
        "missing": CountingBackend("missing", ReadinessStatus.MISSING_EXECUTABLE),
        "ready": CountingBackend("ready"),
    }
    pool = make_pool([missing, ready], backends=backends)

    decision = pool.select_resource(make_task(), role="implementation")

    assert decision.selected.resource_id == "ready"
    assert decision.exclusion_for("missing").reason == "MISSING_EXECUTABLE"


def test_fake_future_provider_uses_generic_registration_selection_and_capacity(tmp_path):
    profile = make_profile(
        "fake_future_provider",
        provider_id="future_org",
        model_id="future_model_observed",
    )
    backend = CountingBackend("fake_future_provider")
    pool = make_pool(
        [profile],
        backends={"fake_future_provider": backend},
        state_path=tmp_path / "capacity.json",
    )

    decision = pool.select_resource(make_task(), role="implementation")
    pool.record_result(
        "fake_future_provider",
        AgentExecutionResult(
            agent_id="fake_future_provider",
            role="implementation",
            command="fake",
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.1,
            success=True,
        ),
    )

    assert decision.selected.resource_id == "fake_future_provider"
    assert pool.get_status("fake_future_provider") == ProviderAvailabilityStatus.AVAILABLE
    assert (tmp_path / "capacity.json").is_file()


def test_fake_future_provider_selection_is_recorded_by_generic_trajectory(tmp_path):
    profile = make_profile("fake_future_provider", provider_id="future_org")
    pool = make_pool([profile])
    task = make_task(task_id="FUTURE-TRAJECTORY")
    task.metadata["failover_from_resource_id"] = "temporarily_unavailable"
    decision = pool.select_resource(task, role="implementation")
    route = TaskRouter(resource_pool=pool).route(task)
    execution = AgentExecutionResult(
        agent_id="fake_future_provider",
        role="implementation",
        command="fake",
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.1,
        success=True,
    )
    result = OrchestrationResult(
        task_id=task.task_id,
        task_spec=task,
        final_state="complete",
        exit_code=0,
        routing_decision=route,
        provider_execution=execution,
        resource_selection=decision.to_dict(),
        capacity_after=pool.get_all_statuses(),
        run_dir=str(tmp_path),
    )

    trajectory = ExecutionTrajectoryBuilder.from_orchestration_result(result)

    assert trajectory.resource_selection["selected"]["resource_id"] == (
        "fake_future_provider"
    )
    assert trajectory.resource_selection["selected"]["provider_id"] == "future_org"
    assert trajectory.provider_events[0]["role"] == "implementation"
    assert trajectory.failover_from_resource_id == "temporarily_unavailable"


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        (["claude_code"], "claude_code"),
        (["claude_code", "codex"], "codex"),
        (["claude_code", "codex", "local_runtime"], "codex"),
    ],
)
def test_supported_external_subsets_select_deterministically(resources, expected):
    profiles = [
        make_profile(
            resource_id,
            locality=(
                ResourceLocality.LOCAL
                if resource_id == "local_runtime"
                else ResourceLocality.HOSTED
            ),
            economics=(
                EconomicClass.LOCAL
                if resource_id == "local_runtime"
                else EconomicClass.SUBSCRIPTION
            ),
        )
        for resource_id in resources
    ]
    policy = ProviderPolicySettings(
        preferred_external=[
            resource_id
            for resource_id in ("codex", "claude_code")
            if resource_id in resources
        ]
    )
    pool = make_pool(profiles, policy=policy)

    decision = pool.select_resource(make_task(), role="implementation")

    assert decision.selected.resource_id == expected


def test_local_only_performs_zero_hosted_probes_and_can_select_local_planning():
    hosted = make_profile("hosted")
    local = make_profile(
        "local",
        locality=ResourceLocality.LOCAL,
        economics=EconomicClass.LOCAL,
        roles=["planning", "review"],
        repository_access=False,
    )
    backends = {
        "hosted": CountingBackend("hosted"),
        "local": CountingBackend("local"),
    }
    pool = make_pool(
        [hosted, local],
        backends=backends,
        operating_mode="local_only",
    )

    decision = pool.select_resource(make_task(risk="low"), role="planning")

    assert backends["hosted"].probe_calls == 0
    assert len(backends["hosted"].executed_calls) == 0
    assert decision.selected.resource_id == "local"
    assert decision.exclusion_for("hosted").reason == "EGRESS_FORBIDDEN"


def test_local_resource_without_repository_contract_is_excluded_for_implementation():
    local = make_profile(
        "local",
        locality=ResourceLocality.LOCAL,
        economics=EconomicClass.LOCAL,
        repository_access=False,
    )
    pool = make_pool([local], operating_mode="local_only")

    decision = pool.select_resource(make_task(risk="low"), role="implementation")

    assert decision.status == ResourceSelectionStatus.BLOCKED
    assert decision.blocked_reason == "NO_ELIGIBLE_AI_RESOURCE"
    assert decision.exclusion_for("local").reason == "MISSING_REQUIRED_CAPABILITY"


def test_no_egress_task_excludes_external_resource_without_probing():
    hosted = make_profile("hosted")
    backend = CountingBackend("hosted")
    pool = make_pool([hosted], backends={"hosted": backend}, probe_on_start=False)

    decision = pool.select_resource(
        make_task(no_egress=True),
        role="implementation",
    )

    assert backend.probe_calls == 0
    assert decision.status == ResourceSelectionStatus.BLOCKED
    assert decision.exclusion_for("hosted").reason == "TASK_EGRESS_FORBIDDEN"


def test_operator_preference_cannot_override_capability():
    incapable = make_profile("preferred", repository_access=False)
    capable = make_profile("capable")
    policy = ProviderPolicySettings(preferred_external=["preferred", "capable"])
    pool = make_pool([incapable, capable], policy=policy)

    decision = pool.select_resource(make_task(), role="implementation")

    assert decision.selected.resource_id == "capable"
    assert decision.exclusion_for("preferred").reason == "MISSING_REQUIRED_CAPABILITY"


def test_reviewer_role_eligibility_is_enforced():
    implementer_only = make_profile("implementer", roles=["implementation"])
    reviewer = make_profile("reviewer", roles=["review"])
    pool = make_pool([implementer_only, reviewer])

    decision = pool.select_resource(make_task(), role="review")

    assert decision.selected.resource_id == "reviewer"
    assert decision.exclusion_for("implementer").reason == "ROLE_NOT_SUPPORTED"


def test_subscription_first_and_no_paid_api_fallback():
    subscription = make_profile("subscription")
    metered = make_profile("metered", economics=EconomicClass.METERED_API)
    policy = ProviderPolicySettings(
        subscription_first=True,
        allow_paid_api=False,
        preferred_external=["metered", "subscription"],
    )
    pool = make_pool([subscription, metered], policy=policy)

    first = pool.select_resource(make_task(), role="implementation")
    pool.set_status("subscription", ProviderAvailabilityStatus.SESSION_EXHAUSTED)
    second = pool.select_resource(make_task(), role="implementation")

    assert first.selected.resource_id == "subscription"
    assert second.status == ResourceSelectionStatus.BLOCKED
    assert second.exclusion_for("metered").reason == "PAID_API_FORBIDDEN"


def test_metered_budget_counts_attempts_and_blocks_further_spend():
    metered = make_profile("metered", economics=EconomicClass.METERED_API)
    pool = make_pool(
        [metered],
        policy=ProviderPolicySettings(
            allow_paid_api=True,
            max_metered_invocations=1,
        ),
    )

    first = pool.select_resource(make_task(), role="implementation")
    pool.record_result("metered", failed_result("metered", "tests failed"))
    second = pool.select_resource(make_task(), role="implementation")

    assert first.selected.resource_id == "metered"
    assert second.status == ResourceSelectionStatus.BLOCKED
    assert second.exclusion_for("metered").reason == "METERED_BUDGET_EXHAUSTED"


def test_configured_model_override_is_used_in_selected_identity():
    profile = make_profile(
        "local_runtime",
        locality=ResourceLocality.LOCAL,
        economics=EconomicClass.LOCAL,
        roles=["planning"],
        repository_access=False,
        model_id="default-model",
    )
    profile.model_configurable = True
    settings = AppSettings(
        operating_mode="local_only",
        providers={
            "local_runtime": ProviderResourceSettings(
                enabled=True,
                model_id="configured-model",
            )
        },
    )
    pool = ProviderPoolManager.from_settings(
        settings,
        registry=AgentRegistry(agents=[profile]),
        probe_on_start=False,
    )

    decision = pool.select_resource(make_task(risk="low"), role="planning")

    assert decision.selected.model_id == "configured-model"
    assert pool.inventory()[0]["identity"]["model_id"] == "configured-model"


def test_temporary_rate_limit_recovers_after_bounded_cooldown():
    backend = CountingBackend("resource")
    pool = make_pool(
        [make_profile("resource")], backends={"resource": backend}
    )
    state = pool.get_resource_status("resource")
    state.status = ProviderAvailabilityStatus.RATE_LIMITED
    state.retry_after = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    decision = pool.select_resource(make_task(), role="implementation")

    assert decision.selected.resource_id == "resource"
    assert backend.probe_calls == 2


def test_explicit_override_bypasses_recommendation_but_not_hard_policy():
    first = make_profile("first")
    requested = make_profile("requested")
    pool = make_pool(
        [first, requested],
        policy=ProviderPolicySettings(preferred_external=["first"]),
    )

    allowed = pool.select_resource(
        make_task(preferred="requested"),
        role="implementation",
        explicit_resource_id="requested",
    )
    pool.set_status("requested", ProviderAvailabilityStatus.AUTH_REQUIRED)
    blocked = pool.select_resource(
        make_task(preferred="requested"),
        role="implementation",
        explicit_resource_id="requested",
    )

    assert allowed.selected.resource_id == "requested"
    assert allowed.explicit_override is True
    assert blocked.status == ResourceSelectionStatus.BLOCKED
    assert blocked.exclusion_for("requested").reason == "AUTH_REQUIRED"
    # The healthy resource the pin passed over is named, not silently dropped.
    passed_over = blocked.exclusion_for("first")
    assert passed_over.reason == "EXPLICIT_AGENT_REQUESTED"
    assert passed_over.stage == "operator_override" and passed_over.detail == "requested"
    assert blocked.eligible_resources == []


def test_pinned_failover_names_the_workers_it_did_not_offer():
    """`work --agent X`: once X is attempted, the others are reported as passed over
    by the pin rather than vanishing (Objective 1 dogfood, OBJ1-CLAUDE)."""
    pool = make_pool([make_profile("first"), make_profile("requested")])

    decision = pool.select_resource(
        make_task(preferred="requested"),
        role="implementation",
        exclude_resource_ids=["requested"],
    )

    assert decision.status == ResourceSelectionStatus.BLOCKED
    assert decision.exclusion_for("requested").reason == "ALREADY_ATTEMPTED"
    assert decision.exclusion_for("first").reason == "EXPLICIT_AGENT_REQUESTED"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("usage limit reached", ProviderFailureClass.SESSION_LIMIT),
        ("429 too many requests", ProviderFailureClass.RATE_LIMITED),
        ("authentication required", ProviderFailureClass.AUTHENTICATION_REQUIRED),
        ("Failed to authenticate: OAuth session expired and could not be refreshed",
         ProviderFailureClass.AUTHENTICATION_REQUIRED),
        ("provider service unavailable", ProviderFailureClass.PROVIDER_UNAVAILABLE),
        ("command not found", ProviderFailureClass.MISSING_EXECUTABLE),
        ("tests failed: assertion error", ProviderFailureClass.ENGINEERING_FAILURE),
        ("malformed yaml review", ProviderFailureClass.MALFORMED_OUTPUT),
    ],
)
def test_failure_taxonomy_is_normalized_without_conflation(stderr, expected):
    pool = make_pool([make_profile("resource")])

    assert pool.classify_failure("resource", failed_result("resource", stderr)) == expected


def test_capacity_state_persists_recovers_and_is_shared(tmp_path):
    path = tmp_path / "capacity.json"
    profile = make_profile("resource")
    first = make_pool([profile], state_path=path)
    first.record_result("resource", failed_result("resource", "usage limit reached"))

    restarted = make_pool([profile], state_path=path, probe_on_start=False)
    before = restarted.select_resource(make_task(), role="implementation")
    restarted.reset_resource("resource", reprobe=False)
    after = restarted.select_resource(make_task(), role="implementation")

    assert restarted.get_status("resource") == ProviderAvailabilityStatus.UNKNOWN
    assert before.status == ResourceSelectionStatus.BLOCKED
    assert before.exclusion_for("resource").reason == "SESSION_EXHAUSTED"
    assert after.selected.resource_id == "resource"


def test_oauth_expiration_persists_and_routes_to_another_worker(tmp_path):
    path = tmp_path / "capacity.json"
    profiles = [make_profile("expired"), make_profile("healthy")]
    pool = make_pool(profiles, state_path=path)
    pool.record_result("expired", failed_result(
        "expired", "Failed to authenticate: OAuth session expired and could not be refreshed"
    ))

    restarted = make_pool(profiles, state_path=path, probe_on_start=False)
    decision = restarted.select_resource(make_task(), role="implementation")

    assert restarted.get_status("expired") == ProviderAvailabilityStatus.AUTH_REQUIRED
    assert decision.selected.resource_id == "healthy"
    assert decision.exclusion_for("expired").reason == "AUTH_REQUIRED"


def test_engineering_failure_does_not_exhaust_resource():
    pool = make_pool([make_profile("resource")])
    before = pool.get_status("resource")

    failure_class = pool.record_result(
        "resource",
        failed_result("resource", "pytest: one assertion failed"),
    )

    assert failure_class == ProviderFailureClass.ENGINEERING_FAILURE
    assert pool.get_status("resource") == before


def test_independent_review_prefers_distinct_resource_and_reports_unavailability():
    implementation = make_profile("implementation", provider_id="one")
    independent = make_profile("independent", provider_id="two")
    pool = make_pool([implementation, independent])

    mapping, diversity = pool.select_reviewers(
        "implementation",
        ["correctness-reviewer"],
    )
    one_pool = make_pool([implementation])
    same_mapping, same_diversity = one_pool.select_reviewers(
        "implementation",
        ["correctness-reviewer"],
    )

    assert mapping == {"correctness-reviewer": "independent"}
    assert diversity is True
    assert same_mapping == {"correctness-reviewer": "implementation"}
    assert same_diversity is False


def test_legacy_settings_load_without_declaring_new_provider_authority():
    settings = AppSettings(operating_mode="local_only", llm_model="ollama/model")

    assert settings.providers == {}
    assert settings.provider_policy.allow_paid_api is False


# -- saved MISSING_EXECUTABLE readiness is rechecked, not trusted forever ----------------


def _save_missing_executable(path: Path, profile: AgentProfile) -> None:
    missing = CountingBackend(profile.resource_id, ReadinessStatus.MISSING_EXECUTABLE)
    make_pool([profile], backends={profile.resource_id: missing}, state_path=path)
    saved = json.loads(path.read_text())["resources"][profile.resource_id]
    assert saved["readiness"] == "MISSING_EXECUTABLE"


def test_saved_missing_executable_is_rechecked_and_selectable_once_installed(tmp_path):
    """A test run without the CLIs on PATH saved MISSING_EXECUTABLE for every worker,
    and `howlplane route` (no probe on start) then excluded them all indefinitely."""
    path = tmp_path / "capacity.json"
    profile = make_profile("resource")
    _save_missing_executable(path, profile)
    saved_bytes = path.read_bytes()

    installed = CountingBackend("resource", ReadinessStatus.READY)
    registry = AgentRegistry(agents=[profile])
    pool = ProviderPoolManager(
        registry=registry,
        resources={"resource": ProviderResourceSettings(enabled=True)},
        backend_resolver=lambda _rid: installed,
        state_path=path,
        probe_on_start=False,
        read_only=True,
    )
    decision = pool.select_resource(make_task(), role="implementation")

    assert decision.selected is not None and decision.selected.resource_id == "resource"
    assert installed.probe_calls == 1
    assert path.read_bytes() == saved_bytes, "a read-only pool rewrote the capacity file"


def test_saved_missing_executable_stays_excluded_when_recheck_confirms_it(tmp_path):
    path = tmp_path / "capacity.json"
    profile = make_profile("resource")
    _save_missing_executable(path, profile)

    still_missing = CountingBackend("resource", ReadinessStatus.MISSING_EXECUTABLE)
    pool = make_pool(
        [profile], backends={"resource": still_missing}, state_path=path, probe_on_start=False
    )
    decision = pool.select_resource(make_task(), role="implementation")

    assert still_missing.probe_calls == 1
    assert decision.status == ResourceSelectionStatus.BLOCKED
    assert decision.exclusion_for("resource").reason == "MISSING_EXECUTABLE"


def test_saved_missing_executable_is_not_probed_for_a_no_egress_task(tmp_path):
    path = tmp_path / "capacity.json"
    profile = make_profile("hosted")
    _save_missing_executable(path, profile)

    backend = CountingBackend("hosted", ReadinessStatus.READY)
    pool = make_pool([profile], backends={"hosted": backend}, state_path=path, probe_on_start=False)
    decision = pool.select_resource(make_task(no_egress=True), role="implementation")

    assert backend.probe_calls == 0
    assert decision.exclusion_for("hosted").reason == "TASK_EGRESS_FORBIDDEN"


def test_saved_auth_required_is_not_cleared_by_a_readiness_recheck(tmp_path):
    path = tmp_path / "capacity.json"
    profile = make_profile("expired")
    pool = make_pool([profile], state_path=path)
    pool.record_result("expired", failed_result(
        "expired", "Failed to authenticate: OAuth session expired and could not be refreshed"
    ))

    backend = CountingBackend("expired", ReadinessStatus.READY)
    restarted = make_pool(
        [profile], backends={"expired": backend}, state_path=path, probe_on_start=False
    )
    decision = restarted.select_resource(make_task(), role="implementation")

    assert backend.probe_calls == 0
    assert decision.exclusion_for("expired").reason == "AUTH_REQUIRED"


def test_launch_recorded_missing_executable_is_rechecked_once_reinstalled(tmp_path):
    """A launch that found no binary records the status, not the readiness
    (issues.md #22); the resource must not stay excluded after reinstalling."""
    reinstalled = CountingBackend("resource", ReadinessStatus.READY)
    pool = make_pool([make_profile("resource")], backends={"resource": reinstalled},
                     state_path=tmp_path / "capacity.json")
    state = pool.get_resource_status("resource")
    state.status = ProviderAvailabilityStatus.MISSING_EXECUTABLE
    assert state.readiness == ReadinessStatus.READY

    decision = pool.select_resource(make_task(), role="implementation")

    assert reinstalled.probe_calls == 2  # once on start, once to recheck
    assert decision.selected is not None and decision.selected.resource_id == "resource"


@pytest.mark.parametrize("role", ["implementation", "correctness-reviewer"])
def test_role_exclusion_expires_and_success_clears_it(role):
    pool = make_pool([make_profile("resource")])
    result = failed_result("resource", "permission denied")
    pool.record_result("resource", result, role=role)
    state = pool._provider_states["resource"]
    assert role in state.role_exclusions
    assert state.role_exclusion_set_at[role] == state.last_failure_at
    assert pool.check_capacity_exclusion("resource", role) is not None
    assert pool.select_resource(make_task(), role=role).selected is None
    for excluded_role in state.role_exclusions:
        state.role_exclusion_set_at[excluded_role] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
    assert pool.check_capacity_exclusion("resource", role) is None
    assert pool.select_resource(make_task(), role=role).selected.resource_id == "resource"
    result.success = True
    pool.record_result("resource", result, role=role)
    assert not state.role_exclusions
    assert not state.role_exclusion_set_at


@pytest.mark.parametrize("role", ["review", "correctness-reviewer", "test-falsifier"])
def test_review_success_clears_umbrella_exclusion(role):
    pool = make_pool([make_profile("resource")])
    state = pool._provider_states["resource"]
    state.role_exclusions = {"review": "EXECUTION_PERMISSION_REQUIRED"}
    state.role_exclusion_set_at = {"review": datetime.now(timezone.utc).isoformat()}
    result = failed_result("resource", "")
    result.success = True
    pool.record_result("resource", result, role=role)
    assert not state.role_exclusions
    assert not state.role_exclusion_set_at


@pytest.mark.parametrize("age_hours,blocked", [(1, True), (25, False), (None, False)])
def test_old_capacity_role_exclusion_uses_failure_time(tmp_path, age_hours, blocked):
    path = tmp_path / "capacity.json"
    state = {
        "agent_id": "resource", "name": "resource",
        "status": "AVAILABLE", "readiness": "READY",
        "role_exclusions": {"implementation": "EXECUTION_PERMISSION_REQUIRED"},
    }
    if age_hours is not None:
        state["last_failure_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=age_hours)
        ).isoformat()
    path.write_text(json.dumps({
        "schema": "howlplane.provider_capacity/v1",
        "resources": {"resource": state},
    }))
    pool = make_pool([make_profile("resource")], state_path=path, probe_on_start=False)
    assert (pool.check_capacity_exclusion("resource", "implementation") is not None) == blocked
    decision = pool.select_resource(make_task(), role="implementation")
    assert (decision.selected is None) == blocked


@pytest.mark.parametrize("cooldown,blocked", [(60, False), (7200, True)])
def test_role_exclusion_cooldown_and_timestamp_survive_restart(tmp_path, cooldown, blocked):
    path = tmp_path / "capacity.json"
    profiles = [make_profile("resource")]
    pool = make_pool(profiles, state_path=path)
    pool.record_result("resource", failed_result("resource", "permission denied"),
                       role="implementation")
    state = pool._provider_states["resource"]
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state.role_exclusion_set_at["implementation"] = timestamp
    pool._persist()
    # A different failure must not restart an existing role's cooldown.
    pool.record_result("resource", failed_result("resource", "tests failed"))
    restarted = make_pool(profiles, state_path=path, probe_on_start=False,
                          role_exclusion_cooldown_seconds=cooldown)
    assert restarted._provider_states["resource"].role_exclusion_set_at == {
        "implementation": timestamp,
    }
    assert (restarted.select_resource(make_task(), role="implementation").selected is None) == blocked


@pytest.mark.parametrize("age_hours,blocked", [(23, True), (25, False), (-240, False)])
def test_default_role_cooldown(age_hours, blocked):
    pool = make_pool([make_profile("resource")])
    assert pool.role_exclusion_cooldown_seconds == 86400
    state = pool._provider_states["resource"]
    state.role_exclusions = {"implementation": "EXECUTION_PERMISSION_REQUIRED"}
    state.role_exclusion_set_at = {"implementation": (
        datetime.now(timezone.utc) - timedelta(hours=age_hours)
    ).isoformat()}
    assert (pool.check_capacity_exclusion("resource", "implementation") is not None) == blocked
    assert (pool.select_resource(make_task(), role="implementation").selected is None) == blocked


@pytest.mark.parametrize("last_failure", [None, "2000-01-01T00:00:00+00:00"])
@pytest.mark.parametrize("set_at", [None, {"review": "2000-01-01T00:00:00+00:00"}, []])
def test_legacy_exclusion_not_revived_by_unrelated_failure(tmp_path, last_failure, set_at):
    path = tmp_path / "capacity.json"
    payload = {
        "agent_id": "resource", "name": "resource", "status": "AVAILABLE",
        "readiness": "READY", "last_failure_at": last_failure,
        "role_exclusions": {"implementation": "EXECUTION_PERMISSION_REQUIRED"},
        "role_exclusion_set_at": set_at,
    }
    path.write_text(json.dumps({
        "schema": "howlplane.provider_capacity/v1",
        "resources": {"resource": payload},
    }))
    pool = make_pool([make_profile("resource")], state_path=path, probe_on_start=False)
    assert pool.check_capacity_exclusion("resource", "implementation") is None
    pool.record_result("resource", failed_result("resource", "unit tests failed"), role="review")
    assert pool.check_capacity_exclusion("resource", "implementation") is None
    restarted = make_pool([make_profile("resource")], state_path=path, probe_on_start=False)
    assert restarted.check_capacity_exclusion("resource", "implementation") is None


@pytest.mark.parametrize("timestamp,blocked", [
    ("garbage", False), (123, False), ("2999-01-01T00:00:00", False),
    (None, False), ("", False),
])
def test_invalid_role_timestamp_expires(timestamp, blocked):
    pool = make_pool([make_profile("resource")])
    state = pool._provider_states["resource"]
    state.role_exclusions = {"implementation": "EXECUTION_PERMISSION_REQUIRED"}
    state.last_failure_at = datetime.now(timezone.utc).isoformat()
    state.role_exclusion_set_at = {"implementation": timestamp}
    assert (pool.check_capacity_exclusion("resource", "implementation") is not None) == blocked


def test_naive_role_timestamp_assumes_utc():
    pool = make_pool([make_profile("resource")])
    state = pool._provider_states["resource"]
    state.role_exclusions = {"implementation": "EXECUTION_PERMISSION_REQUIRED"}
    state.role_exclusion_set_at = {"implementation": (
        datetime.now(timezone.utc) - timedelta(hours=23)
    ).replace(tzinfo=None).isoformat()}
    assert pool.check_capacity_exclusion("resource", "implementation") is not None


@pytest.mark.parametrize("cooldown", [-1, float("nan"), float("inf"), -float("inf")])
def test_invalid_role_cooldown_rejected(cooldown):
    with pytest.raises(ValueError):
        make_pool([make_profile("resource")], role_exclusion_cooldown_seconds=cooldown)


def test_zero_role_cooldown_never_blocks():
    pool = make_pool([make_profile("resource")], role_exclusion_cooldown_seconds=0)
    pool.record_result("resource", failed_result("resource", "permission denied"), role="implementation")
    assert pool.check_capacity_exclusion("resource", "implementation") is None


def test_permission_refailure_restarts_expired_role_cooldown():
    pool = make_pool([make_profile("resource")])
    result = failed_result("resource", "permission denied")
    pool.record_result("resource", result, role="implementation")
    state = pool._provider_states["resource"]
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state.role_exclusion_set_at["implementation"] = old
    assert pool.check_capacity_exclusion("resource", "implementation") is None
    before = datetime.now(timezone.utc)
    pool.record_result("resource", result, role="implementation")
    assert datetime.fromisoformat(state.role_exclusion_set_at["implementation"]) >= before
    assert pool.check_capacity_exclusion("resource", "implementation") is not None


@pytest.mark.parametrize("review_age,role_age,expected", [(25, 1, "specific"), (1, 25, "umbrella"), (25, 25, None)])
def test_review_exclusions_have_independent_clocks(review_age, role_age, expected):
    pool = make_pool([make_profile("resource")])
    state = pool._provider_states["resource"]
    state.role_exclusions = {"review": "umbrella", "correctness-reviewer": "specific"}
    state.role_exclusion_set_at = {
        role: (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
        for role, age in [("review", review_age), ("correctness-reviewer", role_age)]
    }
    reason = pool.check_capacity_exclusion("resource", "correctness-reviewer")
    assert reason == (f"ROLE_EXCLUDED:{expected}" if expected else None)
    assert (pool.select_resource(make_task(), role="correctness-reviewer").selected is None) == bool(expected)


def test_implementation_success_preserves_review_exclusion():
    pool = make_pool([make_profile("resource")])
    pool.record_result("resource", failed_result("resource", "permission denied"), role="review")
    state = pool._provider_states["resource"]
    timestamps = dict(state.role_exclusion_set_at)
    result = failed_result("resource", "")
    result.success = True
    pool.record_result("resource", result, role="implementation")
    assert state.role_exclusion_set_at == timestamps
    assert pool.check_capacity_exclusion("resource", "review") is not None


def test_reset_clears_role_exclusions_and_timestamps():
    pool = make_pool([make_profile("resource")])
    pool.record_result("resource", failed_result("resource", "permission denied"), role="review")
    state = pool.reset_resource("resource", reprobe=False)
    assert state.role_exclusions == {}
    assert state.role_exclusion_set_at == {}


def test_status_normalizes_invalid_timestamp_map():
    state = ProviderStatus.from_dict({
        "agent_id": "resource", "name": "resource", "role_exclusion_set_at": [],
    })
    assert state.role_exclusion_set_at == {}


@pytest.mark.parametrize("age_hours,blocked", [(1, True), (25, False)])
def test_partial_legacy_timestamp_map_uses_original_failure(age_hours, blocked):
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    state = ProviderStatus.from_dict({
        "agent_id": "resource", "name": "resource", "status": "AVAILABLE",
        "role_exclusions": {"implementation": "EXECUTION_PERMISSION_REQUIRED"},
        "role_exclusion_set_at": {"review": "2000-01-01T00:00:00+00:00"},
        "last_failure_at": timestamp,
    })
    pool = make_pool([make_profile("resource")])
    pool._provider_states["resource"] = state
    assert state.role_exclusion_set_at["implementation"] == timestamp
    pool.record_result("resource", failed_result("resource", "tests failed"), role="review")
    assert state.role_exclusion_set_at["implementation"] == timestamp
    assert (pool.check_capacity_exclusion("resource", "implementation") is not None) == blocked


@pytest.mark.parametrize("last_failure", [None, "2000-01-01T00:00:00+00:00"])
def test_runtime_legacy_exclusion_not_refreshed(last_failure):
    pool = make_pool([make_profile("resource")])
    state = pool._provider_states["resource"]
    state.role_exclusions = {"implementation": "EXECUTION_PERMISSION_REQUIRED"}
    state.last_failure_at = last_failure
    pool.record_result("resource", failed_result("resource", "tests failed"), role="review")
    assert pool.check_capacity_exclusion("resource", "implementation") is None
