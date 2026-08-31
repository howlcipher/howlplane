#!/usr/bin/env python3
"""
proposed_action.py

Defines the structured representation of executable and consequential actions
distinguishing proposal/implementation artifacts from real-world side effects.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.task_spec import DataClassSerializationMixin

PROPOSED_ACTION_SCHEMA_VERSION = "howlplane.proposed_action/v1"

CONSEQUENTIAL_RULES: List[Tuple[Tuple[str, ...], str, str, str, Optional[str]]] = [
    (
        ("create_release_candidate", "create release candidate", "release candidate tag", "tag release candidate", "howlchangeops"),
        "create_release_candidate",
        "high",
        "package_publishing",
        "howlchangeops",
    ),
    (
        ("terraform apply", "kubectl apply"),
        "infrastructure_apply",
        "critical",
        "infrastructure_apply",
        None,
    ),
    (
        ("drop table", "drop column", "truncate"),
        "destructive_database_change",
        "critical",
        "destructive_database_change",
        None,
    ),
    (
        ("twine upload", "npm publish", "publish package", "publish"),
        "package_publishing",
        "high",
        "package_publishing",
        None,
    ),
    (
        ("sendmail", "smtp", "webhook"),
        "external_messaging",
        "medium",
        "external_messaging",
        None,
    ),
    (
        ("production deploy", "deploy to prod", "deploy production"),
        "production_deployment",
        "critical",
        "production_deployment",
        None,
    ),
    (
        ("force push", "push --force", "push -f", "git push -f"),
        "force_push",
        "critical",
        "force_push",
        None,
    ),
    (
        ("filter-branch", "reset --hard.*origin", "rebase -i.*origin", "history rewrite", "rewrite history"),
        "history_rewrite",
        "critical",
        "history_rewrite",
        None,
    ),
    (
        ("bypass required check", "bypass ci", "skip required status check", "--admin merge"),
        "bypass_required_checks",
        "critical",
        "bypass_required_checks",
        None,
    ),
    (
        ("weaken branch protection", "disable branch protection", "remove required status check"),
        "branch_protection_weakening",
        "critical",
        "branch_protection_weakening",
        None,
    ),
]

# Paths whose modification always requires human authority (#59 Phase 10),
# regardless of any AuthorityEnvelope: these files define or enforce the
# authority system itself. A campaign must never be able to rewrite the
# rules controlling its own authority and use the new rules in the same
# unattended run.
SELF_MODIFICATION_PATHS: Tuple[str, ...] = (
    "src/control_plane/authority_profile.py",
    "src/control_plane/authority_envelope.py",
    "src/control_plane/human_boundary.py",
    "src/control_plane/executor.py",
)

# Whole subtrees under the same rule. The exact-path tuple above is matched
# with `endswith`, which cannot express a directory: a factory module added
# later would silently escape the rule that every other part of the supervisor
# obeys. Prefix matching is fail-closed for anything added under these roots.
#
# The factory supervisor governs its own dispatch, so a campaign editing it
# while running under it is the same class of hazard as editing the authority
# system itself.
SELF_MODIFICATION_PATH_PREFIXES: Tuple[str, ...] = (
    "src/control_plane/factory/",
)


def infer_proposed_actions_from_diff(files_changed: List[str], repo_name: str = "") -> List["ProposedAction"]:
    """
    Detects attempted self-modification of the authority enforcement system
    from a list of changed file paths (#59 Phase 10/13). Always
    risk_level="critical" with no executor_id -- permanently human-only, not
    executable-via-approval by any bounded executor.
    """
    for f in files_changed or []:
        normalized = f.replace("\\", "/")
        if any(normalized.endswith(p) for p in SELF_MODIFICATION_PATHS) or any(
            prefix in normalized for prefix in SELF_MODIFICATION_PATH_PREFIXES
        ):
            return [
                ProposedAction(
                    action_type="authority_enforcement_modification",
                    target_repo=repo_name,
                    risk_level="critical",
                    requires_bounded_execution=True,
                    authority_boundary="authority_enforcement_modification",
                    executor_id=None,
                    arguments={"changed_path": f},
                )
            ]
    return []


@dataclass
class ProposedAction(DataClassSerializationMixin):
    """Smallest useful representation of an executable/consequential action."""

    action_type: str
    target_repo: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = "medium"
    requires_bounded_execution: bool = True
    authority_boundary: Optional[str] = None
    evidence_references: Dict[str, Any] = field(default_factory=dict)
    executor_id: Optional[str] = None
    decision_id: Optional[str] = None
    schema: str = PROPOSED_ACTION_SCHEMA_VERSION


def infer_proposed_actions(
    objective: str,
    repo_name: str,
    planned_actions: Optional[List[str]] = None,
    human_approval_requirements: Optional[List[str]] = None,
) -> List[ProposedAction]:
    """
    Infers explicit consequential actions from task metadata and planned actions.
    Distinguishes implementation proposals from executable side effects.
    """
    actions: List[ProposedAction] = []
    seen: set = set()
    combined = f"{objective} {' '.join(planned_actions or [])}".lower()

    for keywords, act_type, risk, boundary, executor in CONSEQUENTIAL_RULES:
        if any(kw in combined for kw in keywords) and act_type not in seen:
            seen.add(act_type)
            actions.append(
                ProposedAction(
                    action_type=act_type,
                    target_repo=repo_name,
                    risk_level=risk,
                    requires_bounded_execution=True,
                    authority_boundary=boundary,
                    executor_id=executor,
                )
            )

    if human_approval_requirements:
        for req in human_approval_requirements:
            if req not in seen:
                seen.add(req)
                exec_id = "howlchangeops" if req == "create_release_candidate" else None
                actions.append(
                    ProposedAction(
                        action_type=req,
                        target_repo=repo_name,
                        risk_level="critical" if "apply" in req or "destructive" in req else "high",
                        requires_bounded_execution=True,
                        authority_boundary=req,
                        executor_id=exec_id,
                    )
                )

    return actions
