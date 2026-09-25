#!/usr/bin/env python3
"""
reconciliation.py

Review reconciliation workflow that processes multiple reviewer findings,
classifies agreement/disagreement, and prevents silent suppression of defects.
"""

from dataclasses import dataclass, field, asdict
import json, yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

REVIEW_FINDING_SCHEMA_VERSION = "ai.review_finding/v1"

VALID_SEVERITIES = {"blocker", "high", "medium", "low", "informational"}
VALID_FINDING_STATUSES = {
    "open",
    "confirmed",
    "likely",
    "disputed",
    "false_positive",
    "out_of_scope",
    "requires_human_judgment",
}
VALID_REDUNDANCY_STATUSES = {"independent", "duplicate", "overlapping"}


class ReconciliationValidationError(ValueError):
    """Raised when finding reconciliation violates safety rules (e.g. dismissing blocker without reason)."""
    pass


from howlplane.control_plane.task_spec import DataClassSerializationMixin


@dataclass
class ReviewFinding(DataClassSerializationMixin):
    """Represents a single finding discovered during review."""

    id: str
    reviewer_role: str
    title: str
    severity: str
    category: str
    description: str
    status: str = "open"
    component: Optional[str] = None
    claim: Optional[str] = None
    location: Optional[str] = None
    evidence: Optional[str] = None
    suggested_fix: Optional[str] = None
    related_finding_ids: List[str] = field(default_factory=list)
    redundancy_status: str = "independent"  # "independent", "duplicate", "overlapping"
    resolution_reason: Optional[str] = None
    schema: str = REVIEW_FINDING_SCHEMA_VERSION

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ReconciliationValidationError("Finding id must be a non-empty string.")
        if self.severity not in VALID_SEVERITIES:
            raise ReconciliationValidationError(
                f"Finding severity '{self.severity}' invalid. Allowed: {sorted(VALID_SEVERITIES)}"
            )
        if self.status not in VALID_FINDING_STATUSES:
            raise ReconciliationValidationError(
                f"Finding status '{self.status}' invalid. Allowed: {sorted(VALID_FINDING_STATUSES)}"
            )
        if self.redundancy_status not in VALID_REDUNDANCY_STATUSES:
            raise ReconciliationValidationError(
                f"redundancy_status '{self.redundancy_status}' invalid. Allowed: {sorted(VALID_REDUNDANCY_STATUSES)}"
            )

        # Rule: Any dismissed blocker or high finding must have a resolution reason
        if self.status in ("false_positive", "out_of_scope") and self.severity in ("blocker", "high"):
            if not self.resolution_reason or not self.resolution_reason.strip():
                raise ReconciliationValidationError(
                    f"Finding '{self.id}' with severity '{self.severity}' cannot be marked '{self.status}' "
                    "without an explicit non-empty resolution_reason."
                )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewFinding":
        d = dict(data)
        d.pop("schema", None)
        return cls(schema=REVIEW_FINDING_SCHEMA_VERSION, **d)


