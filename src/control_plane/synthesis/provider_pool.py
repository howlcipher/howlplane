#!/usr/bin/env python3
"""
provider_pool.py

Manages multi-agent provider pool availability, task suitability ranking,
quota exhaustion detection, dynamic provider fallback, and cross-provider review.
Distinguishes transient provider exhaustion from engineering failures.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
)
from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.task_spec import DataClassSerializationMixin, TaskSpec

PROVIDER_POOL_SCHEMA_VERSION = "howlplane.provider_pool/v1"

LOCAL_PROVIDER_IDS = {"local_ollama", "ollama_local"}

# Task classes/skills that must never be routed to a local Tier-3 model merely
# because cloud providers are exhausted. See milestone #58 Phase 9.
LOCAL_INELIGIBLE_TASK_CLASSES = {"security_patch", "infrastructure"}
LOCAL_INELIGIBLE_SKILLS = {
    "cyber_security", "blue_team", "red_team", "bug_bounty_hunter",
    "database_management", "devops_sre", "network_engineering",
}

# Reviewer roles a local Tier-3 model must never be assigned to, regardless of
# task risk level (#59.2 Phase 10). The local model must never become the
# sole/only security authority: is_task_local_eligible() gates by task
# risk/class, but review-role assignment (select_reviewers, below) has no
# task risk level of its own to gate on, so this is role-based instead.
LOCAL_INELIGIBLE_REVIEWER_ROLES = {"security-reviewer"}


def is_task_local_eligible(task: Optional[TaskSpec]) -> bool:
    """
    Determines whether a task is an appropriate Tier-3 / low-risk candidate for
    the local (quota-free, weaker) Ollama model, per milestone #58 Phase 9.

    Local-eligible: low risk_level and not touching a disallowed task_class/skill.
    Everything else (medium/high/critical risk, security/authority/infra work,
    or when no task is given) defaults to ineligible so local participation
    never silently expands to consequential or ambiguous work.
    """
    if task is None:
        return False
    if task.risk_level != "low":
        return False
    if task.task_class in LOCAL_INELIGIBLE_TASK_CLASSES:
        return False
    if set(task.required_skills or []).intersection(LOCAL_INELIGIBLE_SKILLS):
        return False
    return True


class ProviderAvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SESSION_EXHAUSTED = "SESSION_EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"
    RESOURCE_CONSTRAINED = "RESOURCE_CONSTRAINED"
    UNKNOWN = "UNKNOWN"


# Task suitability preference order
TASK_SUITABILITY_PREFERENCES: Dict[str, List[str]] = {
    "routine": ["agy", "codex", "devin_cli", "claude_code", "local_ollama"],
    "code_heavy": ["codex", "agy", "devin_cli", "claude_code", "local_ollama"],
    "large_autonomous": ["devin_cli", "codex", "agy", "claude_code"],
    "architecture_security": ["claude_code", "codex", "agy", "devin_cli"],
}

# Known provider exhaustion and rate limit signatures
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
        # Observed live during campaign DOGFOOD-20260822-005043-16adca (#59.1):
        # "Error: Individual quota reached. Please upgrade your subscription to
        # increase your limits. Resets in 89h21m16s." matched no pattern above,
        # so a genuine subscription exhaustion was misread as a normal
        # engineering failure and no failover could occur.
        "individual quota reached",
    ],
    "devin_cli": [
        "session limit",
        "quota unavailable",
        "rate limited",
        "credits exhausted",
        "insufficient funds",
    ],
    "local_ollama": [
        "connection refused",
        "not running",
        "server unavailable",
    ],
}


@dataclass
class ProviderExhaustionEvent(DataClassSerializationMixin):
    """Event recording when a provider encounters quota exhaustion or availability failure."""

    agent_id: str
    failure_type: str  # "session_limit", "rate_limit", "quota_exhausted", "unavailable"
    raw_error: str
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    task_id: Optional[str] = None


@dataclass
class ProviderStatus(DataClassSerializationMixin):
    """Live state of an agent provider in the session."""

    agent_id: str
    name: str
    status: ProviderAvailabilityStatus = ProviderAvailabilityStatus.AVAILABLE
    last_checked: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    consecutive_failures: int = 0
    exhaustion_event: Optional[ProviderExhaustionEvent] = None
    success_count: int = 0
    total_duration_seconds: float = 0.0
    unavailable_reason: Optional[str] = None


class ProviderPoolManager:
    """
    Manages provider selection, exhaustion detection, fallback routing,
    and cross-provider reviewer assignment.
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        avoid_provider: Optional[str] = None,
        fallback_provider: Optional[str] = None,
    ):
        self.registry = registry or AgentRegistry()
        self.avoid_provider = avoid_provider
        self.fallback_provider = fallback_provider
        self._provider_states: Dict[str, ProviderStatus] = {}
        self._initialize_states()

    def _initialize_states(self) -> None:
        for p in self.registry.list_agents():
            backend = AgentBackendRegistry.get_backend(p.agent_id)
            status, reason = self._probe_backend(backend)
            self._provider_states[p.agent_id] = ProviderStatus(
                agent_id=p.agent_id,
                name=p.name,
                status=status,
                unavailable_reason=reason,
            )

    @staticmethod
    def _probe_backend(backend: AgentBackend) -> Tuple[ProviderAvailabilityStatus, Optional[str]]:
        """
        Probes a backend's live availability. Backends exposing a `diagnose()`
        method (e.g. the local Ollama backend) provide a fine-grained reason
        distinguishing "not installed" / "service unavailable" / "model not
        installed" / "resource constrained" from a simple boolean.
        """
        diagnose = getattr(backend, "diagnose", None)
        if callable(diagnose):
            diag = diagnose()
            if diag.available:
                return ProviderAvailabilityStatus.AVAILABLE, None
            if diag.reason == "RESOURCE_CONSTRAINED":
                return ProviderAvailabilityStatus.RESOURCE_CONSTRAINED, diag.reason
            return ProviderAvailabilityStatus.UNAVAILABLE, diag.reason
        if backend.is_available():
            return ProviderAvailabilityStatus.AVAILABLE, None
        return ProviderAvailabilityStatus.UNAVAILABLE, None

    def _normalize(self, agent_id: str) -> str:
        return AgentBackendRegistry.normalize_agent_id(agent_id)

    def get_status(self, agent_id: str) -> ProviderAvailabilityStatus:
        nid = self._normalize(agent_id)
        if nid in self._provider_states:
            return self._provider_states[nid].status
        return ProviderAvailabilityStatus.UNKNOWN

    def set_status(self, agent_id: str, status: ProviderAvailabilityStatus, event: Optional[ProviderExhaustionEvent] = None) -> None:
        nid = self._normalize(agent_id)
        if nid not in self._provider_states:
            self._provider_states[nid] = ProviderStatus(agent_id=nid, name=nid)
        self._provider_states[nid].status = status
        self._provider_states[nid].last_checked = datetime.now(timezone.utc).isoformat()
        if event:
            self._provider_states[nid].exhaustion_event = event

    def reset_transient_exhaustion(self) -> None:
        """Resets transient rate-limited and session-exhausted states on new or resumed campaigns."""
        for agent_id, status_obj in self._provider_states.items():
            backend = AgentBackendRegistry.get_backend(agent_id)
            status, reason = self._probe_backend(backend)
            status_obj.status = status
            status_obj.unavailable_reason = reason
            if status == ProviderAvailabilityStatus.AVAILABLE:
                status_obj.exhaustion_event = None

    def get_all_statuses(self) -> Dict[str, str]:
        """Returns snapshot of current provider statuses."""
        return {aid: s.status.value for aid, s in self._provider_states.items()}

    def has_available_providers(self) -> bool:
        """Returns True if at least one provider is currently AVAILABLE."""
        return any(
            s.status == ProviderAvailabilityStatus.AVAILABLE
            for s in self._provider_states.values()
        )

    def is_all_cloud_exhausted(self) -> bool:
        """
        Returns True when every non-local (cloud) provider is exhausted, rate-limited,
        or unavailable. Local Ollama is quota-free and intentionally excluded from this
        check: its presence must never keep this returning False forever, but it also
        must never count as "cloud capacity" for exhaustion purposes (see #58 Phase 13).
        """
        cloud_statuses = [
            ps.status for aid, ps in self._provider_states.items()
            if aid not in LOCAL_PROVIDER_IDS
        ]
        if not cloud_statuses:
            return True
        return all(
            st in (
                ProviderAvailabilityStatus.SESSION_EXHAUSTED,
                ProviderAvailabilityStatus.RATE_LIMITED,
                ProviderAvailabilityStatus.UNAVAILABLE,
            )
            for st in cloud_statuses
        )

    def detect_exhaustion(self, agent_id: str, result: AgentExecutionResult, task_id: Optional[str] = None) -> Optional[ProviderExhaustionEvent]:
        """
        Distinguishes provider availability failures from normal engineering failures.
        """
        nid = self._normalize(agent_id)
        if result.success:
            if nid in self._provider_states:
                self._provider_states[nid].status = ProviderAvailabilityStatus.AVAILABLE
                self._provider_states[nid].success_count += 1
                self._provider_states[nid].total_duration_seconds += result.duration_seconds
            return None

        # Local Ollama has no subscription quota: it can never be "exhausted" in the
        # RATE_LIMITED/SESSION_EXHAUSTED sense. Classify its failures explicitly instead
        # of pattern-matching model output text (#58 Phase 11).
        if nid in LOCAL_PROVIDER_IDS:
            reason = (result.error_message or "").upper()
            if reason == "RESOURCE_CONSTRAINED":
                event = ProviderExhaustionEvent(
                    agent_id=nid, failure_type="unavailable",
                    raw_error=result.stderr.strip() or "Insufficient available RAM", task_id=task_id,
                )
                self.set_status(nid, ProviderAvailabilityStatus.RESOURCE_CONSTRAINED, event)
                return event
            if reason in ("OLLAMA_NOT_INSTALLED", "OLLAMA_SERVICE_UNAVAILABLE", "MODEL_NOT_INSTALLED", "LOCAL_PROVIDER_UNAVAILABLE", "LOCAL_PROVIDER_BUSY"):
                event = ProviderExhaustionEvent(
                    agent_id=nid, failure_type="unavailable",
                    raw_error=result.stderr.strip() or reason, task_id=task_id,
                )
                self.set_status(nid, ProviderAvailabilityStatus.UNAVAILABLE, event)
                return event
            # ENGINEERING_FAILURE / LOCAL_CAPABILITY_INSUFFICIENT / LOCAL_CONTEXT_INSUFFICIENT:
            # the model ran and produced a wrong/incomplete answer. Not an exhaustion event.
            return None

        combined_output = (result.stderr + "\n" + result.stdout).lower()

        # Check binary missing
        if result.exit_code == 127 or "not installed" in combined_output or "not found" in combined_output:
            event = ProviderExhaustionEvent(
                agent_id=nid,
                failure_type="unavailable",
                raw_error=result.stderr.strip() or "Binary not found",
                task_id=task_id,
            )
            self.set_status(nid, ProviderAvailabilityStatus.UNAVAILABLE, event)
            return event

        # Check provider exhaustion signatures across specific provider patterns and generic patterns
        patterns = list(EXHAUSTION_PATTERNS.get(nid, []))
        generic_exhaustion = [
            "quota exceeded",
            "rate limit",
            "rate_limit",
            "usage limit",
            "session limit",
            "credits exhausted",
            "insufficient quota",
            "429 too many requests",
            "resource exhausted",
            "out of capacity",
            "session exhausted",
            # Provider-agnostic phrasings of subscription exhaustion. "quota
            # reached" generalizes the live agy wording above; "upgrade your
            # subscription" is only ever emitted by a billing/limit refusal,
            # never by a compiler or test failure (#59.1).
            "quota reached",
            "upgrade your subscription",
        ]
        all_patterns = patterns + generic_exhaustion

        for pattern in all_patterns:
            if pattern in combined_output:
                ftype = "rate_limit" if "rate" in pattern or "429" in pattern else "session_limit"
                event = ProviderExhaustionEvent(
                    agent_id=nid,
                    failure_type=ftype,
                    raw_error=result.stderr.strip() or pattern,
                    task_id=task_id,
                )
                new_status = ProviderAvailabilityStatus.RATE_LIMITED if ftype == "rate_limit" else ProviderAvailabilityStatus.SESSION_EXHAUSTED
                self.set_status(nid, new_status, event)
                return event

        # Normal engineering failure: tests failed, compiler syntax error, etc.
        # Do NOT mark provider as exhausted!
        return None

    def is_all_exhausted(self) -> bool:
        """Returns True if all configured providers are in an exhausted or rate-limited status."""
        statuses = [ps.status for ps in self._provider_states.values()]
        return bool(statuses) and all(
            st in (ProviderAvailabilityStatus.SESSION_EXHAUSTED, ProviderAvailabilityStatus.RATE_LIMITED)
            for st in statuses
        )

    def select_candidates(
        self,
        task_category: str = "code_heavy",
        avoid_provider: Optional[str] = None,
        preferred_agent: Optional[str] = None,
        task: Optional[TaskSpec] = None,
    ) -> List[str]:
        """
        Ranks candidate agents by task suitability and availability,
        placing avoided/fallback providers last.

        `task`, when provided, gates local (quota-free) providers by Tier-3
        eligibility (#58 Phase 9): local models are never selected for
        medium/high/critical risk or disallowed task classes merely because
        cloud providers ran out of quota. When `task` is omitted, local
        eligibility is not evaluated and existing (pre-#58) behavior applies.
        """
        avoid_norm = self._normalize(avoid_provider) if avoid_provider else (
            self._normalize(self.avoid_provider) if self.avoid_provider else None
        )
        pref_agent_norm = self._normalize(preferred_agent) if preferred_agent else None
        pref_list = [self._normalize(a) for a in TASK_SUITABILITY_PREFERENCES.get(task_category, TASK_SUITABILITY_PREFERENCES["code_heavy"])]

        if task is not None and not is_task_local_eligible(task):
            pref_list = [a for a in pref_list if a not in LOCAL_PROVIDER_IDS]

        candidates: List[str] = []

        # If user specified preferred agent and it's available, place first
        if pref_agent_norm and self.get_status(pref_agent_norm) == ProviderAvailabilityStatus.AVAILABLE:
            if pref_agent_norm not in LOCAL_PROVIDER_IDS or task is None or is_task_local_eligible(task):
                candidates.append(pref_agent_norm)

        for agent_id in pref_list:
            if agent_id not in candidates and self.get_status(agent_id) == ProviderAvailabilityStatus.AVAILABLE:
                candidates.append(agent_id)

        # Append degraded/unknown agents before avoided providers
        for agent_id in pref_list:
            if agent_id not in candidates and self.get_status(agent_id) == ProviderAvailabilityStatus.UNKNOWN:
                candidates.append(agent_id)

        # Move avoid_provider to the very end
        if avoid_norm and avoid_norm in candidates:
            candidates.remove(avoid_norm)
            candidates.append(avoid_norm)

        return candidates

    def select_reviewers(
        self,
        implementing_agent_id: str,
        required_roles: List[str],
        allow_same_provider: bool = False,
    ) -> Tuple[Dict[str, str], bool]:
        """
        Selects independent reviewers from distinct providers whenever available.
        Returns mapping of role_id -> agent_id, and boolean indicating if full
        provider diversity was achieved.

        Roles in LOCAL_INELIGIBLE_REVIEWER_ROLES (#59.2 Phase 10) never receive
        a local provider, even when local is the only distinct candidate --
        the local Tier-3 model must never become the sole security authority.
        """
        impl_norm = self._normalize(implementing_agent_id)
        available_agents = [
            self._normalize(a.agent_id)
            for a in self.registry.list_agents()
            if self.get_status(a.agent_id) == ProviderAvailabilityStatus.AVAILABLE
        ]

        distinct_candidates = [a for a in available_agents if a != impl_norm]
        role_mapping: Dict[str, str] = {}
        diversity_achieved = True

        for idx, role in enumerate(required_roles):
            role_candidates = distinct_candidates
            if role in LOCAL_INELIGIBLE_REVIEWER_ROLES:
                role_candidates = [c for c in distinct_candidates if c not in LOCAL_PROVIDER_IDS]

            if role_candidates:
                chosen = role_candidates[idx % len(role_candidates)]
                role_mapping[role] = chosen
            elif allow_same_provider and available_agents:
                role_mapping[role] = impl_norm
                diversity_achieved = False
            else:
                role_mapping[role] = impl_norm
                diversity_achieved = False

        return role_mapping, diversity_achieved
