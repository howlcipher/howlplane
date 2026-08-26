#!/usr/bin/env python3
"""Shared configurable AI resource pool, capacity, and selection authority."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    LAUNCH_OUTCOME_NOT_INSTALLED,
    LAUNCH_OUTCOME_SPAWN_FAILED,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.resource_models import (
    AuthenticationStatus,
    BackendReadiness,
    CognitiveRecommendation,
    EconomicClass,
    ProviderFailureClass,
    ReadinessStatus,
    ResourceExclusion,
    ResourceIdentity,
    ResourceLocality,
    ResourceSelectionDecision,
    ResourceSelectionStatus,
)
from src.control_plane.task_spec import DataClassSerializationMixin, TaskSpec
from src.infrastructure.config_loader import (
    ProviderPolicySettings,
    ProviderResourceSettings,
)

PROVIDER_POOL_SCHEMA_VERSION = "howlplane.provider_pool/v2"
PROVIDER_CAPACITY_SCHEMA_VERSION = "howlplane.provider_capacity/v1"

LOCAL_PROVIDER_IDS = {"local_ollama", "ollama_local"}
LOCAL_INELIGIBLE_TASK_CLASSES = {"security_patch", "infrastructure"}
LOCAL_INELIGIBLE_SKILLS = {
    "cyber_security",
    "blue_team",
    "red_team",
    "bug_bounty_hunter",
    "database_management",
    "devops_sre",
    "network_engineering",
}
LOCAL_INELIGIBLE_REVIEWER_ROLES = {"security-reviewer"}

TASK_SUITABILITY_PREFERENCES: Dict[str, List[str]] = {
    "routine": ["agy", "codex", "devin_cli", "claude_code", "local_ollama"],
    "code_heavy": ["codex", "agy", "devin_cli", "claude_code", "local_ollama"],
    "large_autonomous": ["devin_cli", "codex", "agy", "claude_code"],
    "architecture_security": ["claude_code", "codex", "agy", "devin_cli"],
}

EXHAUSTION_PATTERNS: Dict[str, List[str]] = {
    "claude_code": [
        "usage limit reached",
        "rate limit exceeded",
        "quota exceeded",
        "credit limit",
        "429 too many requests",
        "overloaded_error",
        "insufficient_quota",
    ],
    "codex": [
        "session limit reached",
        "rate_limit_exceeded",
        "insufficient_quota",
        "quota exceeded",
        "out of capacity",
        "usage limit",
    ],
    "agy": [
        "quota exhausted",
        "rate limit",
        "resource exhausted",
        "resource_exhausted",
        "token limit",
        "exceeded your current quota",
        "individual quota reached",
    ],
    "devin_cli": [
        "session limit",
        "quota unavailable",
        "rate limited",
        "credits exhausted",
        "insufficient funds",
    ],
    "local_ollama": ["connection refused", "not running", "server unavailable"],
}


def is_task_local_eligible(task: Optional[TaskSpec]) -> bool:
    """Returns whether existing low risk local participation policy permits a task."""
    if task is None or task.risk_level != "low":
        return False
    if task.task_class in LOCAL_INELIGIBLE_TASK_CLASSES:
        return False
    return not set(task.required_skills or []).intersection(LOCAL_INELIGIBLE_SKILLS)


class ProviderAvailabilityStatus(str, Enum):
    """Shared current capacity and availability states."""

    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    SESSION_EXHAUSTED = "SESSION_EXHAUSTED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    MISSING_EXECUTABLE = "MISSING_EXECUTABLE"
    UNREACHABLE = "UNREACHABLE"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    RESOURCE_CONSTRAINED = "RESOURCE_CONSTRAINED"
    UNKNOWN = "UNKNOWN"


class ProviderConfigurationError(ValueError):
    """Raised when operator resource configuration is invalid."""


@dataclass
class ProviderExhaustionEvent(DataClassSerializationMixin):
    """Observed availability failure retained for legacy and new consumers."""

    agent_id: str
    failure_type: str
    raw_error: str
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    task_id: Optional[str] = None


@dataclass
class ProviderStatus(DataClassSerializationMixin):
    """Durable state for one configured resource identity."""

    agent_id: str
    name: str
    provider_id: Optional[str] = None
    interface_id: Optional[str] = None
    resource_id: Optional[str] = None
    model_id: Optional[str] = None
    status: ProviderAvailabilityStatus = ProviderAvailabilityStatus.UNKNOWN
    readiness: ReadinessStatus = ReadinessStatus.NOT_PROBED
    authentication: AuthenticationStatus = AuthenticationStatus.UNKNOWN
    last_checked: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    observed_at: Optional[str] = None
    consecutive_failures: int = 0
    exhaustion_event: Optional[ProviderExhaustionEvent] = None
    success_count: int = 0
    metered_invocation_count: int = 0
    total_duration_seconds: float = 0.0
    unavailable_reason: Optional[str] = None
    normalized_failure_class: Optional[str] = None
    evidence: Optional[str] = None
    retry_after: Optional[str] = None
    reset_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderStatus":
        payload = dict(data)
        payload["status"] = ProviderAvailabilityStatus(
            payload.get("status", ProviderAvailabilityStatus.UNKNOWN)
        )
        payload["readiness"] = ReadinessStatus(
            payload.get("readiness", ReadinessStatus.NOT_PROBED)
        )
        payload["authentication"] = AuthenticationStatus(
            payload.get("authentication", AuthenticationStatus.UNKNOWN)
        )
        event = payload.get("exhaustion_event")
        if isinstance(event, dict):
            payload["exhaustion_event"] = ProviderExhaustionEvent.from_dict(event)
        valid = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in valid})


class ProviderCapacityStore:
    """Atomic shared capacity state used across commands and orchestration roles."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> Dict[str, ProviderStatus]:
        if not self.path.is_file():
            return {}
        payload = safe_load_json(self.path)
        if payload.get("schema") != PROVIDER_CAPACITY_SCHEMA_VERSION:
            raise ProviderConfigurationError("Unsupported provider capacity schema.")
        resources = payload.get("resources", {})
        if not isinstance(resources, dict):
            raise ProviderConfigurationError("Provider capacity resources must be a mapping.")
        return {
            resource_id: ProviderStatus.from_dict(status)
            for resource_id, status in resources.items()
            if isinstance(status, dict)
        }

    def save(self, states: Dict[str, ProviderStatus]) -> Path:
        return atomic_write_json(
            self.path,
            {
                "schema": PROVIDER_CAPACITY_SCHEMA_VERSION,
                "resources": {
                    key: value.to_dict() for key, value in sorted(states.items())
                },
            },
        )


