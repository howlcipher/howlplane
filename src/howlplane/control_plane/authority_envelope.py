#!/usr/bin/env python3
"""
authority_envelope.py

Durable, tamper-evident, campaign-bound instantiation of an AuthorityProfile
(#59 Phase 8). Where an AuthorityProfile is a static, code-defined policy
template, an AuthorityEnvelope is the concrete, expiring authorization that a
specific campaign actually holds -- created exactly once, at explicit
operator-invoked campaign start (or explicit reauthorization on resume), and
never mutated afterward.

Tamper evidence uses a native SHA-256 canonical-digest scheme, not HMAC:
genuine HMAC signing in this codebase only exists inside the external,
out-of-repo HowlChangeOps binary (see executor.py), which is not extendable
from within howlplane. This module instead reuses the same pattern already
established by proposed_action.py / human_boundary.py's
RepositoryStateFingerprint: canonical JSON -> SHA-256, verified on every
read, write-once persistence, fail closed on any mismatch.

Mechanical non-self-expansion guarantees:
  - create_envelope() is the sole constructor path. It is only ever invoked
    from cli.py's `ai dogfood --authority-profile <id>` dispatch, driven by a
    value that arrives from the operator's shell invocation, never from
    AI-generated text or task output.
  - save_envelope() refuses to overwrite an existing envelope file for the
    same campaign. No code in marathon.py, git_integration.py, or any
    agent-invoked module calls it a second time mid-campaign.
  - Every consumer calls load_envelope() fresh from disk (never a long-lived
    cached in-process object an agent's output could have mutated), which
    re-verifies the digest on every single call.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from howlplane.control_plane.atomic_io import atomic_write_json, safe_load_json
from howlplane.control_plane.authority_profile import AuthorityProfile
from howlplane.control_plane.task_spec import DataClassSerializationMixin

AUTHORITY_ENVELOPE_SCHEMA_VERSION = "howlplane.authority_envelope/v1"
ENVELOPE_FILENAME = "authority_envelope.json"

# Fields excluded from the policy digest computation because they are not
# part of the granted policy itself (the digest binds the POLICY, and is
# recomputed from those fields to check for drift).
_DIGEST_EXCLUDED_FIELDS = {"policy_digest", "schema"}


class EnvelopeError(Exception):
    """Base exception for authority envelope errors."""
    pass


class EnvelopeAlreadyExistsError(EnvelopeError):
    """Raised when attempting to save an envelope over an existing one (write-once)."""
    pass


class TamperedEnvelopeError(EnvelopeError):
    """Raised when a loaded envelope's digest does not match its recomputed digest."""
    pass


class EnvelopeNotFoundError(EnvelopeError):
    """Raised when no envelope file exists for a campaign."""
    pass


@dataclass
class AuthorityEnvelope(DataClassSerializationMixin):
    """Durable, campaign-bound, tamper-evident delegated authority grant."""

    campaign_id: str
    profile_id: str
    profile_version: str
    created_at: str
    expires_at: str
    authorized_repositories: List[str] = field(default_factory=list)
    allowed_action_classes: List[str] = field(default_factory=list)
    denied_action_classes: List[str] = field(default_factory=list)
    max_merges: int = 0
    external_spend_usd_limit: float = 0.0
    local_ram_threshold_gib: float = 8.0
    local_keep_alive: Union[int, str] = "5m"
    local_only_iteration_limit: int = 10
    operator_origin: str = ""
    policy_digest: str = ""
    schema: str = AUTHORITY_ENVELOPE_SCHEMA_VERSION


