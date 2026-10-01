"""Resolve a bare ``howlplane approve ID`` / ``reject ID`` to the one thing it names.

An owner should not need to know whether an id is a governed task, a parked Factory
work item or a repository proposal, nor where the Factory keeps its state. This
module only *finds* the target (read-only) and hands it back to the existing
decision paths in ``governance_cli``; it adds no approval authority of its own.

Resolution is exact and fail-closed: an id is accepted only when exactly one valid
target carries it. No prefix matching, no guessing.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure

TASK, WORK_ITEM, PROPOSAL = "task", "work_item", "proposal"


@dataclass(frozen=True)
class DecisionTarget:
    kind: str
    item_id: str
    state_dir: Optional[str] = None
    repo: Optional[str] = None


def discover_campaign(args: argparse.Namespace):
    """The campaign for the current repository (or ``--state-dir``), without creating anything.

    Returns None when there is no repository to discover from. Several active
    campaigns for one repository is ambiguous and refused with how to choose.
    """
    from howlplane.control_plane.factory.campaign import CampaignError, campaign_from_state_dir, resolve_campaign
    explicit = getattr(args, "state_dir", None)
    if explicit:
        return campaign_from_state_dir(explicit)
    try:
        return resolve_campaign(getattr(args, "repo", None), prefer_active=True)
    except CampaignError as exc:
        if "Multiple active" in str(exc):
            raise OperatorFailure(OperatorError(
                "DECISION_CAMPAIGN_AMBIGUOUS", "More than one HowlPlane run is active for this repository.",
                str(exc), "Name the one you mean.", "howlplane approve ID --state-dir PATH")) from exc
        return None


def _task_dirs(args: argparse.Namespace, campaign) -> List[Path]:
    """Repositories whose ``.task_runs`` may hold governed tasks: the checkout and the isolated Factory worktree."""
    dirs: List[Path] = []
    from howlplane.control_plane.launcher import find_git_repo_root
    try:
        dirs.append(Path(find_git_repo_root(getattr(args, "repo", None))))
    except Exception:
        pass
    if campaign is not None and Path(campaign.target_dir).is_dir() and Path(campaign.target_dir) not in dirs:
        dirs.append(Path(campaign.target_dir))
    return dirs


def _exists(store, item_id: str) -> bool:
    try:
        return bool(store.exists(item_id))
    except Exception:
        return False


def _awaiting_tasks(task_dirs: List[Path]) -> List[str]:
    from howlplane.control_plane.task_spec import TaskSpec
    found: List[str] = []
    for base in task_dirs:
        for task_file in sorted((base / ".task_runs").glob("*/task.yaml")):
            try:
                if TaskSpec.load_from_file(str(task_file)).current_state == "awaiting_human":
                    found.append(task_file.parent.name)
            except Exception:
                continue
    return found


def waiting_for_decision(args: argparse.Namespace) -> List[str]:
    """Ids currently waiting for an owner decision, for error messages."""
    from howlplane.control_plane.factory.repo_proposal import RepoProposalStore
    from howlplane.control_plane.factory.work_item import WorkItemState, WorkItemStore
    campaign = discover_campaign(args)
    ids: List[str] = []
    if campaign is not None:
        state = Path(campaign.state_dir)
        if (state / "work_items").is_dir():
            ids += [i.work_item_id for i in WorkItemStore(state / "work_items").list_all()
                    if i.state == WorkItemState.AWAITING_OWNER]
        if (state / "repo_proposals").is_dir():
            ids += [p.proposal_id for p in RepoProposalStore(state / "repo_proposals").list_awaiting_authority()]
    return ids + _awaiting_tasks(_task_dirs(args, campaign))


def resolve_decision_target(args: argparse.Namespace) -> Optional[DecisionTarget]:
    """The single target the positional id names, or None to use the plain task path.

    None means nothing in a Factory campaign carries the id and no campaign state
    exists to search, so the established task command reports its own error.
    """
    from howlplane.control_plane.factory.repo_proposal import RepoProposalStore
    from howlplane.control_plane.factory.work_item import WorkItemStore
    item_id = args.task_id
    campaign = discover_campaign(args)
    matches: List[DecisionTarget] = []
    searched_state = False
    if campaign is not None:
        state = Path(campaign.state_dir)
        if (state / "work_items").is_dir():
            searched_state = True
            if _exists(WorkItemStore(state / "work_items"), item_id):
                matches.append(DecisionTarget(WORK_ITEM, item_id, state_dir=str(state)))
        if (state / "repo_proposals").is_dir():
            searched_state = True
            if _exists(RepoProposalStore(state / "repo_proposals"), item_id):
                matches.append(DecisionTarget(PROPOSAL, item_id, state_dir=str(state)))
    for base in _task_dirs(args, campaign):
        if (base / ".task_runs" / item_id / "task.yaml").is_file():
            matches.append(DecisionTarget(TASK, item_id, repo=str(base)))
            break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        kinds = ", ".join(m.kind.replace("_", " ") for m in matches)
        raise OperatorFailure(OperatorError(
            "DECISION_TARGET_AMBIGUOUS", f"'{item_id}' names more than one thing.",
            f"It matches: {kinds}. HowlPlane will not guess which one you mean.",
            "Say which one.", f"howlplane approve --work-item {item_id}"))
    if not searched_state:
        return None
    waiting = waiting_for_decision(args)
    raise OperatorFailure(OperatorError(
        "DECISION_TARGET_NOT_FOUND", f"Nothing called '{item_id}' is waiting for a decision.",
        "Checked this repository's work items, repository proposals and task runs."
        + (f" Waiting now: {', '.join(waiting)}." if waiting else " Nothing is waiting right now."),
        "See what HowlPlane needs from you.", "howlplane status"))


def apply_target(args: argparse.Namespace, target: DecisionTarget) -> None:
    """Point ``args`` at the resolved target so the existing decision path handles it."""
    if target.kind == WORK_ITEM:
        args.work_item, args.task_id, args.state_dir = target.item_id, None, target.state_dir
    elif target.kind == PROPOSAL:
        args.proposal, args.task_id, args.state_dir = target.item_id, None, target.state_dir
    elif target.repo:
        args.repo = target.repo
