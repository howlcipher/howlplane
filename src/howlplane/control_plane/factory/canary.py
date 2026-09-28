#!/usr/bin/env python3
"""Safe, bounded canary engine for Factory acceptance testing.

A canary dispatches synthetic owner-direction work items through a lightweight
engine that only verifies routing and isolation.  It never invokes a real AI
provider, never opens backlog sources, and never mutates the operator's source
checkout.  The resulting worktree changes are limited to a single temporary
marker file that is removed immediately after verification.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from howlplane.control_plane.factory.campaign import canonical_path
from howlplane.control_plane.factory.dispatcher import DispatchOutcome
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemState


CANARY_ORIGIN = "owner_direction"
CANARY_KIND = "factory_routing_canary"


class CanaryProviderPool:
    """Provider pool stand-in that always reports capacity for canary runs."""

    def has_available_providers(self) -> bool:
        return True

    def inventory(self) -> List[Dict[str, Any]]:
        return []

    def reset_transient_exhaustion(self) -> None:
        pass


def canary_evidence_for_workspace(workspace: Any) -> List[Dict[str, Any]]:
    """Return one synthetic canary evidence dict per workspace repository."""
    from howlplane.control_plane.factory.target import Workspace

    ws: Workspace = workspace
    evidence: List[Dict[str, Any]] = []
    for repo in ws.repositories:
        slug = repo.repository
        evidence.append(
            {
                "origin": CANARY_ORIGIN,
                "repository": slug,
                "title": f"Factory routing canary for {slug}",
                "description": (
                    "Verify that the Factory dispatches a work item to the correct "
                    f"repository-specific managed worktree for {slug} without mutating "
                    "the operator checkout."
                ),
                "identity_keys": [f"factory-canary:{slug}"],
                "evidence_refs": [],
                "evidence_fingerprints": [f"factory-canary:{slug}"],
                "source_file_rank": 0,
                "source_rank": 0,
                "kind": CANARY_KIND,
                "trusted_provenance": True,
            }
        )
    return evidence


@dataclass
class CanaryEngine:
    """Lightweight engine that proves routing and isolation for one repository."""

    target_repo: Path
    repo_slug: str
    allowed_marker_parents: List[Path] = field(default_factory=list)

    def execute_factory_work_item(
        self,
        work_item: WorkItem,
        files_changed: Optional[List[str]] = None,
        dispatch_id: Optional[str] = None,
        run_mode: str = "continuous",
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Verify the work item routes to the expected repository worktree."""
        dispatch_id = dispatch_id or work_item.work_item_id
        actual_target = canonical_path(self.target_repo)

        # Refuse to operate outside an explicitly Factory-managed worktree. The
        # operator checkout must never be touched by a canary.
        if not self._is_managed_worktree(actual_target):
            return False, {
                "task_id": work_item.work_item_id,
                "target_repo": str(actual_target),
                "repo_slug": self.repo_slug,
                "integration_mode": "parked",
                "failure_reason": "CANARY_TARGET_NOT_MANAGED",
                "failure_class": "AUTHORITY_BLOCKED",
                "failure_code": "canary_target_not_managed",
            }

        # Refuse if the work item's repository identity does not match this
        # engine's configured repository.
        if work_item.repository != self.repo_slug:
            return False, {
                "task_id": work_item.work_item_id,
                "target_repo": str(actual_target),
                "repo_slug": self.repo_slug,
                "work_item_repository": work_item.repository,
                "integration_mode": "parked",
                "failure_reason": "CANARY_REPOSITORY_MISMATCH",
                "failure_class": "AUTHORITY_BLOCKED",
                "failure_code": "canary_repository_mismatch",
            }

        marker = actual_target / ".factory_canary" / f"{dispatch_id}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        marker.write_text(
            f"Factory routing canary for {self.repo_slug}\n"
            f"work_item: {work_item.work_item_id}\n"
            f"dispatch_id: {dispatch_id}\n"
            f"verified_at: {now}\n",
            encoding="utf-8",
        )

        if not marker.exists():
            return False, {
                "task_id": work_item.work_item_id,
                "target_repo": str(actual_target),
                "repo_slug": self.repo_slug,
                "integration_mode": "parked",
                "failure_reason": "CANARY_MARKER_NOT_WRITTEN",
                "failure_class": "DEPENDENCY_BLOCKED",
                "failure_code": "canary_marker_not_written",
            }

        # Remove the marker and the empty parent directory to leave no lasting
        # canary artifacts.
        try:
            marker.unlink()
            marker.parent.rmdir()
        except OSError:
            pass

        return True, {
            "target_repo": str(actual_target),
            "repo_slug": self.repo_slug,
            "work_item_repository": work_item.repository,
            "canary_marker": str(marker),
            "integration_mode": "canary",
            "dispatch_id": dispatch_id,
        }

    def _is_managed_worktree(self, path: Path) -> bool:
        for parent in self.allowed_marker_parents:
            try:
                path.relative_to(parent)
                return True
            except ValueError:
                pass
        return False


class CanaryDispatcherAdapter:
    """Routes canary work items to the per-repository CanaryEngine."""

    def __init__(self, engines: Dict[str, CanaryEngine]):
        self.engines = engines

    def dispatch(
        self,
        work_item: WorkItem,
        dispatch_id: str,
        task_id: str,
        run_mode: str = "continuous",
    ) -> DispatchOutcome:
        engine = self.engines.get(work_item.repository)
        if engine is None:
            return DispatchOutcome(
                success=False,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.FAILED,
                reason="canary_engine_not_found",
                blocker="canary_engine_not_found",
                failure_class="DEPENDENCY_BLOCKED",
                failure_code="canary_engine_not_found",
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        success, git_record = engine.execute_factory_work_item(
            work_item, files_changed=[], dispatch_id=dispatch_id, run_mode=run_mode
        )
        if success:
            return DispatchOutcome(
                success=True,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.SHIPPED,
                reason="canary_routing_verified",
                git_record=git_record,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        return DispatchOutcome(
            success=False,
            work_item_id=work_item.work_item_id,
            next_work_item_state=WorkItemState.FAILED,
            reason=git_record.get("failure_reason", "canary_failed"),
            git_record=git_record,
            blocker=git_record.get("failure_code"),
            failure_class=git_record.get("failure_class", "DEPENDENCY_BLOCKED"),
            failure_code=git_record.get("failure_code", "canary_failed"),
            task_id=task_id,
            dispatch_id=dispatch_id,
        )
