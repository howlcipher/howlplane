"""Owner decisions on repository proposals awaiting authority (backlog #81)."""

import json
import shlex
from pathlib import Path

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.factory import factory_cli
from howlplane.control_plane.factory import proposal_decision as pd
from howlplane.control_plane.factory.repo_proposal import (
    ProposalState,
    RepoProposalStore,
    contract_fingerprint,
)
from howlplane.control_plane.presentation.errors import OperatorFailure
from howlplane.control_plane.presentation.operator import derive_operator_status, render_operator_text

pytestmark = pytest.mark.unit


def _store(state_dir):
    return RepoProposalStore(Path(state_dir) / "repo_proposals")


def _propose(state_dir, proposal_id="RP-003", name="normalizer"):
    store = _store(state_dir)
    store.propose(proposal_id, name, "propose_new_repository", "reusable normalization service",
                  ["fp-b", "fp-a"], {"capability_id": "normalization"})
    return store


def test_printed_commands_parse_and_target_that_proposal(tmp_path):
    state_dir = tmp_path / "state dir"
    store = _propose(state_dir)
    [decision] = factory_cli._proposal_decisions(store, state_dir)
    assert decision["kind"] == "proposal" and decision["proposal_id"] == "RP-003"
    for verb in ("approve", "reject"):
        argv = shlex.split(decision[verb])
        assert argv[0] == "howlplane"
        args = cli.build_parser().parse_args(argv[1:])
        assert args.proposal == "RP-003" and args.task_id is None and args.work_item is None
        assert Path(args.state_dir) == state_dir


def test_approve_marks_accepted_with_evidence_and_creates_nothing(tmp_path):
    store = _propose(tmp_path)
    before = store.load("RP-003")
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    before_files = sorted(p.name for p in tmp_path.rglob("*"))
    out = pd.decide_proposal(tmp_path, "RP-003", "approved", reason="fits roadmap", ledger=ledger)
    decided = store.load("RP-003")
    assert decided.state == ProposalState.ACCEPTED.value and out["state"] == "accepted"
    assert decided.decision_reason == "fits roadmap" and decided.decided_at and decided.decided_by == "human_operator"
    [entry] = ledger.list_all_entries()
    assert entry.action == "repo_proposal_decision" and entry.task_id == "RP-003"
    assert entry.human_decision == "approved" and entry.agent_id == "human_operator"
    meta = entry.metadata
    assert (meta["previous_state"], meta["new_state"]) == ("awaiting_authority", "accepted")
    assert meta["operator_source"] == "cli" and meta["reason"] == "fits roadmap"
    assert meta["evidence_fingerprints"] == ["fp-a", "fp-b"]
    assert meta["bootstrap_contract_sha256"] == contract_fingerprint(before) == out["bootstrap_contract_sha256"]
    # Only the proposal file and the ledger changed: no repository or other artifact appeared.
    new_files = set(sorted(p.name for p in tmp_path.rglob("*"))) - set(before_files)
    assert new_files == {"ledger.jsonl"}


def test_reject_marks_rejected(tmp_path):
    store = _propose(tmp_path)
    out = pd.decide_proposal(tmp_path, "RP-003", "rejected")
    assert out["state"] == "rejected" and store.load("RP-003").state == "rejected"
    assert store.list_awaiting_authority() == []


def test_fingerprint_is_stable_across_the_state_change(tmp_path):
    store = _propose(tmp_path)
    before = contract_fingerprint(store.load("RP-003"))
    pd.decide_proposal(tmp_path, "RP-003", "approved")
    assert contract_fingerprint(store.load("RP-003")) == before


@pytest.mark.parametrize("first,second", [("approved", "approved"), ("approved", "rejected"),
                                          ("rejected", "approved"), ("rejected", "rejected")])
def test_a_second_decision_is_refused_and_changes_nothing(tmp_path, first, second):
    store = _propose(tmp_path)
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    pd.decide_proposal(tmp_path, "RP-003", first, ledger=ledger)
    state = store.load("RP-003").state
    with pytest.raises(pd.ProposalDecisionError) as err:
        pd.decide_proposal(tmp_path, "RP-003", second, ledger=ledger)
    assert err.value.code == "PROPOSAL_NOT_AWAITING_AUTHORITY"
    assert store.load("RP-003").state == state
    assert len(ledger.list_all_entries()) == 1


def test_unknown_proposal_is_reported(tmp_path):
    with pytest.raises(pd.ProposalDecisionError) as err:
        pd.decide_proposal(tmp_path, "nope", "approved")
    assert err.value.code == "PROPOSAL_NOT_FOUND"


