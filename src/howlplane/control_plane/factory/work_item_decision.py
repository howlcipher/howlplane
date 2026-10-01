"""Owner decision on a parked (``awaiting_owner``) work item.

``approve`` and ``reject`` decide governed tasks through ``HumanLifecycleManager``.
A work item can also park in ``awaiting_owner`` with no governed task waiting
(a restart during dispatch, an authority-boundary block whose task already ended).
This module is the owner's answer for those items and is deliberately narrow so it
is not a second authority path:

* it only applies transitions already on ``WORK_ITEM_TRANSITIONS``
  (``awaiting_owner`` to ``ready`` or ``rejected``);
* it refuses when a linked governed task really is ``awaiting_human``, because
  that decision belongs to ``approve TASK`` and its drift and verification checks;
* approving only requeues the item. Re-dispatch still runs the full governed
  lifecycle, so an authority boundary parks it again unless authority is granted;
* every decision is written to the evidence ledger.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.factory import owner_decision as od
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemState, WorkItemStore
from howlplane.control_plane.task_spec import TaskSpec

APPROVED, REJECTED = od.APPROVED, od.REJECTED


class WorkItemDecisionError(od.OwnerDecisionError):
    """A work item decision cannot be applied."""


def awaiting_human_task(item: Any, target_dir: Union[str, Path, None]) -> Optional[str]:
    """The id of a linked governed task that really is ``awaiting_human``, else None."""
    if target_dir is None:
        return None
    for task_id in item.task_ids:
        task_file = Path(target_dir) / ".task_runs" / task_id / "task.yaml"
        try:
            if TaskSpec.load_from_file(str(task_file)).current_state == "awaiting_human":
                return task_id
        except Exception:
            continue
    return None


def decide_work_item(
    state_dir: Union[str, Path],
    work_item_id: str,
    decision: str,
    *,
    target_dir: Union[str, Path, None] = None,
    reason: Optional[str] = None,
    ledger: Optional[EvidenceLedger] = None,
) -> Dict[str, Any]:
    """Apply the owner's decision to one parked work item and record it."""
    store = WorkItemStore(Path(state_dir).resolve() / "work_items")
    if not store.exists(work_item_id):
        raise od.not_found_error(WorkItemDecisionError, "WORK_ITEM_NOT_FOUND", "work item", work_item_id, state_dir)
    item: WorkItem = store.load(work_item_id)
    if item.state != WorkItemState.AWAITING_OWNER:
        raise od.not_awaiting_error(
            WorkItemDecisionError, "WORK_ITEM_NOT_AWAITING_OWNER", "work item", work_item_id, item.state,
            "an owner decision", "Only items parked for the owner can be decided.")
    task_id = awaiting_human_task(item, target_dir)
    if task_id:
        verb = "approve" if decision == APPROVED else "reject"
        raise WorkItemDecisionError(
            "GOVERNED_TASK_DECISION_REQUIRED",
            f"Work item '{work_item_id}' has governed task '{task_id}' awaiting approval.",
            "That decision is recorded on the task so its drift and verification checks apply.",
            f"Decide the task instead (howlplane {verb} {task_id}).", f"howlplane {verb} {task_id}")

    previous = item.state
    if decision == APPROVED:
        item.transition_to(WorkItemState.READY, reason=reason or "owner_approved")
    else:
        item.transition_to(WorkItemState.REJECTED, reason=reason or "owner_rejected")
    store.save_object(item)
    od.record_decision(
        ledger, work_item_id, "work_item_decision", decision,
        {"reason": reason, "from_state": previous, "to_state": item.state,
         "linked_task_ids": list(item.task_ids)})
    return {"work_item_id": work_item_id, "decision": decision, "from_state": previous,
            "state": item.state, "reason": reason}


def work_item_commands(work_item_id: str, state_dir: str, target_dir: str) -> Dict[str, str]:
    """Exact approve and reject commands for a parked work item."""
    return od.decision_commands("work-item", work_item_id, state_dir, repo=target_dir)


__all__: List[str] = ["decide_work_item", "work_item_commands", "awaiting_human_task",
                      "WorkItemDecisionError", "APPROVED", "REJECTED"]
