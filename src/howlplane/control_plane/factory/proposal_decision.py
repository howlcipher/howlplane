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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from howlplane.control_plane.factory import owner_decision as od
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.factory.repo_proposal import (
    ProposalState,
    RepoProposal,
    RepoProposalStore,
    contract_fingerprint,
)

APPROVED, REJECTED = od.APPROVED, od.REJECTED
EVIDENCE_ACTION = "repo_proposal_decision"


class ProposalDecisionError(od.OwnerDecisionError):
    """A repository proposal decision cannot be applied."""


def load_proposal(store: RepoProposalStore, proposal_id: str, state_dir: Union[str, Path], error_cls: type,
                  consequence: str) -> RepoProposal:
    """Load one proposal, refusing an unknown id or a damaged record with ``error_cls``."""
    if not store.exists(proposal_id):
        raise od.not_found_error(error_cls, "PROPOSAL_NOT_FOUND", "repository proposal", proposal_id, state_dir)
    try:
        return store.load(proposal_id)
    except Exception as exc:
        raise error_cls(
            "PROPOSAL_MALFORMED", f"Repository proposal '{proposal_id}' cannot be read: {exc}",
            f"The stored record is damaged, so {consequence}.",
            "Inspect the proposal file in the Factory state directory; nothing was changed.",
            "howlplane factory status --verbose")


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
    proposal = load_proposal(store, proposal_id, state_dir, ProposalDecisionError, "no decision is recorded on it")
    if proposal.state != ProposalState.AWAITING_AUTHORITY.value:
        raise od.not_awaiting_error(
            ProposalDecisionError, "PROPOSAL_NOT_AWAITING_AUTHORITY", "repository proposal", proposal_id,
            proposal.state, "authority",
            "A proposal can be decided once; this one already has an outcome or was never submitted.")

    previous = proposal.state
    fingerprint = contract_fingerprint(proposal)
    proposal.state = (ProposalState.ACCEPTED if decision == APPROVED else ProposalState.REJECTED).value
    proposal.decided_at = datetime.now(timezone.utc).isoformat()
    proposal.decided_by = decided_by
    proposal.decision_reason = reason
    store.save_object(proposal)
    entry_id = od.record_decision(
        ledger, proposal_id, EVIDENCE_ACTION, decision,
        {"reason": reason, "previous_state": previous, "new_state": proposal.state,
         "repository_name": proposal.repository_name,
         "evidence_fingerprints": list(proposal.evidence_fingerprints),
         "bootstrap_contract_sha256": fingerprint})
    if decision == APPROVED:
        proposal.approved_contract_sha256 = fingerprint
        proposal.approved_decision_entry_id = entry_id
        store.save_object(proposal)
    return {"proposal_id": proposal_id, "decision": decision, "from_state": previous,
            "state": proposal.state, "reason": reason, "repository_name": proposal.repository_name,
            "bootstrap_contract_sha256": fingerprint}


def proposal_commands(proposal_id: str, state_dir: str) -> Dict[str, str]:
    """Exact approve and reject commands for a proposal awaiting authority."""
    return od.decision_commands("proposal", proposal_id, state_dir)


__all__ = ["decide_proposal", "load_proposal", "proposal_commands", "ProposalDecisionError", "APPROVED", "REJECTED", "EVIDENCE_ACTION"]
