"""Owner decisions on parked work items (backlog row 79)."""

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.factory import factory_cli
from howlplane.control_plane.factory import work_item_decision as wid
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemState, WorkItemStore
from howlplane.control_plane.presentation.errors import OperatorFailure

pytestmark = pytest.mark.unit


def _parked(tmp_path, state=WorkItemState.AWAITING_OWNER, task_ids=()):
    store = WorkItemStore(tmp_path / "state" / "work_items")
    item = WorkItem(work_item_id="WI-9", fingerprint="f", origin="existing_backlog", repository="r", title="t",
                    state=state, task_ids=list(task_ids))
    store.save_object(item)
    return store


def test_printed_commands_parse_and_act_on_that_item(tmp_path):
    store = _parked(tmp_path)
    state_dir, repo = tmp_path / "state dir", tmp_path / "repo"
    (tmp_path / "state").rename(state_dir)
    decisions = factory_cli._owner_decisions(WorkItemStore(state_dir / "work_items"), repo, state_dir)
    assert [d["kind"] for d in decisions] == ["work_item"]
    for verb in ("approve", "reject"):
        argv = shlex.split(decisions[0][verb])
        assert argv[0] == "howlplane"
        args = cli.build_parser().parse_args(argv[1:])
        assert args.work_item == "WI-9" and args.task_id is None
        assert Path(args.state_dir) == state_dir and Path(args.repo) == repo


def test_approve_requeues_and_writes_ledger(tmp_path):
    _parked(tmp_path)
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    out = wid.decide_work_item(tmp_path / "state", "WI-9", "approved", reason="ok", ledger=ledger)
    assert out["state"] == WorkItemState.READY
    assert WorkItemStore(tmp_path / "state" / "work_items").load("WI-9").state == WorkItemState.READY
    entry = ledger.list_all_entries()[0]
    assert entry.action == "work_item_decision" and entry.human_decision == "approved"
    assert entry.agent_id == "human_operator" and entry.task_id == "WI-9"


def test_reject_is_terminal(tmp_path):
    _parked(tmp_path)
    assert wid.decide_work_item(tmp_path / "state", "WI-9", "rejected")["state"] == WorkItemState.REJECTED


@pytest.mark.parametrize("state", [WorkItemState.READY, WorkItemState.BLOCKED, WorkItemState.SHIPPED])
def test_only_awaiting_owner_items_can_be_decided(tmp_path, state):
    _parked(tmp_path, state=state)
    with pytest.raises(wid.WorkItemDecisionError) as err:
        wid.decide_work_item(tmp_path / "state", "WI-9", "approved")
    assert err.value.code == "WORK_ITEM_NOT_AWAITING_OWNER"
    assert WorkItemStore(tmp_path / "state" / "work_items").load("WI-9").state == state


def test_unknown_item_is_reported(tmp_path):
    with pytest.raises(wid.WorkItemDecisionError) as err:
        wid.decide_work_item(tmp_path, "nope", "approved")
    assert err.value.code == "WORK_ITEM_NOT_FOUND"


def test_governed_task_awaiting_human_is_not_decided_here(tmp_path, monkeypatch):
    """One authority: a task that awaits approval is decided by approve TASK, never by this path."""
    _parked(tmp_path, task_ids=["FACTORY-WI-9"])
    monkeypatch.setattr(wid.TaskSpec, "load_from_file",
                        staticmethod(lambda path: SimpleNamespace(current_state="awaiting_human")))
    with pytest.raises(wid.WorkItemDecisionError) as err:
        wid.decide_work_item(tmp_path / "state", "WI-9", "approved", target_dir=tmp_path)
    assert err.value.code == "GOVERNED_TASK_DECISION_REQUIRED"
    assert err.value.command == "howlplane approve FACTORY-WI-9"
    assert WorkItemStore(tmp_path / "state" / "work_items").load("WI-9").state == WorkItemState.AWAITING_OWNER


def test_task_awaiting_human_keeps_the_task_command(tmp_path, monkeypatch):
    store = _parked(tmp_path, task_ids=["FACTORY-WI-9"])
    monkeypatch.setattr(factory_cli.TaskSpec, "load_from_file",
                        staticmethod(lambda path: SimpleNamespace(current_state="awaiting_human")))
    [decision] = factory_cli._owner_decisions(store, tmp_path, tmp_path / "state")
    assert decision["kind"] == "task" and decision["approve"].startswith("howlplane approve FACTORY-WI-9 ")


def test_cli_requires_exactly_one_target(tmp_path):
    parser = cli.build_parser()
    for argv in (["approve"], ["approve", "T-1", "--work-item", "WI-9"]):
        with pytest.raises(OperatorFailure) as err:
            cli._handle_decision(parser.parse_args(argv), "approved")
        assert err.value.error.code == "DECISION_TARGET_REQUIRED"


def test_cli_work_item_end_to_end_json(tmp_path, capsys):
    _parked(tmp_path)
    args = cli.build_parser().parse_args([
        "approve", "--work-item", "WI-9", "--state-dir", str(tmp_path / "state"),
        "--repo", str(tmp_path), "--ledger-file", str(tmp_path / "l.jsonl"), "--json"])
    assert cli.cmd_approve(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == WorkItemState.READY
    assert (tmp_path / "l.jsonl").exists()
