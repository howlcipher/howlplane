#!/usr/bin/env python3
"""Dispatcher adapter that hands selected factory work to the governed lifecycle."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.control_plane.factory.work_item import WorkItem, WorkItemState
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine


@dataclass
class DispatchOutcome:
    success: bool
    work_item_id: str
    next_work_item_state: str
    reason: str
    git_record: Optional[Dict[str, Any]] = None
    blocker: Optional[str] = None
    retry_after: Optional[str] = None
    requires_authority: bool = False
    provider_unavailable: bool = False
    failure_class: Optional[str] = None
    failure_code: Optional[str] = None
    task_id: Optional[str] = None
    dispatch_id: Optional[str] = None
    attempt: int = 1


class MarathonDispatcherAdapter:
    """Routes a factory WorkItem through MarathonDogfoodEngine's governed path."""

    def __init__(
        self,
        engine_factory: Callable[[], MarathonDogfoodEngine],
    ):
        self._engine_factory = engine_factory

    @staticmethod
    def _files_changed_from_work_item(work_item: WorkItem) -> List[str]:
        """Extract repo-relative paths from work item evidence for boundary evaluation."""
        paths: List[str] = []
        for ref in work_item.evidence_refs or []:
            path = ref.split("#", 1)[0]
            if path:
                paths.append(path)
        source_ref = work_item.source_ref or {}
        source_file = source_ref.get("source_file") or source_ref.get("file")
        if source_file:
            paths.append(str(source_file))
        return paths

    def dispatch(
        self,
        work_item: WorkItem,
        dispatch_id: str,
        task_id: str,
    ) -> DispatchOutcome:
        engine = self._engine_factory()
        files_changed = self._files_changed_from_work_item(work_item)
        success, git_record = engine.execute_factory_work_item(
            work_item, files_changed=files_changed
        )
        if git_record and git_record.get("integration_mode") == "parked":
            return DispatchOutcome(
                success=False,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.AWAITING_OWNER,
                reason="parked_awaiting_human_authority",
                git_record=git_record,
                blocker="authority_boundary",
                requires_authority=True,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        if success:
            return DispatchOutcome(
                success=True,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.SHIPPED,
                reason="governed_lifecycle_completed",
                git_record=git_record,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        record = git_record or {}
        failure_reason = record.get("failure_reason", "unknown")
        failure_class = record.get("failure_class")
        failure_code = record.get("failure_code")
        if failure_class in {"PROVIDER_EXHAUSTED", "PROVIDER_UNAVAILABLE"}:
            return DispatchOutcome(
                success=False,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.DEFERRED,
                reason=failure_reason,
                git_record=git_record,
                blocker="provider_unavailable",
                provider_unavailable=True,
                failure_class=failure_class,
                failure_code=failure_code,
                retry_after=record.get("retry_after"),
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        if failure_class == "AUTHORITY_BLOCKED":
            return DispatchOutcome(
                success=False,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.AWAITING_OWNER,
                reason=failure_reason,
                git_record=git_record,
                blocker="authority_boundary",
                requires_authority=True,
                failure_class=failure_class,
                failure_code=failure_code,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        if failure_class == "DEPENDENCY_BLOCKED":
            return DispatchOutcome(
                success=False,
                work_item_id=work_item.work_item_id,
                next_work_item_state=WorkItemState.BLOCKED,
                reason=failure_reason,
                git_record=git_record,
                blocker=record.get("blocker_id") or "dependency",
                failure_class=failure_class,
                failure_code=failure_code,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
        return DispatchOutcome(
            success=False,
            work_item_id=work_item.work_item_id,
            next_work_item_state=WorkItemState.FAILED,
            reason=failure_reason,
            git_record=git_record,
            task_id=task_id,
            dispatch_id=dispatch_id,
        )