@dataclass
class ReconciliationResult:
    """Summary and categorized output of finding reconciliation."""

    findings: List[ReviewFinding]
    confirmed: List[ReviewFinding] = field(default_factory=list)
    likely: List[ReviewFinding] = field(default_factory=list)
    disputed: List[ReviewFinding] = field(default_factory=list)
    false_positives: List[ReviewFinding] = field(default_factory=list)
    out_of_scope: List[ReviewFinding] = field(default_factory=list)
    requires_human_judgment: List[ReviewFinding] = field(default_factory=list)
    unresolved_blockers: int = 0
    unresolved_highs: int = 0
    duplicate_count: int = 0
    overlapping_count: int = 0
    unique_findings_by_role: Dict[str, int] = field(default_factory=dict)
    findings_by_role: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "total_findings": len(self.findings),
                "confirmed_count": len(self.confirmed),
                "likely_count": len(self.likely),
                "disputed_count": len(self.disputed),
                "false_positive_count": len(self.false_positives),
                "out_of_scope_count": len(self.out_of_scope),
                "requires_human_count": len(self.requires_human_judgment),
                "unresolved_blockers": self.unresolved_blockers,
                "unresolved_highs": self.unresolved_highs,
                "duplicate_count": self.duplicate_count,
                "overlapping_count": self.overlapping_count,
                "unique_findings_by_role": self.unique_findings_by_role,
                "findings_by_role": self.findings_by_role,
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def render_markdown(self) -> str:
        """Generates a human-readable reconciliation report."""
        lines = [
            "# Review Reconciliation Report",
            "",
            "## Summary",
            f"- **Total findings:** {len(self.findings)}",
            f"- **Confirmed findings:** {len(self.confirmed)}",
            f"- **Likely findings:** {len(self.likely)}",
            f"- **Disputed findings:** {len(self.disputed)}",
            f"- **Requires human judgment:** {len(self.requires_human_judgment)}",
            f"- **False positives (dismissed with reason):** {len(self.false_positives)}",
            f"- **Out of scope (dismissed with reason):** {len(self.out_of_scope)}",
            f"- **Duplicates detected:** {self.duplicate_count}",
            f"- **Overlapping findings:** {self.overlapping_count}",
            f"- **Unresolved Blockers / Highs:** {self.unresolved_blockers + self.unresolved_highs}",
            "",
            "## Findings & Unique Signal by Reviewer Role",
            "| Reviewer Role | Total Findings | Unique Findings | Duplicates / Overlaps |",
            "| --- | --- | --- | --- |",
        ]
        for role, total in sorted(self.findings_by_role.items()):
            unique = self.unique_findings_by_role.get(role, 0)
            dups = total - unique
            lines.append(f"| `{role}` | {total} | {unique} | {dups} |")
        lines.append("")

        if self.confirmed:
            lines.append("## Confirmed Findings (Multi-Reviewer Agreement / Verified)")
            for f in self.confirmed:
                redundancy_tag = f" `[{f.redundancy_status}]`" if f.redundancy_status != "independent" else ""
                lines.append(f"- **[{f.severity.upper()}] {f.id}**: {f.title} ({f.reviewer_role}){redundancy_tag}")
                if f.location:
                    lines.append(f"  - Location: `{f.location}`")
                lines.append(f"  - Description: {f.description}")
                if f.suggested_fix:
                    lines.append(f"  - Suggested fix: {f.suggested_fix}")
            lines.append("")

        if self.likely:
            lines.append("## Likely Findings (Single Reviewer High Confidence)")
            for f in self.likely:
                redundancy_tag = f" `[{f.redundancy_status}]`" if f.redundancy_status != "independent" else ""
                lines.append(f"- **[{f.severity.upper()}] {f.id}**: {f.title} ({f.reviewer_role}){redundancy_tag}")
                if f.location:
                    lines.append(f"  - Location: `{f.location}`")
                lines.append(f"  - Description: {f.description}")
                if f.suggested_fix:
                    lines.append(f"  - Suggested fix: {f.suggested_fix}")
            lines.append("")

        if self.disputed:
            lines.append("## Disputed Findings (Requires Resolution)")
            for f in self.disputed:
                lines.append(f"- **[{f.severity.upper()}] {f.id}**: {f.title} ({f.reviewer_role})")
                lines.append(f"  - Description: {f.description}")
                if f.resolution_reason:
                    lines.append(f"  - Dispute notes: {f.resolution_reason}")
            lines.append("")

        if self.requires_human_judgment:
            lines.append("## Requires Human Judgment")
            for f in self.requires_human_judgment:
                lines.append(f"- **[{f.severity.upper()}] {f.id}**: {f.title} ({f.reviewer_role})")
                lines.append(f"  - Reason for human review: {f.resolution_reason or f.description}")
            lines.append("")

        return "\n".join(lines)


