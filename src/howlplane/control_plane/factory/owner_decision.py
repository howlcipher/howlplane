"""Plumbing shared by the owner decision paths (parked work items, repository proposals).

Each path keeps its own state rules in its own module; only the parts that must
not diverge live here: the error shape, the exact printed commands, and the
evidence entry written for every decision.
"""

import shlex
from typing import Any, Dict, Optional

from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger


APPROVED = "approved"
REJECTED = "rejected"


class OwnerDecisionError(Exception):
    """The decision cannot be applied; ``code`` is a stable operator error code."""

    def __init__(self, code: str, message: str, why: str, next_action: str, command: Optional[str] = None):
        super().__init__(message)
        self.code, self.why, self.next_action, self.command = code, why, next_action, command


def decision_commands(flag: str, item_id: str, state_dir: str, repo: Optional[str] = None) -> Dict[str, str]:
    """Exact ``approve`` and ``reject`` commands that target one item by ``--flag``."""
    tail = f"--{flag} {shlex.quote(item_id)} --state-dir {shlex.quote(str(state_dir))}"
    if repo is not None:
        tail += f" --repo {shlex.quote(str(repo))}"
    return {"approve": f"howlplane approve {tail}", "reject": f"howlplane reject {tail}"}


def record_decision(
    ledger: Optional[EvidenceLedger], item_id: str, action: str, decision: str, metadata: Dict[str, Any]
) -> Optional[str]:
    """Append the evidence entry for one owner decision; returns its entry id (None without a ledger)."""
    if not ledger:
        return None
    entry = EvidenceEntry(
        task_id=item_id, agent_id="human_operator", action=action, result=decision,
        human_decision=decision, metadata={"operator_source": "cli", **metadata})
    ledger.append_entry(entry)
    return entry.entry_id


def require_state_dir(args: Any, subject: str, what: str) -> None:
    """Refuse an owner-facing command that was given no Factory ``--state-dir``."""
    if getattr(args, "state_dir", None):
        return
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    raise OperatorFailure(OperatorError(
        "MISSING_STATE_DIRECTORY", f"{subject} needs --state-dir.",
        f"The Factory state directory says where the {what} is stored.",
        "Copy the exact command from the Factory status.", "howlplane factory status"))


def not_found_error(error_cls, code: str, label: str, item_id: str, state_dir: Any) -> OwnerDecisionError:
    return error_cls(
        code, f"No {label} '{item_id}' in {state_dir}.", "The id or the state directory is wrong.",
        "List what is waiting and copy the exact command.", "howlplane factory status")


def not_awaiting_error(error_cls, code: str, label: str, item_id: str, state: str, waiting_for: str, why: str):
    return error_cls(
        code, f"{label[0].upper()}{label[1:]} '{item_id}' is '{state}', not awaiting {waiting_for}.", why,
        "List what is still waiting.", "howlplane factory status")