def canonical_json(data: Dict[str, Any]) -> bytes:
    """Deterministic canonical serialization used for digest computation."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_fields(envelope_dict: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in envelope_dict.items() if k not in _DIGEST_EXCLUDED_FIELDS}


def compute_policy_digest(envelope_dict: Dict[str, Any]) -> str:
    """SHA-256 hex digest over the canonicalized policy fields of an envelope."""
    return hashlib.sha256(canonical_json(_digest_fields(envelope_dict))).hexdigest()


def create_envelope(
    profile: AuthorityProfile,
    campaign_id: str,
    operator_origin: str,
    now: Optional[datetime] = None,
) -> AuthorityEnvelope:
    """
    Sole constructor for a durable AuthorityEnvelope. Binds `profile` to
    `campaign_id` for exactly `profile.ttl_hours` from `now`, and stamps a
    SHA-256 policy digest over the resulting fields.
    """
    now = now or datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=profile.ttl_hours)

    envelope = AuthorityEnvelope(
        campaign_id=campaign_id,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
        authorized_repositories=list(profile.authorized_repositories),
        allowed_action_classes=list(profile.allowed_action_classes),
        denied_action_classes=list(profile.denied_action_classes),
        max_merges=profile.max_merges,
        external_spend_usd_limit=profile.external_spend_usd_limit,
        local_ram_threshold_gib=profile.local_ram_threshold_gib,
        local_keep_alive=profile.local_keep_alive,
        local_only_iteration_limit=profile.local_only_iteration_limit,
        operator_origin=operator_origin,
        policy_digest="",
    )
    envelope.policy_digest = compute_policy_digest(envelope.to_dict())
    return envelope


def verify_envelope_integrity(envelope: AuthorityEnvelope) -> Tuple[bool, Optional[str]]:
    """
    Recomputes the policy digest from the envelope's current field values and
    compares it to the stored digest. Any drift -- whether from a bug, an
    agent shelling out to edit the file, or manual tampering -- fails closed.
    """
    expected = compute_policy_digest(envelope.to_dict())
    if expected != envelope.policy_digest:
        return False, (
            f"TAMPERED_ENVELOPE: recomputed policy digest '{expected}' does not match "
            f"stored digest '{envelope.policy_digest}' for campaign '{envelope.campaign_id}'"
        )
    return True, None


def is_expired(envelope: AuthorityEnvelope, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        expires_at = datetime.fromisoformat(envelope.expires_at)
    except (TypeError, ValueError):
        # Unparseable expiry is treated as already expired -- fail closed.
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now >= expires_at


def _envelope_path(campaign_dir: Union[str, Path]) -> Path:
    return Path(campaign_dir).resolve() / ENVELOPE_FILENAME


def save_envelope(envelope: AuthorityEnvelope, campaign_dir: Union[str, Path]) -> Path:
    """
    Atomically persists a newly created envelope. Write-once: refuses to
    overwrite an existing envelope file for this campaign. Explicit
    reauthorization (#59 Phase 15) must go through a fresh campaign_dir
    layout or an explicit, operator-driven overwrite path -- never an
    implicit one.
    """
    target = _envelope_path(campaign_dir)
    if target.is_file():
        raise EnvelopeAlreadyExistsError(
            f"An authority envelope already exists at {target}; refusing to overwrite. "
            "Explicit reauthorization must use a new campaign or an explicit operator action."
        )
    return atomic_write_json(target, envelope.to_dict())


def load_envelope(campaign_dir: Union[str, Path]) -> AuthorityEnvelope:
    """
    Loads and integrity-verifies the envelope for a campaign. Raises
    EnvelopeNotFoundError if absent, TamperedEnvelopeError on digest
    mismatch (fail closed) -- never returns an unverified envelope.
    """
    target = _envelope_path(campaign_dir)
    if not target.is_file():
        raise EnvelopeNotFoundError(f"No authority envelope found at {target}")
    data = safe_load_json(target)
    envelope = AuthorityEnvelope.from_dict(data)
    ok, reason = verify_envelope_integrity(envelope)
    if not ok:
        raise TamperedEnvelopeError(reason)
    return envelope


class AuthorityDecision(str, Enum):
    DELEGATED_AUTHORITY_ALLOW = "DELEGATED_AUTHORITY_ALLOW"
    DENIED_BY_ENVELOPE = "DENIED_BY_ENVELOPE"
    OUTSIDE_ENVELOPE_SCOPE = "OUTSIDE_ENVELOPE_SCOPE"
    ENVELOPE_EXPIRED = "ENVELOPE_EXPIRED"
    ENVELOPE_TAMPERED = "ENVELOPE_TAMPERED"
    ENVELOPE_ABSENT = "ENVELOPE_ABSENT"


def evaluate_action_against_envelope(
    envelope: Optional[AuthorityEnvelope],
    action_class: str,
    repo_slug: str,
    merges_so_far: int = 0,
    spend_so_far: float = 0.0,
    now: Optional[datetime] = None,
) -> Tuple[AuthorityDecision, str]:
    """
    THE single deterministic choke point for "is this action delegated?".
    Pure function: no side effects, no AI call, no I/O beyond what the caller
    already loaded. Every consumer (HumanBoundaryGate, GitIntegrationExecutor,
    the marathon loop) must call this rather than re-deriving its own
    judgment of what the envelope permits.

    Denies always win over allows. Fails closed on any absence/tamper/expiry.
    """
    if envelope is None:
        return AuthorityDecision.ENVELOPE_ABSENT, "No authority envelope is bound to this campaign."

    ok, reason = verify_envelope_integrity(envelope)
    if not ok:
        return AuthorityDecision.ENVELOPE_TAMPERED, reason or "Envelope failed integrity verification."

    if is_expired(envelope, now=now):
        return (
            AuthorityDecision.ENVELOPE_EXPIRED,
            f"Authority envelope for campaign '{envelope.campaign_id}' expired at {envelope.expires_at}.",
        )

    if action_class in envelope.denied_action_classes:
        return (
            AuthorityDecision.DENIED_BY_ENVELOPE,
            f"Action class '{action_class}' is explicitly denied by profile '{envelope.profile_id}'.",
        )

    if repo_slug not in envelope.authorized_repositories:
        return (
            AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE,
            f"Repository '{repo_slug}' is not in the authorized repository allowlist "
            f"{envelope.authorized_repositories}.",
        )

    if action_class not in envelope.allowed_action_classes:
        return (
            AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE,
            f"Action class '{action_class}' is not in the allowed action classes for "
            f"profile '{envelope.profile_id}'.",
        )

    if action_class == "merge_pull_request" and merges_so_far >= envelope.max_merges:
        return (
            AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE,
            f"merge_budget_reached: {merges_so_far}/{envelope.max_merges} merges already used "
            f"this campaign.",
        )

    if spend_so_far > envelope.external_spend_usd_limit:
        return (
            AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE,
            f"spend_budget_reached: ${spend_so_far} exceeds the ${envelope.external_spend_usd_limit} "
            f"limit for profile '{envelope.profile_id}'.",
        )

    return AuthorityDecision.DELEGATED_AUTHORITY_ALLOW, f"Action class '{action_class}' is delegated by envelope."
