#!/usr/bin/env python3
"""Typed contracts for AI resource identity, readiness, and selection."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from src.control_plane.task_spec import DataClassSerializationMixin

RESOURCE_SELECTION_SCHEMA_VERSION = "howlplane.resource_selection/v1"
AI_RESOURCE_INVENTORY_SCHEMA_VERSION = "howlplane.ai_resources/v1"


class ResourceLocality(str, Enum):
    """Observable location of a configured resource."""

    HOSTED = "HOSTED"
    LOCAL = "LOCAL"
    UNKNOWN = "UNKNOWN"


class EconomicClass(str, Enum):
    """Billing relationship without inferred prices."""

    SUBSCRIPTION = "SUBSCRIPTION"
    METERED_API = "METERED_API"
    LOCAL = "LOCAL"
    UNKNOWN = "UNKNOWN"


class ReadinessStatus(str, Enum):
    """Non-generative runtime readiness observation."""

    READY = "READY"
    NOT_PROBED = "NOT_PROBED"
    MISSING_EXECUTABLE = "MISSING_EXECUTABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNREACHABLE = "UNREACHABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class AuthenticationStatus(str, Enum):
    """Authentication state only when safely observable."""

    AUTHENTICATED = "AUTHENTICATED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class ProviderFailureClass(str, Enum):
    """Central normalized provider and engineering failure taxonomy."""

    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SESSION_LIMIT = "SESSION_LIMIT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    MISSING_EXECUTABLE = "MISSING_EXECUTABLE"
    ENGINEERING_FAILURE = "ENGINEERING_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    POLICY_FAILURE = "POLICY_FAILURE"
    UNKNOWN = "UNKNOWN"


class ResourceSelectionStatus(str, Enum):
    """Outcome of deterministic resource selection."""

    SELECTED = "SELECTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ResourceIdentity(DataClassSerializationMixin):
    """Identity levels that must never be conflated."""

    provider_id: str
    interface_id: str
    resource_id: str
    model_id: Optional[str] = None


@dataclass
class BackendReadiness(DataClassSerializationMixin):
    """Safe adapter readiness result that never requires generation."""

    status: ReadinessStatus
    installed: Optional[bool] = None
    reachable: Optional[bool] = None
    authentication: AuthenticationStatus = AuthenticationStatus.UNKNOWN
    reason: Optional[str] = None
    evidence: Optional[str] = None


@dataclass(frozen=True)
class ResourceExclusion(DataClassSerializationMixin):
    """Explainable reason a registered resource did not remain eligible."""

    resource_id: str
    reason: str
    stage: str
    detail: Optional[str] = None


@dataclass(frozen=True)
class CognitiveRecommendation(DataClassSerializationMixin):
    """Authority-free advisory ranking result."""

    resource_id: Optional[str]
    reason: str
    strategy_id: str = "deterministic_router/v1"


@dataclass
class ResourceSelectionDecision(DataClassSerializationMixin):
    """Stable evidence packet for every deterministic selection attempt."""

    task_class: str
    role: str
    required_capabilities: List[str]
    status: ResourceSelectionStatus
    registered_resources: List[ResourceIdentity] = field(default_factory=list)
    eligible_resources: List[ResourceIdentity] = field(default_factory=list)
    exclusions: List[ResourceExclusion] = field(default_factory=list)
    selected: Optional[ResourceIdentity] = None
    selection_policy: str = "adaptive_capacity"
    economic_policy: Dict[str, Any] = field(default_factory=dict)
    cognitive_recommendation: Optional[CognitiveRecommendation] = None
    capacity_before: Dict[str, str] = field(default_factory=dict)
    explicit_override: bool = False
    blocked_reason: Optional[str] = None
    diversity_achieved: Optional[bool] = None
    schema: str = RESOURCE_SELECTION_SCHEMA_VERSION

    def exclusion_for(self, resource_id: str) -> ResourceExclusion:
        """Returns one resource's exclusion or raises a precise lookup error."""
        for exclusion in self.exclusions:
            if exclusion.resource_id == resource_id:
                return exclusion
        raise KeyError(f"No exclusion was recorded for resource '{resource_id}'.")

    def blocked_outcome(self) -> Dict[str, Any]:
        """Returns the structured public failure contract."""
        return {
            "status": "BLOCKED",
            "reason": self.blocked_reason or "NO_ELIGIBLE_AI_RESOURCE",
            "role": self.role,
            "exclusions": [item.to_dict() for item in self.exclusions],
            "schema": self.schema,
        }