def test_malformed_record_is_refused_without_writing(tmp_path):
    _propose(tmp_path)
    path = tmp_path / "repo_proposals" / "RP-003.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    with pytest.raises(pd.ProposalDecisionError) as err:
        pd.decide_proposal(tmp_path, "RP-003", "approved", ledger=ledger)
    assert err.value.code == "PROPOSAL_MALFORMED"
    assert path.read_text(encoding="utf-8") == "{not json" and ledger.list_all_entries() == []


def test_proposal_in_another_state_directory_is_not_decided(tmp_path):
    _propose(tmp_path / "campaign_a")
    with pytest.raises(pd.ProposalDecisionError) as err:
        pd.decide_proposal(tmp_path / "campaign_b", "RP-003", "approved")
    assert err.value.code == "PROPOSAL_NOT_FOUND"
    assert _store(tmp_path / "campaign_a").load("RP-003").state == "awaiting_authority"


def test_records_written_before_decisions_existed_still_load(tmp_path):
    store = _propose(tmp_path)
    path = tmp_path / "repo_proposals" / "RP-003.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("decided_at", "decided_by", "decision_reason"):
        data.pop(key, None)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load("RP-003").decided_at is None


@pytest.mark.parametrize("argv", [["approve"], ["approve", "T-1", "--proposal", "RP-003"],
                                  ["reject", "--work-item", "WI-1", "--proposal", "RP-003"]])
def test_cli_requires_exactly_one_target(argv):
    with pytest.raises(OperatorFailure) as err:
        cli._handle_decision(cli.build_parser().parse_args(argv), "approved")
    assert err.value.error.code == "DECISION_TARGET_REQUIRED"


def test_cli_proposal_needs_a_state_directory():
    with pytest.raises(OperatorFailure) as err:
        cli._handle_decision(cli.build_parser().parse_args(["approve", "--proposal", "RP-003"]), "approved")
    assert err.value.error.code == "MISSING_STATE_DIRECTORY"


def test_cli_proposal_end_to_end_json_and_text(tmp_path, capsys):
    _propose(tmp_path)
    ledger = str(tmp_path / "l.jsonl")
    args = cli.build_parser().parse_args(
        ["approve", "--proposal", "RP-003", "--state-dir", str(tmp_path), "--ledger-file", ledger, "--json"])
    assert cli.cmd_approve(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "accepted"
    again = cli.build_parser().parse_args(["reject", "--proposal", "RP-003", "--state-dir", str(tmp_path)])
    with pytest.raises(OperatorFailure) as err:
        cli.cmd_reject(again)
    assert err.value.error.code == "PROPOSAL_NOT_AWAITING_AUTHORITY"
    assert EvidenceLedger(ledger).list_all_entries()[0].action == "repo_proposal_decision"


def test_status_prints_valid_proposal_commands(tmp_path, capsys):
    state_dir = tmp_path / "state"
    store = _propose(state_dir)
    from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
    SupervisorStateStore(state_dir / "supervisor").save(SupervisorStateStore(state_dir / "supervisor").load())
    assert cli.main(["factory", "status", "--state-dir", str(state_dir)]) == 0
    text = capsys.readouterr().out
    assert "OWNER REQUIRED" in text and "RP-003" in text
    command = next(line.strip() for line in text.splitlines() if line.strip().startswith("howlplane approve"))
    cli.build_parser().parse_args(shlex.split(command)[1:])
    assert cli.main(["factory", "status", "--state-dir", str(state_dir), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["proposals_awaiting_authority"][0]["proposal_id"] == "RP-003"
    assert doc["decisions"][0]["kind"] == "proposal"
    assert doc["operator"]["next_action"]["command"] == doc["decisions"][0]["approve"]
    assert store.load("RP-003").state == "awaiting_authority"  # status is read-only


def test_operator_model_renders_proposal_decision():
    status = {"state": "waiting_for_authority", "proposals_awaiting_authority": [
        {"proposal_id": "RP-003", "repository_name": "normalizer", "disposition": "propose_new_repository"}],
        "decisions": [{"kind": "proposal", "proposal_id": "RP-003", "repository_name": "normalizer",
                       "approve": "howlplane approve --proposal RP-003 --state-dir /s",
                       "reject": "howlplane reject --proposal RP-003 --state-dir /s"}]}
    op = derive_operator_status(status)
    text = "\n".join(render_operator_text(op, status))
    assert "repository proposal RP-003" in text and "howlplane reject --proposal RP-003" in text