class ReviewReconciler:
    """Performs deterministic reconciliation of multi-agent review findings."""

    @staticmethod
    def reconcile(findings: List[ReviewFinding]) -> ReconciliationResult:
        """
        Reconciles a collection of review findings into classified categories.
        Ensures all validations pass and preserves every disagreement.
        """
        # Validate all findings first
        for f in findings:
            f.validate()

        confirmed: List[ReviewFinding] = []
        likely: List[ReviewFinding] = []
        disputed: List[ReviewFinding] = []
        false_positives: List[ReviewFinding] = []
        out_of_scope: List[ReviewFinding] = []
        requires_human: List[ReviewFinding] = []

        unresolved_blockers = 0
        unresolved_highs = 0
        duplicate_count = 0
        overlapping_count = 0
        findings_by_role: Dict[str, int] = {}
        unique_findings_by_role: Dict[str, int] = {}

        # Index findings by location and component
        location_map: Dict[str, List[ReviewFinding]] = {}
        for f in findings:
            findings_by_role[f.reviewer_role] = findings_by_role.get(f.reviewer_role, 0) + 1
            if f.location:
                loc_key = f.location.strip().lower()
                location_map.setdefault(loc_key, []).append(f)

        # Detect cross-reviewer duplicates and overlaps
        processed_pairs = set()
        for f in findings:
            if f.location:
                loc_key = f.location.strip().lower()
                co_located = location_map.get(loc_key, [])
                distinct_reviewers = {other.reviewer_role for other in co_located if other.id != f.id}

                if distinct_reviewers:
                    # Match found across different reviewer roles
                    other_findings = [other for other in co_located if other.id != f.id]
                    for other in other_findings:
                        pair = tuple(sorted([f.id, other.id]))
                        if pair not in processed_pairs:
                            processed_pairs.add(pair)
                            if other.id not in f.related_finding_ids:
                                f.related_finding_ids.append(other.id)
                            if f.id not in other.related_finding_ids:
                                other.related_finding_ids.append(f.id)

                    # Determine redundancy classification
                    if f.redundancy_status == "independent":
                        # If first finding for this location, mark overlapping; if subsequent, duplicate
                        first_for_loc = co_located[0].id == f.id
                        if first_for_loc:
                            f.redundancy_status = "overlapping"
                            overlapping_count += 1
                        else:
                            f.redundancy_status = "duplicate"
                            duplicate_count += 1
                else:
                    if f.redundancy_status == "duplicate":
                        duplicate_count += 1
                    elif f.redundancy_status == "overlapping":
                        overlapping_count += 1
            else:
                if f.redundancy_status == "duplicate":
                    duplicate_count += 1
                elif f.redundancy_status == "overlapping":
                    overlapping_count += 1

            if f.redundancy_status == "independent":
                unique_findings_by_role[f.reviewer_role] = unique_findings_by_role.get(f.reviewer_role, 0) + 1

        for f in findings:
            # Check if status was already set or should be deduced
            if f.status == "open":
                # Auto-classification logic
                loc_matches = location_map.get(f.location.strip().lower(), []) if f.location else []
                reviewers_at_loc = {m.reviewer_role for m in loc_matches}

                if len(reviewers_at_loc) > 1:
                    # Multiple distinct reviewers flagged this location
                    f.status = "confirmed"
                elif f.severity in ("blocker", "high") and f.category == "security":
                    f.status = "requires_human_judgment"
                    f.resolution_reason = "Security-critical finding automatically routed for human verification."
                elif f.severity in ("blocker", "high"):
                    f.status = "likely"
                else:
                    f.status = "likely"

            # Sort into buckets
            if f.status == "confirmed":
                confirmed.append(f)
            elif f.status == "likely":
                likely.append(f)
            elif f.status == "disputed":
                disputed.append(f)
            elif f.status == "false_positive":
                false_positives.append(f)
            elif f.status == "out_of_scope":
                out_of_scope.append(f)
            elif f.status == "requires_human_judgment":
                requires_human.append(f)

            # Count unresolved severe items
            if f.status in ("confirmed", "likely", "disputed", "requires_human_judgment"):
                if f.severity == "blocker":
                    unresolved_blockers += 1
                elif f.severity == "high":
                    unresolved_highs += 1

        return ReconciliationResult(
            findings=findings,
            confirmed=confirmed,
            likely=likely,
            disputed=disputed,
            false_positives=false_positives,
            out_of_scope=out_of_scope,
            requires_human_judgment=requires_human,
            unresolved_blockers=unresolved_blockers,
            unresolved_highs=unresolved_highs,
            duplicate_count=duplicate_count,
            overlapping_count=overlapping_count,
            unique_findings_by_role=unique_findings_by_role,
            findings_by_role=findings_by_role,
        )