class ProviderPoolManager:
    """Single owner for resource readiness, capacity, and final selection."""

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        avoid_provider: Optional[str] = None,
        fallback_provider: Optional[str] = None,
        resources: Optional[Dict[str, ProviderResourceSettings]] = None,
        policy: Optional[ProviderPolicySettings] = None,
        operating_mode: str = "connected",
        backend_resolver: Optional[Callable[[str], AgentBackend]] = None,
        state_path: Optional[Union[str, Path]] = None,
        probe_on_start: bool = True,
        read_only: bool = False,
    ):
        self.registry = registry or AgentRegistry()
        self.avoid_provider = avoid_provider
        self.fallback_provider = fallback_provider
        self.policy = policy or ProviderPolicySettings()
        self.operating_mode = operating_mode
        self._backend_resolver = backend_resolver or AgentBackendRegistry.get_backend
        self._legacy_mode = resources is None
        configured = resources if resources is not None else {
            profile.resource_id: ProviderResourceSettings(enabled=True)
            for profile in self.registry.list_resources()
        }
        self.resources = self._normalize_resources(configured)
        self.read_only = read_only
        self._store = ProviderCapacityStore(state_path) if state_path else None
        self._provider_states: Dict[str, ProviderStatus] = (
            self._store.load() if self._store else {}
        )
        self._validate_configuration()
        self._initialize_states(probe_on_start=probe_on_start)

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        registry: Optional[AgentRegistry] = None,
        state_path: Optional[Union[str, Path]] = None,
        read_only: bool = False,
        probe_on_start: bool = True,
    ) -> "ProviderPoolManager":
        """Builds the production pool with deterministic legacy migration."""
        actual_registry = registry or AgentRegistry()
        configured = dict(settings.providers)
        if not configured:
            if settings.operating_mode == "local_only":
                configured = {"local_ollama": ProviderResourceSettings(enabled=True)}
            else:
                configured = {
                    profile.resource_id: ProviderResourceSettings(enabled=True)
                    for profile in actual_registry.list_resources()
                }
        return cls(
            registry=actual_registry,
            resources=configured,
            policy=settings.provider_policy,
            operating_mode=settings.operating_mode,
            state_path=state_path,
            read_only=read_only,
            probe_on_start=probe_on_start,
        )

    @classmethod
    def from_config(
        cls,
        *,
        read_only: bool = False,
        probe_on_start: bool = True,
    ) -> "ProviderPoolManager":
        """Builds a pool from canonical plus operator-local configuration."""
        from src.infrastructure.config_loader import default_loader

        capacity_path = Path.home() / ".config" / "howlplane" / "provider_capacity.json"
        return cls.from_settings(
            default_loader.settings,
            state_path=capacity_path,
            read_only=read_only,
            probe_on_start=probe_on_start,
        )

    def _normalize(self, resource_id: str) -> str:
        return AgentBackendRegistry.normalize_agent_id(resource_id)

    def _normalize_resources(
        self,
        resources: Dict[str, ProviderResourceSettings],
    ) -> Dict[str, ProviderResourceSettings]:
        normalized: Dict[str, ProviderResourceSettings] = {}
        for supplied_id, resource_config in resources.items():
            resource_id = self._normalize(supplied_id)
            if resource_id in normalized:
                raise ProviderConfigurationError(
                    f"Duplicate resource definition after alias normalization: {resource_id}"
                )
            normalized[resource_id] = (
                resource_config
                if isinstance(resource_config, ProviderResourceSettings)
                else ProviderResourceSettings.model_validate(resource_config)
            )
        return normalized

    def _validate_configuration(self) -> None:
        if self.operating_mode not in ("local_only", "connected"):
            raise ProviderConfigurationError(
                f"Invalid operating mode: {self.operating_mode}"
            )
        known = {
            profile.resource_id or profile.agent_id
            for profile in self.registry.list_resources()
        }
        unknown = sorted(set(self.resources).difference(known))
        if unknown:
            raise ProviderConfigurationError(
                f"Unknown configured resource ID(s): {', '.join(unknown)}"
            )
        preferences = self.policy.preferred_external + self.policy.preferred_local
        unknown_preferences = sorted(
            {self._normalize(item) for item in preferences}.difference(known)
        )
        if unknown_preferences:
            raise ProviderConfigurationError(
                "Unknown preferred resource ID(s): " + ", ".join(unknown_preferences)
            )
        for resource_id, resource_config in self.resources.items():
            profile = self.registry.get_resource(resource_id)
            if profile is None:
                continue
            if (
                resource_config.interface_id
                and resource_config.interface_id != profile.interface_id
            ):
                raise ProviderConfigurationError(
                    f"Invalid interface reference for resource '{resource_id}'."
                )
            if resource_config.model_id:
                if profile.model_id and resource_config.model_id != profile.model_id:
                    if not profile.model_configurable:
                        raise ProviderConfigurationError(
                            f"Invalid model reference for resource '{resource_id}'."
                        )
                elif profile.model_id is None and not profile.model_configurable:
                    raise ProviderConfigurationError(
                        f"Model identity is not configurable for resource '{resource_id}'."
                    )

    def _new_status(self, profile: AgentProfile) -> ProviderStatus:
        identity = profile.resource_identity()
        return ProviderStatus(
            agent_id=profile.agent_id,
            name=profile.name,
            provider_id=identity.provider_id,
            interface_id=identity.interface_id,
            resource_id=identity.resource_id,
            model_id=identity.model_id,
        )

    def _resource_identity(self, profile: AgentProfile) -> ResourceIdentity:
        """Returns configured/observed identity without mutating the profile."""
        base = profile.resource_identity()
        state = self._provider_states.get(base.resource_id)
        return ResourceIdentity(
            provider_id=base.provider_id,
            interface_id=base.interface_id,
            resource_id=base.resource_id,
            model_id=state.model_id if state is not None else base.model_id,
        )

    def _initialize_states(self, *, probe_on_start: bool) -> None:
        known_ids: Set[str] = set()
        for profile in self.registry.list_resources():
            resource_id = profile.resource_id or profile.agent_id
            known_ids.add(resource_id)
            state = self._provider_states.get(resource_id) or self._new_status(profile)
            self._provider_states[resource_id] = state
            resource_config = self.resources.get(resource_id)
            if resource_config is None:
                self._disable_without_probe(state, "NOT_CONFIGURED")
                continue
            if not resource_config.enabled:
                self._disable_without_probe(state, "OPERATOR_DISABLED")
                continue
            if self._egress_forbidden(profile):
                self._disable_without_probe(state, "EGRESS_FORBIDDEN")
                continue
            if resource_config.model_id:
                state.model_id = resource_config.model_id
            if probe_on_start:
                self._apply_readiness(resource_id, profile, state)
        self._provider_states = {
            key: value for key, value in self._provider_states.items() if key in known_ids
        }
        self._persist()

    def _disable_without_probe(self, state: ProviderStatus, reason: str) -> None:
        state.status = ProviderAvailabilityStatus.DISABLED
        state.readiness = ReadinessStatus.NOT_PROBED
        state.unavailable_reason = reason
        state.observed_at = datetime.now(timezone.utc).isoformat()

    def _egress_forbidden(self, profile: AgentProfile) -> bool:
        return (
            self.operating_mode == "local_only"
            and profile.locality != ResourceLocality.LOCAL.value
        )

    def _apply_readiness(
        self,
        resource_id: str,
        profile: AgentProfile,
        state: ProviderStatus,
    ) -> BackendReadiness:
        backend = self._backend_resolver(resource_id)
        readiness = backend.probe_readiness()
        now = datetime.now(timezone.utc).isoformat()
        state.readiness = ReadinessStatus(readiness.status)
        state.authentication = AuthenticationStatus(readiness.authentication)
        state.unavailable_reason = readiness.reason
        state.evidence = readiness.evidence
        state.last_checked = now
        state.observed_at = now
        failure_states = {
            ReadinessStatus.MISSING_EXECUTABLE: ProviderAvailabilityStatus.MISSING_EXECUTABLE,
            ReadinessStatus.AUTH_REQUIRED: ProviderAvailabilityStatus.AUTH_REQUIRED,
            ReadinessStatus.UNREACHABLE: ProviderAvailabilityStatus.UNREACHABLE,
            ReadinessStatus.UNAVAILABLE: ProviderAvailabilityStatus.UNAVAILABLE,
        }
        if readiness.status in failure_states:
            state.status = failure_states[readiness.status]
        elif readiness.status == ReadinessStatus.READY:
            retained = {
                ProviderAvailabilityStatus.SESSION_EXHAUSTED,
                ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
                ProviderAvailabilityStatus.RATE_LIMITED,
            }
            if state.status not in retained:
                if self._legacy_mode or profile.locality == ResourceLocality.LOCAL.value:
                    state.status = ProviderAvailabilityStatus.AVAILABLE
                else:
                    state.status = ProviderAvailabilityStatus.UNKNOWN
        return readiness

    def _persist(self) -> None:
        if self._store and not self.read_only:
            self._store.save(self._provider_states)

    def get_status(self, agent_id: str) -> ProviderAvailabilityStatus:
        resource_id = self._normalize(agent_id)
        state = self._provider_states.get(resource_id)
        return state.status if state else ProviderAvailabilityStatus.UNKNOWN

    def get_resource_status(self, resource_id: str) -> Optional[ProviderStatus]:
        return self._provider_states.get(self._normalize(resource_id))

    def set_status(
        self,
        agent_id: str,
        status: ProviderAvailabilityStatus,
        event: Optional[ProviderExhaustionEvent] = None,
    ) -> None:
        resource_id = self._normalize(agent_id)
        state = self._provider_states.get(resource_id)
        if state is None:
            profile = self.registry.get_resource(resource_id)
            state = self._new_status(profile) if profile else ProviderStatus(
                agent_id=resource_id,
                name=resource_id,
                resource_id=resource_id,
            )
            self._provider_states[resource_id] = state
        state.status = ProviderAvailabilityStatus(status)
        if state.status == ProviderAvailabilityStatus.AVAILABLE:
            state.readiness = ReadinessStatus.READY
            state.unavailable_reason = None
        state.last_checked = datetime.now(timezone.utc).isoformat()
        state.observed_at = state.last_checked
        if event:
            state.exhaustion_event = event
        self._persist()

    def get_all_statuses(self) -> Dict[str, str]:
        return {
            resource_id: state.status.value
            for resource_id, state in sorted(self._provider_states.items())
        }

    def inventory(self) -> List[Dict[str, Any]]:
        """Returns registered, configured, readiness, and capacity distinctions."""
        rows = []
        for profile in self.registry.list_resources():
            resource_id = profile.resource_id or profile.agent_id
            resource_config = self.resources.get(resource_id)
            state = self._provider_states[resource_id]
            rows.append({
                "identity": self._resource_identity(profile).to_dict(),
                "name": profile.name,
                "registered": True,
                "configured": resource_config is not None,
                "enabled": bool(resource_config and resource_config.enabled),
                "locality": profile.locality,
                "economic_class": profile.economic_class,
                "readiness": state.readiness.value,
                "authentication": state.authentication.value,
                "capacity": state.status.value,
                "reason": state.unavailable_reason,
                "observed_at": state.observed_at,
                "last_success_at": state.last_success_at,
                "last_failure_at": state.last_failure_at,
                "metered_invocation_count": state.metered_invocation_count,
                "retry_after": state.retry_after,
                "reset_at": state.reset_at,
            })
        return rows

    def has_available_providers(self) -> bool:
        usable = {
            ProviderAvailabilityStatus.AVAILABLE,
            ProviderAvailabilityStatus.DEGRADED,
            ProviderAvailabilityStatus.UNKNOWN,
        }
        return any(state.status in usable for state in self._provider_states.values())

    def is_all_cloud_exhausted(self) -> bool:
        cloud = []
        for profile in self.registry.list_resources():
            if profile.locality == ResourceLocality.LOCAL.value:
                continue
            state = self._provider_states.get(profile.resource_id or profile.agent_id)
            if state and state.status != ProviderAvailabilityStatus.DISABLED:
                cloud.append(state.status)
        unavailable = {
            ProviderAvailabilityStatus.SESSION_EXHAUSTED,
            ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
            ProviderAvailabilityStatus.RATE_LIMITED,
            ProviderAvailabilityStatus.AUTH_REQUIRED,
            ProviderAvailabilityStatus.MISSING_EXECUTABLE,
            ProviderAvailabilityStatus.UNREACHABLE,
            ProviderAvailabilityStatus.UNAVAILABLE,
        }
        return not cloud or all(status in unavailable for status in cloud)

    def is_all_exhausted(self) -> bool:
        active = [
            state.status
            for state in self._provider_states.values()
            if state.status != ProviderAvailabilityStatus.DISABLED
        ]
        exhausted = {
            ProviderAvailabilityStatus.SESSION_EXHAUSTED,
            ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
            ProviderAvailabilityStatus.RATE_LIMITED,
        }
        return bool(active) and all(status in exhausted for status in active)

    def classify_failure(
        self,
        agent_id: str,
        result: AgentExecutionResult,
    ) -> ProviderFailureClass:
        """Normalizes availability separately from engineering results."""
        if result.success:
            return ProviderFailureClass.UNKNOWN
        resource_id = self._normalize(agent_id)
        combined = "\n".join(
            [result.error_message or "", result.stderr or "", result.stdout or ""]
        ).lower()
        # Structural execution evidence outranks transcript text. A long agent
        # transcript is arbitrary third-party content that routinely contains the
        # very phrases these markers look for, so where the harness observed the
        # process directly, that observation wins (SLOPFIX-03).
        metadata = result.metadata or {}
        launch_outcome = metadata.get(LAUNCH_OUTCOME_KEY)
        if launch_outcome in (LAUNCH_OUTCOME_NOT_INSTALLED, LAUNCH_OUTCOME_SPAWN_FAILED):
            return ProviderFailureClass.MISSING_EXECUTABLE
        if metadata.get(TIMEOUT_SOURCE_KEY) == TIMEOUT_SOURCE_HARNESS:
            return ProviderFailureClass.TRANSPORT_UNAVAILABLE
        # A provider that demonstrably started cannot be a missing executable,
        # whatever its session log says about commands that were not found.
        # Backends that stamp no markers keep the older text-based behavior, with
        # one correction: a timeout verdict is the more specific signal and so
        # outranks the substring scan. Exit 127 stays decisive either way, since
        # the shell reserves it for a command it could not execute.
        if launch_outcome != LAUNCH_OUTCOME_LAUNCHED and (
            result.exit_code == 127
            or (not result.timed_out and any(marker in combined for marker in (
                "command not found", "binary not found", "not installed",
            )))
        ):
            return ProviderFailureClass.MISSING_EXECUTABLE
        if any(marker in combined for marker in (
            "authentication required", "not authenticated", "login required",
            "unauthorized", "invalid api key",
        )):
            return ProviderFailureClass.AUTHENTICATION_REQUIRED
        if any(marker in combined for marker in (
            "429 too many requests", "rate_limit", "rate limit", "rate limited",
        )):
            return ProviderFailureClass.RATE_LIMITED
        if any(marker in combined for marker in (
            "quota exhausted", "quota exceeded", "insufficient_quota",
            "credits exhausted", "insufficient funds", "resource exhausted",
        )):
            return ProviderFailureClass.QUOTA_EXHAUSTED
        session_patterns = EXHAUSTION_PATTERNS.get(resource_id, []) + [
            "usage limit",
            "session limit",
            "individual quota reached",
            "quota reached",
            "upgrade your subscription",
            "out of capacity",
        ]
        if any(marker in combined for marker in session_patterns):
            return ProviderFailureClass.SESSION_LIMIT
        if any(marker in combined for marker in (
            "malformed", "invalid json", "invalid yaml", "parse error",
        )):
            return ProviderFailureClass.MALFORMED_OUTPUT
        if result.timed_out or any(marker in combined for marker in (
            "connection refused", "transport unavailable", "network unreachable",
            "timed out", "timeout waiting for response", "request timed out",
            "connection timed out", "gateway timeout", "read timeout",
            "operation timed out", "deadline exceeded", "timeout after",
        )):
            return ProviderFailureClass.TRANSPORT_UNAVAILABLE
        if any(marker in combined for marker in (
            "provider unavailable", "service unavailable", "overloaded_error", "outage",
        )):
            return ProviderFailureClass.PROVIDER_UNAVAILABLE
        return ProviderFailureClass.ENGINEERING_FAILURE

    def record_result(
        self,
        agent_id: str,
        result: AgentExecutionResult,
        task_id: Optional[str] = None,
    ) -> ProviderFailureClass:
        """Updates shared state only when the observed result warrants it."""
        resource_id = self._normalize(agent_id)
        state = self._provider_states.get(resource_id)
        if state is None:
            raise KeyError(f"Unknown resource '{resource_id}'.")
        now = datetime.now(timezone.utc)
        profile = self.registry.get_resource(resource_id)
        if (
            profile is not None
            and profile.economic_class == EconomicClass.METERED_API.value
        ):
            state.metered_invocation_count += 1
            self._persist()
        if result.success:
            state.status = ProviderAvailabilityStatus.AVAILABLE
            state.normalized_failure_class = None
            state.unavailable_reason = None
            state.exhaustion_event = None
            state.success_count += 1
            state.total_duration_seconds += result.duration_seconds
            state.last_success_at = now.isoformat()
            state.last_checked = now.isoformat()
            state.observed_at = now.isoformat()
            state.retry_after = None
            self._persist()
            return ProviderFailureClass.UNKNOWN

        failure_class = self.classify_failure(resource_id, result)
        availability_map = {
            ProviderFailureClass.QUOTA_EXHAUSTED: ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
            ProviderFailureClass.SESSION_LIMIT: ProviderAvailabilityStatus.SESSION_EXHAUSTED,
            ProviderFailureClass.RATE_LIMITED: ProviderAvailabilityStatus.RATE_LIMITED,
            ProviderFailureClass.AUTHENTICATION_REQUIRED: ProviderAvailabilityStatus.AUTH_REQUIRED,
            ProviderFailureClass.PROVIDER_UNAVAILABLE: ProviderAvailabilityStatus.UNAVAILABLE,
            ProviderFailureClass.TRANSPORT_UNAVAILABLE: ProviderAvailabilityStatus.UNREACHABLE,
            ProviderFailureClass.MISSING_EXECUTABLE: ProviderAvailabilityStatus.MISSING_EXECUTABLE,
        }
        if failure_class in availability_map:
            state.status = availability_map[failure_class]
            state.consecutive_failures += 1
            state.last_failure_at = now.isoformat()
            state.last_checked = now.isoformat()
            state.observed_at = now.isoformat()
            state.normalized_failure_class = failure_class.value
            state.unavailable_reason = failure_class.value
            temporary = {
                ProviderFailureClass.SESSION_LIMIT,
                ProviderFailureClass.RATE_LIMITED,
                ProviderFailureClass.TRANSPORT_UNAVAILABLE,
                ProviderFailureClass.PROVIDER_UNAVAILABLE,
            }
            if failure_class in temporary and self.policy.cooldown_seconds:
                state.retry_after = (
                    now + timedelta(seconds=self.policy.cooldown_seconds)
                ).isoformat()
            state.exhaustion_event = ProviderExhaustionEvent(
                agent_id=resource_id,
                failure_type={
                    ProviderFailureClass.SESSION_LIMIT: "session_limit",
                    ProviderFailureClass.RATE_LIMITED: "rate_limit",
                    ProviderFailureClass.QUOTA_EXHAUSTED: "quota_exhausted",
                    ProviderFailureClass.AUTHENTICATION_REQUIRED: "authentication_required",
                }.get(failure_class, "unavailable"),
                raw_error=(result.stderr or result.error_message or failure_class.value).strip(),
                task_id=task_id,
            )
            self._persist()
        return failure_class

    def detect_exhaustion(
        self,
        agent_id: str,
        result: AgentExecutionResult,
        task_id: Optional[str] = None,
    ) -> Optional[ProviderExhaustionEvent]:
        """Compatibility wrapper returning events only for availability failures."""
        resource_id = self._normalize(agent_id)
        if resource_id in LOCAL_PROVIDER_IDS:
            reason = (result.error_message or "").upper()
            if reason == "RESOURCE_CONSTRAINED":
                event = ProviderExhaustionEvent(
                    agent_id=resource_id,
                    failure_type="unavailable",
                    raw_error=result.stderr or reason,
                    task_id=task_id,
                )
                self.set_status(
                    resource_id,
                    ProviderAvailabilityStatus.RESOURCE_CONSTRAINED,
                    event,
                )
                return event
        failure_class = self.record_result(agent_id, result, task_id=task_id)
        availability_classes = {
            ProviderFailureClass.QUOTA_EXHAUSTED,
            ProviderFailureClass.SESSION_LIMIT,
            ProviderFailureClass.RATE_LIMITED,
            ProviderFailureClass.AUTHENTICATION_REQUIRED,
            ProviderFailureClass.PROVIDER_UNAVAILABLE,
            ProviderFailureClass.TRANSPORT_UNAVAILABLE,
            ProviderFailureClass.MISSING_EXECUTABLE,
        }
        if failure_class not in availability_classes:
            return None
        return self._provider_states[self._normalize(agent_id)].exhaustion_event

    def reset_resource(self, resource_id: str, *, reprobe: bool = True) -> ProviderStatus:
        """Clears only current availability state for one configured resource."""
        normalized = self._normalize(resource_id)
        profile = self.registry.get_resource(normalized)
        config = self.resources.get(normalized)
        if profile is None or config is None:
            raise ProviderConfigurationError(f"Resource '{resource_id}' is not configured.")
        if not config.enabled:
            raise ProviderConfigurationError(f"Resource '{resource_id}' is operator disabled.")
        state = self._provider_states[normalized]
        state.status = ProviderAvailabilityStatus.UNKNOWN
        state.normalized_failure_class = None
        state.exhaustion_event = None
        state.retry_after = None
        state.reset_at = datetime.now(timezone.utc).isoformat()
        state.unavailable_reason = None
        if reprobe and not self._egress_forbidden(profile):
            self._apply_readiness(normalized, profile, state)
        self._persist()
        return state

    def reset_transient_exhaustion(self) -> None:
        """Compatibility reset using bounded non-generative readiness probes."""
        for resource_id, resource_config in self.resources.items():
            if resource_config.enabled:
                profile = self.registry.get_resource(resource_id)
                if profile and not self._egress_forbidden(profile):
                    self.reset_resource(resource_id, reprobe=True)

    def _task_forbids_egress(self, task: TaskSpec) -> bool:
        if task.metadata.get("allow_egress") is False:
            return True
        combined = " ".join(task.constraints).lower()
        return "no egress" in combined or "zero external" in combined

    def _required_capabilities(self, task: TaskSpec, role: str) -> List[str]:
        required = set(task.metadata.get("required_resource_capabilities", []))
        if role in ("implementation", "remediation"):
            required.update({"file_editing", "repository_access"})
        elif self._is_review_role(role):
            required.add("code_review")
        else:
            required.add("code_generation")
        if "terminal_execution" in task.allowed_tools:
            required.add("command_execution")
        return sorted(required)

    @staticmethod
    def _is_review_role(role: str) -> bool:
        return (
            role == "review"
            or role.endswith("-reviewer")
            or role == "test-falsifier"
        )

    @staticmethod
    def _has_capability(profile: AgentProfile, capability: str) -> bool:
        facts = {
            "repository_access": profile.supports_repository_access,
            "command_execution": profile.supports_command_execution,
            "structured_output": profile.supports_structured_output,
            "tool_calling": profile.supports_tool_calling,
        }
        return facts.get(capability, capability in profile.capabilities)

    @staticmethod
    def _capacity_exclusion(status: ProviderAvailabilityStatus) -> Optional[str]:
        blocked = {
            ProviderAvailabilityStatus.RATE_LIMITED,
            ProviderAvailabilityStatus.SESSION_EXHAUSTED,
            ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
            ProviderAvailabilityStatus.AUTH_REQUIRED,
            ProviderAvailabilityStatus.MISSING_EXECUTABLE,
            ProviderAvailabilityStatus.UNREACHABLE,
            ProviderAvailabilityStatus.UNAVAILABLE,
            ProviderAvailabilityStatus.RESOURCE_CONSTRAINED,
        }
        return status.value if status in blocked else None

    def _recover_capacity_if_due(
        self,
        resource_id: str,
        profile: AgentProfile,
        state: ProviderStatus,
    ) -> None:
        """Performs at most one safe re-probe after an observed cooldown."""
        if not state.retry_after:
            return
        try:
            due = datetime.fromisoformat(state.retry_after)
        except ValueError:
            return
        if datetime.now(timezone.utc) < due:
            return
        state.retry_after = None
        state.exhaustion_event = None
        state.normalized_failure_class = None
        state.status = ProviderAvailabilityStatus.UNKNOWN
        if self.read_only:
            state.readiness = ReadinessStatus.NOT_PROBED
            return
        self._apply_readiness(resource_id, profile, state)
        self._persist()

    def _recommend(
        self,
        task: TaskSpec,
        role: str,
        candidates: List[AgentProfile],
    ) -> CognitiveRecommendation:
        from src.control_plane.router import TaskRouter

        preferences = [
            self._normalize(item)
            for item in self.policy.preferred_external + self.policy.preferred_local
        ]
        return TaskRouter(self.registry).recommend_resource(
            task,
            candidates,
            role=role,
            operator_preferences=preferences,
        )

    def _ranking_key(
        self,
        profile: AgentProfile,
        recommendation: CognitiveRecommendation,
        avoid_resource_id: Optional[str],
    ) -> Tuple[int, int, int, int, str]:
        economic = EconomicClass(profile.economic_class or EconomicClass.UNKNOWN)
        if self.policy.subscription_first:
            economic_rank = {
                EconomicClass.SUBSCRIPTION: 0,
                EconomicClass.LOCAL: 1,
                EconomicClass.UNKNOWN: 2,
                EconomicClass.METERED_API: 3,
            }[economic]
        elif self.policy.external_before_local:
            economic_rank = 1 if economic == EconomicClass.LOCAL else 0
        else:
            economic_rank = 0
        state = self._provider_states[profile.resource_id or profile.agent_id]
        capacity_rank = {
            ProviderAvailabilityStatus.AVAILABLE: 0,
            ProviderAvailabilityStatus.DEGRADED: 1,
            ProviderAvailabilityStatus.UNKNOWN: 2,
        }.get(state.status, 3)
        cognitive_rank = 0 if profile.resource_id == recommendation.resource_id else 1
        avoid_rank = 1 if profile.resource_id == avoid_resource_id else 0
        return (
            economic_rank,
            capacity_rank if self.policy.prefer_existing_capacity else 0,
            cognitive_rank,
            avoid_rank,
            profile.resource_id or profile.agent_id,
        )

    def select_resource(
        self,
        task: TaskSpec,
        *,
        role: str,
        explicit_resource_id: Optional[str] = None,
        avoid_resource_id: Optional[str] = None,
        exclude_resource_ids: Optional[Iterable[str]] = None,
    ) -> ResourceSelectionDecision:
        """Applies hard filtering, economics, recommendation, and stable tie break.

        ``avoid_resource_id`` is a soft preference that only demotes a resource in
        the ranking. ``exclude_resource_ids`` is a hard prohibition, used by
        bounded failover so a resource whose cooldown elapsed mid-attempt cannot
        be re-offered and dead-end the loop.
        """
        required = self._required_capabilities(task, role)
        excluded_ids = {
            self._normalize(item) for item in (exclude_resource_ids or []) if item
        }
        registered = [
            self._resource_identity(profile) for profile in self.registry.list_resources()
        ]
        exclusions: List[ResourceExclusion] = []
        candidates: List[AgentProfile] = []
        task_forbids_egress = self._task_forbids_egress(task)

        def exclude(profile: AgentProfile, reason: str, stage: str, detail=None):
            exclusions.append(ResourceExclusion(
                resource_id=profile.resource_id or profile.agent_id,
                reason=reason,
                stage=stage,
                detail=detail,
            ))

        for profile in self.registry.list_resources():
            resource_id = profile.resource_id or profile.agent_id
            resource_config = self.resources.get(resource_id)
            state = self._provider_states[resource_id]
            if resource_config is None:
                exclude(profile, "NOT_CONFIGURED", "operator_permission")
                continue
            if not resource_config.enabled:
                exclude(profile, "OPERATOR_DISABLED", "operator_permission")
                continue
            if self._egress_forbidden(profile):
                exclude(profile, "EGRESS_FORBIDDEN", "egress_policy")
                continue
            if task_forbids_egress and profile.locality != ResourceLocality.LOCAL.value:
                exclude(profile, "TASK_EGRESS_FORBIDDEN", "egress_policy")
                continue
            self._recover_capacity_if_due(resource_id, profile, state)
            if state.readiness in {
                ReadinessStatus.MISSING_EXECUTABLE,
                ReadinessStatus.AUTH_REQUIRED,
                ReadinessStatus.UNREACHABLE,
                ReadinessStatus.UNAVAILABLE,
            }:
                exclude(profile, state.readiness.value, "runtime_readiness")
                continue
            if role not in profile.roles and not (
                self._is_review_role(role) and "review" in profile.roles
            ):
                exclude(profile, "ROLE_NOT_SUPPORTED", "capability")
                continue
            missing = [cap for cap in required if not self._has_capability(profile, cap)]
            if missing:
                exclude(
                    profile,
                    "MISSING_REQUIRED_CAPABILITY",
                    "capability",
                    ",".join(missing),
                )
                continue
            if role in LOCAL_INELIGIBLE_REVIEWER_ROLES and profile.locality == ResourceLocality.LOCAL.value:
                exclude(profile, "LOCAL_REVIEW_AUTHORITY_FORBIDDEN", "authority")
                continue
            capacity_reason = self._capacity_exclusion(state.status)
            if capacity_reason:
                exclude(profile, capacity_reason, "capacity")
                continue
            if resource_id in excluded_ids:
                exclude(profile, "ALREADY_ATTEMPTED", "failover_policy")
                continue
            economics = EconomicClass(profile.economic_class or EconomicClass.UNKNOWN)
            if economics == EconomicClass.METERED_API and not self.policy.allow_paid_api:
                exclude(profile, "PAID_API_FORBIDDEN", "economic_policy")
                continue
            if (
                economics == EconomicClass.METERED_API
                and self.policy.max_metered_invocations is not None
                and state.metered_invocation_count
                >= self.policy.max_metered_invocations
            ):
                exclude(profile, "METERED_BUDGET_EXHAUSTED", "economic_policy")
                continue
            candidates.append(profile)

        recommendation = self._recommend(task, role, candidates)
        avoid_id = self._normalize(avoid_resource_id) if avoid_resource_id else None
        candidates.sort(key=lambda profile: self._ranking_key(profile, recommendation, avoid_id))
        supplied_explicit = explicit_resource_id or task.preferred_agent
        explicit_id = self._normalize(supplied_explicit) if supplied_explicit else None
        selected: Optional[AgentProfile] = None
        if explicit_id:
            selected = next(
                (profile for profile in candidates if profile.resource_id == explicit_id),
                None,
            )
            if selected is None:
                candidates = []
        elif candidates:
            selected = candidates[0]

        status = (
            ResourceSelectionStatus.SELECTED
            if selected is not None
            else ResourceSelectionStatus.BLOCKED
        )
        identities = [self._resource_identity(profile) for profile in candidates]
        selected_identity = self._resource_identity(selected) if selected else None
        if selected_identity is not None:
            identities = [selected_identity] + [
                identity for identity in identities
                if identity.resource_id != selected_identity.resource_id
            ]
        return ResourceSelectionDecision(
            task_class=task.task_class,
            role=role,
            required_capabilities=required,
            status=status,
            registered_resources=registered,
            eligible_resources=identities,
            exclusions=exclusions,
            selected=selected_identity,
            selection_policy=self.policy.strategy,
            economic_policy=self.policy.model_dump(),
            cognitive_recommendation=recommendation,
            capacity_before=self.get_all_statuses(),
            explicit_override=explicit_id is not None,
            blocked_reason=(
                None if selected is not None else "NO_ELIGIBLE_AI_RESOURCE"
            ),
        )

    def select_candidates(
        self,
        task_category: str = "code_heavy",
        avoid_provider: Optional[str] = None,
        preferred_agent: Optional[str] = None,
        task: Optional[TaskSpec] = None,
    ) -> List[str]:
        """Compatibility API returning the new pipeline's ordered candidates."""
        if task is None or self._legacy_mode:
            preference = TASK_SUITABILITY_PREFERENCES.get(
                task_category,
                TASK_SUITABILITY_PREFERENCES["code_heavy"],
            )
            result = []
            for agent_id in preference:
                normalized = self._normalize(agent_id)
                if (
                    task is not None
                    and normalized in LOCAL_PROVIDER_IDS
                    and not is_task_local_eligible(task)
                ):
                    continue
                if self.get_status(normalized) in {
                    ProviderAvailabilityStatus.AVAILABLE,
                    ProviderAvailabilityStatus.UNKNOWN,
                    ProviderAvailabilityStatus.DEGRADED,
                }:
                    result.append(normalized)
            if preferred_agent:
                preferred = self._normalize(preferred_agent)
                if preferred in result:
                    result.remove(preferred)
                    result.insert(0, preferred)
            if avoid_provider:
                avoided = self._normalize(avoid_provider)
                if avoided in result:
                    result.remove(avoided)
                    result.append(avoided)
            return list(dict.fromkeys(result))
        decision = self.select_resource(
            task,
            role="implementation",
            explicit_resource_id=preferred_agent,
            avoid_resource_id=avoid_provider,
        )
        return [identity.resource_id for identity in decision.eligible_resources]

    def select_reviewers(
        self,
        implementing_agent_id: str,
        required_roles: List[str],
        allow_same_provider: bool = False,
        task: Optional[TaskSpec] = None,
    ) -> Tuple[Dict[str, str], bool]:
        """Selects role-capable reviewers and reports identity diversity honestly."""
        implementation_id = self._normalize(implementing_agent_id)
        if self._legacy_mode:
            available = [
                profile.resource_id or profile.agent_id
                for profile in self.registry.list_resources()
                if self.get_status(profile.resource_id or profile.agent_id)
                == ProviderAvailabilityStatus.AVAILABLE
            ]
            distinct = [item for item in available if item != implementation_id]
            mapping: Dict[str, str] = {}
            diversity = True
            for index, role in enumerate(required_roles):
                role_candidates = distinct
                if role in LOCAL_INELIGIBLE_REVIEWER_ROLES:
                    role_candidates = [
                        item for item in role_candidates if item not in LOCAL_PROVIDER_IDS
                    ]
                if role_candidates:
                    mapping[role] = role_candidates[index % len(role_candidates)]
                else:
                    mapping[role] = implementation_id
                    diversity = False
            return mapping, diversity
        implementation = self.registry.get_resource(implementation_id)
        review_task = task or TaskSpec(
            task_id="REVIEW-SELECTION",
            repository="unknown",
            objective="Review an implementation diff",
            task_class="other",
            risk_level="medium",
        )
        mapping: Dict[str, str] = {}
        diversity = True
        used: Set[str] = set()
        for role in required_roles:
            decision = self.select_resource(
                review_task,
                role=role,
                avoid_resource_id=implementation_id,
            )
            eligible = [
                self.registry.get_resource(identity.resource_id)
                for identity in decision.eligible_resources
            ]
            eligible = [profile for profile in eligible if profile is not None]
            distinct = [
                profile for profile in eligible
                if profile.resource_id != implementation_id
                and implementation is not None
                and profile.provider_id != implementation.provider_id
            ]
            choices = distinct or [
                profile for profile in eligible if profile.resource_id != implementation_id
            ]
            if choices:
                unused = [profile for profile in choices if profile.resource_id not in used]
                chosen = (unused or choices)[0]
                mapping[role] = chosen.resource_id or chosen.agent_id
                used.add(chosen.resource_id or chosen.agent_id)
                if chosen not in distinct:
                    diversity = False
            elif implementation is not None:
                mapping[role] = implementation_id
                diversity = False
            elif allow_same_provider and decision.selected:
                mapping[role] = decision.selected.resource_id
                diversity = False
        return mapping, diversity
