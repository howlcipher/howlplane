#!/usr/bin/env python3
"""
authority_profile.py

Canonical, code-defined authority profile templates for delegated overnight
campaign authority (#59). A profile is a static, versioned policy template;
it is bound to a specific campaign as a durable AuthorityEnvelope
(see authority_envelope.py) only at explicit operator invocation.

Profiles are immutable (frozen dataclasses) and only ever exist as the two
module-level constants below. No runtime code in this codebase constructs a
new AuthorityProfile instance or writes to CANONICAL_PROFILES -- this is the
mechanical guarantee that an AI-driven campaign cannot invent or expand its
own authority profile.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Union

from src.control_plane.task_spec import DataClassSerializationMixin

AUTHORITY_PROFILE_SCHEMA_VERSION = "howlplane.authority_profile/v1"

# Deliberately duplicated from src.control_plane.synthesis.campaign_state's
# DEFAULT_LOCAL_ONLY_ITERATION_LIMIT rather than imported: authority_profile.py
# is a foundational module (like human_boundary.py) that must not depend on
# the synthesis package, whose __init__.py eagerly imports marathon.py --
# which itself imports authority_envelope.py/authority_profile.py, creating
# a circular import. Both constants must be kept in sync; a mismatch is
# caught by tests/test_authority_profile.py.
DEFAULT_LOCAL_ONLY_ITERATION_LIMIT = 10


class UnknownProfileError(ValueError):
    """Raised when an unrecognized authority profile id is requested."""
    pass


# Actions an overnight-safe campaign may perform without stopping for a human,
# strictly for the routine task-branch git/GitHub integration lifecycle and
# ordinary engineering work (#59 Phase 9).
OVERNIGHT_SAFE_ALLOWED_ACTIONS: List[str] = [
    "create_task_branch",
    "commit_task_changes",
    "push_task_branch",
    "create_pull_request",
    "inspect_ci",
    "repair_ci_bounded",
    "merge_pull_request",
    "sync_local_main",
    "run_build_test_lint_scan",
    "invoke_configured_ai_provider",
    "switch_ai_provider",
    "use_local_ollama_tier3",
    "update_task_journal",
    "select_next_evidence_backed_task",
    "park_and_continue",
]

# Actions that must NEVER be auto-authorized overnight, regardless of any
# envelope contents (#59 Phase 10). Denies always win over allows in
# evaluate_action_against_envelope().
OVERNIGHT_SAFE_DENIED_ACTIONS: List[str] = [
    "force_push",
    "history_rewrite",
    "bypass_required_checks",
    "branch_protection_weakening",
    "production_deployment",
    "infrastructure_apply",
    "destructive_database_change",
    "credential_provisioning",
    "package_publishing",
    "external_messaging",
    "job_submission",
    "paid_service_usage",
    "external_dependency_addition",
    "security_policy_exception",
    "hygiene_policy_weakening",
    "slop_debt_acceptance",
    "authority_profile_modification",
    "authority_enforcement_modification",
]


@dataclass(frozen=True)
class AuthorityProfile(DataClassSerializationMixin):
    """
    Immutable, code-defined delegated-authority policy template.

    This is a template, not a grant: a profile only becomes operative
    authority for a specific campaign when bound into an AuthorityEnvelope
    at explicit operator-invoked campaign creation (see
    authority_envelope.create_envelope()).
    """

    profile_id: str
    version: str
    ttl_hours: float
    max_merges: int
    external_spend_usd_limit: float
    authorized_repositories: List[str] = field(default_factory=list)
    allowed_action_classes: List[str] = field(default_factory=list)
    denied_action_classes: List[str] = field(default_factory=list)
    local_ram_threshold_gib: float = 8.0
    local_keep_alive: Union[int, str] = "5m"
    local_only_iteration_limit: int = DEFAULT_LOCAL_ONLY_ITERATION_LIMIT
    schema: str = AUTHORITY_PROFILE_SCHEMA_VERSION


# No autonomous merges of any kind. Every consequential action requires an
# explicit human decision. Used as the safe default when no profile is
# selected, and as the fallback when an overnight-safe envelope has expired
# without explicit reauthorization (#59 Phase 15).
STRICT_PROFILE = AuthorityProfile(
    profile_id="strict",
    version="1.0",
    ttl_hours=0.0,
    max_merges=0,
    external_spend_usd_limit=0.0,
    authorized_repositories=[],
    allowed_action_classes=[],
    denied_action_classes=list(OVERNIGHT_SAFE_DENIED_ACTIONS),
    local_ram_threshold_gib=8.0,
    local_keep_alive="5m",
    local_only_iteration_limit=DEFAULT_LOCAL_ONLY_ITERATION_LIMIT,
)

# Canonical overnight-unattended profile (#59 Phase 9). Explicitly scoped to
# howlplane only -- Career_Agent_Core and any other repository are never
# included. A 12-hour TTL and a hard cap of 10 autonomous merges bound the
# blast radius of a single unattended night.
OVERNIGHT_SAFE_PROFILE = AuthorityProfile(
    profile_id="overnight-safe",
    version="1.0",
    ttl_hours=12.0,
    max_merges=10,
    external_spend_usd_limit=0.0,
    authorized_repositories=["howlcipher/howlplane"],
    allowed_action_classes=list(OVERNIGHT_SAFE_ALLOWED_ACTIONS),
    denied_action_classes=list(OVERNIGHT_SAFE_DENIED_ACTIONS),
    local_ram_threshold_gib=9.0,
    local_keep_alive=0,
    local_only_iteration_limit=DEFAULT_LOCAL_ONLY_ITERATION_LIMIT,
)

# Canonical profile for the first unattended HowlFrame backlog marathon.
#
# Deliberately a third profile rather than a widening of OVERNIGHT_SAFE_PROFILE:
# that profile was audited and granted for `howlcipher/howlplane`, and adding a
# second repository to it would silently extend every existing invocation's
# blast radius. A separate profile makes the HowlFrame grant an explicit opt-in
# at the command line.
#
# `merge_pull_request` is absent from the allow list AND `max_merges` is 0, so
# an autonomous merge is refused twice over. The first marathon stops at green
# pull requests and a human authorizes every merge. A marathon that produces
# several independently reviewed, verified, green PRs is useful; one that
# merges unreviewed work to look more autonomous is not.
HOWLFRAME_OVERNIGHT_PROFILE = AuthorityProfile(
    profile_id="howlframe-overnight",
    version="1.0",
    # Shorter than overnight-safe's 12h: this is a first run, and a shorter TTL
    # means an envelope that outlives its usefulness expires sooner.
    ttl_hours=10.0,
    max_merges=0,
    external_spend_usd_limit=0.0,
    authorized_repositories=["howlcipher/howlframe"],
    allowed_action_classes=[
        action for action in OVERNIGHT_SAFE_ALLOWED_ACTIONS
        if action != "merge_pull_request"
    ],
    denied_action_classes=list(OVERNIGHT_SAFE_DENIED_ACTIONS),
    local_ram_threshold_gib=9.0,
    local_keep_alive=0,
    local_only_iteration_limit=DEFAULT_LOCAL_ONLY_ITERATION_LIMIT,
)

CANONICAL_PROFILES: Dict[str, AuthorityProfile] = {
    "strict": STRICT_PROFILE,
    "overnight-safe": OVERNIGHT_SAFE_PROFILE,
    "howlframe-overnight": HOWLFRAME_OVERNIGHT_PROFILE,
}


def get_profile(profile_id: str) -> AuthorityProfile:
    """
    Returns the canonical AuthorityProfile for the given id.
    Raises UnknownProfileError for anything not explicitly defined above --
    there is no dynamic or user-supplied profile construction path.
    """
    profile = CANONICAL_PROFILES.get(profile_id)
    if profile is None:
        raise UnknownProfileError(
            f"Unknown authority profile '{profile_id}'. Known profiles: {sorted(CANONICAL_PROFILES)}"
        )
    return profile
