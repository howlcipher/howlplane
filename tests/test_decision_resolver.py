"""`howlplane approve|reject ID` finds what the id names; one authority path, fail closed."""

import json
from types import SimpleNamespace

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemState, WorkItemStore
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from tests._product_harness import product  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit


def _task_run(repo, task_id):
    task_dir = repo / ".task_runs" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("task_id: " + task_id + "\n", encoding="utf-8")


def test_a_work_item_id_alone_is_enough(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    code, out, _ = product.run("approve", item.work_item_id)
    assert code == 0 and "REQUEUED" in out
    assert product._supervisor().work_item_store.load(item.work_item_id).state == WorkItemState.READY


def test_a_proposal_id_alone_is_enough(product):
    product.run("start", "--authority", "safe")
    store = product.seed_proposal("RP-17")
    code, out, _ = product.run("approve", "RP-17")
    assert code == 0 and "PROPOSAL ACCEPTED" in out
    assert store.load("RP-17").state == "accepted"


def test_reject_resolves_the_same_way(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    code, out, _ = product.run("reject", item.work_item_id, "--reason", "not now")
    assert code == 0 and "REJECTED" in out
    assert product._supervisor().work_item_store.load(item.work_item_id).state == WorkItemState.REJECTED


def test_a_task_id_goes_to_the_governed_task_path(product, monkeypatch):
    _task_run(product.repo, "TASK-123")
    calls = []

    def approve(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_id=kwargs["task_id"], timestamp="t", reason=None, to_json=lambda: "{}")

    monkeypatch.setattr(cli.HumanLifecycleManager, "approve", staticmethod(approve))
    code, out, _ = product.run("approve", "TASK-123")
    assert code == 0 and calls[0]["task_id"] == "TASK-123"
    assert str(calls[0]["target_repo"]) == str(product.repo)


def test_the_ledger_entry_is_the_one_the_explicit_form_writes(product, tmp_path):
    product.run("start", "--authority", "safe")
    first, second = product.seed_owner_decision(), None
    ledger = tmp_path / "short.jsonl"
    assert product.run("approve", first.work_item_id, "--ledger-file", str(ledger))[0] == 0
    entry = EvidenceLedger(str(ledger)).list_all_entries()[0]
    assert entry.action == "work_item_decision" and entry.human_decision == "approved"
    assert entry.agent_id == "human_operator" and entry.task_id == first.work_item_id


def test_an_id_that_names_two_things_is_refused_not_guessed(product):
    product.run("start", "--authority", "safe")
    state = product._campaign().state_dir
    WorkItemStore(state / "work_items").save_object(WorkItem(
        work_item_id="X-1", fingerprint="f", origin="existing_backlog", repository="r", title="t",
        state=WorkItemState.AWAITING_OWNER))
    product.seed_proposal("X-1")
    code, out, err = product.run("approve", "X-1")
    assert code == 1 and "DECISION_TARGET_AMBIGUOUS" in err and "work item, proposal" in err
    assert WorkItemStore(state / "work_items").load("X-1").state == WorkItemState.AWAITING_OWNER


def test_an_unknown_id_lists_what_is_actually_waiting(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    code, out, err = product.run("approve", "WI-nope")
    assert code == 1 and "DECISION_TARGET_NOT_FOUND" in err and item.work_item_id in err
    assert "howlplane status" in err


def test_no_prefix_matching(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    code, _, err = product.run("approve", item.work_item_id[:12])
    assert code == 1 and "DECISION_TARGET_NOT_FOUND" in err


def test_unknown_id_without_a_factory_is_a_friendly_error_not_a_bug_report(product):
    code, out, err = product.run("approve", "TASK-404")
    assert code == 1
    assert "DECISION_TARGET_NOT_FOUND" in err and "INTERNAL_ERROR" not in err


def test_explicit_flags_still_work_and_state_dir_stays_an_override(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    state = str(product._campaign().state_dir)
    code, out, _ = product.run("approve", "--work-item", item.work_item_id, "--state-dir", state, "--json")
    assert code == 0 and json.loads(out)["state"] == WorkItemState.READY


def test_explicit_work_item_flag_discovers_the_state_dir(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    assert product.run("approve", "--work-item", item.work_item_id)[0] == 0


def test_no_target_still_asks_for_one(product):
    code, out, err = product.run("approve")
    assert code == 1 and "DECISION_TARGET_REQUIRED" in err and "howlplane status" in err


def test_unknown_command_is_one_friendly_line_with_exit_2(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=f"{root / 'src'}:{root}")
    done = subprocess.run([sys.executable, "-m", "howlplane.control_plane.cli", "frobnicate"],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert done.returncode == 2
    assert "unknown command 'frobnicate'" in done.stderr and "--help" in done.stderr
    assert "init-task" not in done.stderr and "choose from" not in done.stderr
