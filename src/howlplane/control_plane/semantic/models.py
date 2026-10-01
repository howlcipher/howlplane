#!/usr/bin/env python3
"""Typed contracts for optional System 1 (HowlInstinct) resource judgments.

A judgment is advisory evidence. These types carry what HowlInstinct said and
what Plane's own policy did with it; they never carry authority.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

SEMANTIC_RECOMMENDATION_SCHEMA_VERSION = "howlplane.semantic_recommendation/v1"
SEMANTIC_QUESTION_ID = "resource_selection"


class SemanticMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


class SemanticStatus(str, Enum):
    """Outcome of one semantic recommendation attempt.

    JUDGED is internal: a structurally valid judgment that Plane policy has not
    yet evaluated. It is never recorded as a final status.
    """

    DISABLED = "DISABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    JUDGED = "JUDGED"
    ACCEPTED = "ACCEPTED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    INVALID_RECEIPT = "INVALID_RECEIPT"
    UNKNOWN_RESOURCE = "UNKNOWN_RESOURCE"
    CONFIG_ERROR = "CONFIG_ERROR"


# Statuses that mean the System 1 output could not be trusted. Strict mode
# turns these into BLOCKED; the default is a deterministic fallback.
INTEGRITY_FAILURES = frozenset({
    SemanticStatus.MALFORMED_RESPONSE,
    SemanticStatus.INVALID_RECEIPT,
    SemanticStatus.UNKNOWN_RESOURCE,
    SemanticStatus.CONFIG_ERROR,
})


@dataclass(frozen=True)
class RecommendationContext:
    """Per-call context. Capacity is descriptive input, not a permission."""

    capacity: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class SemanticRecommendation:
    """One semantic recommendation attempt and the policy applied to it.

    ``provider_confidence`` and ``instinct_margin`` stay None when HowlInstinct
    did not report them. Absence is never recorded as zero.
    """

    status: SemanticStatus
    mode: str = SemanticMode.OFF.value
    reason: str = ""
    attempted: bool = False
    selected_resource_id: Optional[str] = None
    distribution: Optional[Dict[str, float]] = None
    provider_confidence: Optional[float] = None
    instinct_margin: Optional[float] = None
    receipt: Optional[Dict[str, Any]] = None
    receipt_digest: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    candidate_ids: List[str] = field(default_factory=list)
    minimum_instinct_margin: Optional[float] = None
    accepted_by_policy: bool = False
    applied: bool = False
    deterministic_recommendation: Optional[str] = None
    final_selection: Optional[str] = None
    disagreement: Optional[bool] = None
    schema: str = SEMANTIC_RECOMMENDATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload
