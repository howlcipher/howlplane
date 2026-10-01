"""Owner decision on a repository proposal awaiting authority.

Mirrors ``work_item_decision``: one narrow path, no second authority system.

* only ``awaiting_authority`` proposals can be decided; a repeat is refused;
* approving marks the proposal ``accepted`` and records the bootstrap contract
  fingerprint that was decided. It creates no repository and runs nothing:
  acceptance only makes the contract eligible for a later, separately governed
  bootstrap path;
* rejecting marks it ``rejected``. ``propose()`` never overwrites an existing
  proposal, so the supervisor will not re-propose a rejected one;
* every decision is written to the evidence ledger.
"""

import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from howlplane.control_plane.factory.repo_proposal import (
    ProposalState,
    RepoProposal,
    RepoProposalStore,
    contract_fingerprint,
)

APPROVED = "approved"
REJECTED = "rejected"
EVIDENCE_ACTION = "repo_proposal_decision"


class ProposalDecisionError(Exception):
    """The decision cannot be applied; ``code`` is a stable operator error code."""

    def __init__(self, code: str, message: str, why: str, next_action: str, command: Optional[str] = None):
        super().__init__(message)
        self.code, self.why, self.next_action, self.command = code, why, next_action, command


def decide_proposal(
    state_dir: Union[str, Path],
    proposal_id: str,
    decision: str,
    *,
    reason: Optional[str] = None,
    decided_by: str = "human_operator",
    ledger: Optional[EvidenceLedger] = None,
) -> Dict[str, Any]:
    """Apply the owner's decision to one proposal awaiting authority and record it."""
    store = RepoProposalStore(Path(state_dir).resolve() / "repo_proposals")
    if not store.exists(proposal_id):
        raise ProposalDecisionError(
            "PROPOSAL_NOT_FOUND", f"No repository proposal '{proposal_id}' in {state_dir}.",
            "The id or the state directory is wrong.",
            "List what is waiting and copy the exact command.", "howlplane factory status")
    try:
        proposal: RepoProposal = store.load(proposal_id)
    except Exception as exc:
        raise ProposalDecisionError(
            "PROPOSAL_MALFORMED", f"Repository proposal '{proposal_id}' cannot be read: {exc}",
            "The stored record is damaged, so no decision is recorded on it.",
            "Inspect the proposal file in the Factory state directory; nothing was changed.",
            "howlplane factory status --verbose")
    if proposal.state != ProposalState.AWAITING_AUTHORITY.value:
        raise ProposalDecisionError(
            "PROPOSAL_NOT_AWAITING_AUTHORITY",
            f"Repository proposal '{proposal_id}' is '{proposal.state}', not awaiting authority.",
            "A proposal can be decided once; this one already has an outcome or was never submitted.",
            "List the proposals still waiting.", "howlplane factory status")

    previous = proposal.state
    fingerprint = contract_fingerprint(proposal)
    proposal.state = (ProposalState.ACCEPTED if decision == APPROVED else ProposalState.REJECTED).value
    proposal.decided_at = datetime.now(timezone.utc).isoformat()
    proposal.decided_by = decided_by
    proposal.decision_reason = reason
    store.save_object(proposal)
    if ledger:
        ledger.append_entry(EvidenceEntry(
            task_id=proposal_id, agent_id="human_operator", action=EVIDENCE_ACTION,
            result=decision, human_decision=decision,
            metadata={"reason": reason, "operator_source": "cli", "previous_state": previous,
                      "new_state": proposal.state, "repository_name": proposal.repository_name,
                      "evidence_fingerprints": list(proposal.evidence_fingerprints),
                      "bootstrap_contract_sha256": fingerprint}))
    return {"proposal_id": proposal_id, "decision": decision, "from_state": previous,
            "state": proposal.state, "reason": reason, "repository_name": proposal.repository_name,
            "bootstrap_contract_sha256": fingerprint}


def proposal_commands(proposal_id: str, state_dir: str) -> Dict[str, str]:
    """Exact approve and reject commands for a proposal awaiting authority."""
    tail = f"--proposal {shlex.quote(proposal_id)} --state-dir {shlex.quote(str(state_dir))}"
    return {"approve": f"howlplane approve {tail}", "reject": f"howlplane reject {tail}"}


__all__: List[str] = ["decide_proposal", "proposal_commands", "ProposalDecisionError",
                      "APPROVED", "REJECTED", "EVIDENCE_ACTION"]
