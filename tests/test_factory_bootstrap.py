"""#82: the single governed, fingerprint-bound consumer of an accepted repository proposal."""

import json
import shlex
from pathlib import Path

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.factory import bootstrap as bs
from howlplane.control_plane.factory import factory_cli
from howlplane.control_plane.factory import proposal_decision as pd
from howlplane.control_plane.factory.repo_proposal import (
    CapabilityRegistry,
    CapabilityStore,
    RepoProposalStore,
)
from howlplane.control_plane.presentation.errors import OperatorFailure

pytestmark = pytest.mark.unit

PLAN = {
    "capability_id": "normalization",
    "consumer_repositories": ["howlplane"],
    "clear_purpose": True,
    "bounded_maintenance": True,
    "deterministic_verification": True,
    "language": "python",
}


def _passing(target, language):
    return {"status": "passed", "command": ["fake"], "exit_code": 0, "output_tail": ""}


def _failing(target, language):
    return {"status": "failed", "command": ["fake"], "exit_code": 1, "output_tail": "boom"}


class Env:
    def __init__(self, tmp_path, plan=None, name="normalizer"):
        self.state = tmp_path / "state"
        self.root = tmp_path / "repos"
        self.ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
        self.store = RepoProposalStore(self.state / "repo_proposals")
        self.store.propose("RP-1", name, "propose_new_repository", "why", ["fp"], dict(plan or PLAN))

    def accept(self):
        return pd.decide_proposal(self.state, "RP-1", "approved", reason="ok", ledger=self.ledger)

    def run(self, verifier=_passing, **kw):
        return bs.bootstrap_proposal(self.state, "RP-1", ledger=self.ledger, target_root=self.root,
                                     verifier=verifier, **kw)

    def actions(self):
        return [e.action for e in self.ledger.get_task_entries("RP-1")]

    def registry(self):
        return CapabilityRegistry(CapabilityStore(self.state / "capabilities"))


def _code(env, **kw):
    with pytest.raises(bs.BootstrapError) as exc:
        env.run(**kw)
    return exc.value.code


def test_approval_itself_creates_nothing_and_records_the_fingerprint(tmp_path):
    env = Env(tmp_path)
    env.accept()
    assert not env.root.exists()
    assert not (env.state / "bootstrap_runs").exists()
    proposal = env.store.load("RP-1")
    assert proposal.approved_contract_sha256 and proposal.approved_decision_entry_id


@pytest.mark.parametrize("decide", [None, "rejected"])
def test_unaccepted_proposals_cannot_bootstrap(tmp_path, decide):
    env = Env(tmp_path)
    if decide:
        pd.decide_proposal(env.state, "RP-1", decide, ledger=env.ledger)
    assert _code(env) == "BOOTSTRAP_PROPOSAL_NOT_ACCEPTED"
    assert not env.root.exists()
    assert "bootstrap_refused" in env.actions()


def test_unknown_proposal_is_refused(tmp_path):
    env = Env(tmp_path)
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap_proposal(env.state, "NOPE", ledger=env.ledger, target_root=env.root)
    assert exc.value.code == "PROPOSAL_NOT_FOUND"


def test_success_scaffolds_verifies_registers_and_records_evidence(tmp_path):
    env = Env(tmp_path)
    env.accept()
    result = env.run()
    target = env.root / "normalizer"
    assert result["state"] == bs.COMPLETED and Path(result["target_path"]) == target
    assert (target / "README.md").is_file() and (target / "BOOTSTRAP.md").is_file()
    assert (target / ".git").is_dir()
    assert env.store.load("RP-1").bootstrap_plan == PLAN  # decision record untouched
    run = bs.BootstrapRunStore(env.state / "bootstrap_runs").load("RP-1")
    assert run.state == bs.COMPLETED and run.verification["status"] == "passed"
    record = env.registry().find("normalization")
    assert record.verification_status == "verified" and "normalizer" in record.provided_by
    started, completed = [e for e in env.ledger.get_task_entries("RP-1") if e.action.startswith("bootstrap_")]
    assert started.action == "bootstrap_started" and completed.action == "bootstrap_completed"
    meta = completed.metadata
    assert meta["contract_sha256"] == env.store.load("RP-1").approved_contract_sha256
    assert meta["execution_id"] == run.execution_id and meta["decision_entry_id"]
    assert meta["repository_name"] == "normalizer" and meta["verification"]["status"] == "passed"
    assert meta["authority"] == "accepted_proposal_local_scaffold_only"
    env.ledger.read_entries(strict=True)  # every entry is schema-valid


def test_real_python_scaffold_passes_its_own_baseline_test(tmp_path):
    env = Env(tmp_path)
    env.accept()
    result = env.run(verifier=bs.default_verifier)
    assert result["state"] == bs.COMPLETED and result["verification"]["exit_code"] == 0


def test_failed_verification_is_recorded_truthfully_and_does_not_register(tmp_path):
    env = Env(tmp_path)
    env.accept()
    result = env.run(verifier=_failing)
    assert result["state"] == bs.FAILED and "verification failed" in result["error"]
    assert env.registry().find("normalization") is None
    assert bs.BootstrapRunStore(env.state / "bootstrap_runs").load("RP-1").state == bs.FAILED
    assert "bootstrap_failed" in env.actions() and "bootstrap_completed" not in env.actions()


def test_verifier_exception_is_a_failure_not_a_success(tmp_path):
    env = Env(tmp_path)
    env.accept()

    def boom(target, language):
        raise RuntimeError("tool missing")

    assert env.run(verifier=boom)["state"] == bs.FAILED
    assert env.registry().find("normalization") is None


def test_failed_run_needs_explicit_retry_and_a_clean_target(tmp_path):
    env = Env(tmp_path)
    env.accept()
    env.run(verifier=_failing)
    assert _code(env) == "BOOTSTRAP_PREVIOUSLY_FAILED"
    assert _code(env, retry=True) == "BOOTSTRAP_TARGET_CONFLICT"  # leftover is never adopted or overwritten
    import shutil
    shutil.rmtree(env.root / "normalizer")
    assert env.run(retry=True)["state"] == bs.COMPLETED


def test_duplicate_invocation_cannot_create_twice(tmp_path):
    env = Env(tmp_path)
    env.accept()
    env.run()
    marker = (env.root / "normalizer" / bs.MARKER_FILE).read_text()
    assert _code(env) == "BOOTSTRAP_ALREADY_COMPLETED"
    assert (env.root / "normalizer" / bs.MARKER_FILE).read_text() == marker
    assert env.actions().count("bootstrap_started") == 1


def test_changed_contract_after_approval_fails_closed(tmp_path):
    env = Env(tmp_path)
    env.accept()
    tampered = env.store.load("RP-1")
    tampered.bootstrap_plan = {**PLAN, "capability_id": "something-else"}
    env.store.save_object(tampered)
    assert _code(env) == "BOOTSTRAP_CONTRACT_CHANGED"
    assert not env.root.exists()
    refusal = [e for e in env.ledger.get_task_entries("RP-1") if e.action == "bootstrap_refused"][-1]
    assert refusal.metadata["approved_sha256"] != refusal.metadata["current_sha256"]


def test_rewriting_the_stored_fingerprint_too_is_still_caught_by_the_ledger(tmp_path):
    env = Env(tmp_path)
    env.accept()
    tampered = env.store.load("RP-1")
    tampered.repository_name = "evil"
    from howlplane.control_plane.factory.repo_proposal import contract_fingerprint
    tampered.approved_contract_sha256 = contract_fingerprint(tampered)
    env.store.save_object(tampered)
    assert _code(env) == "BOOTSTRAP_CONTRACT_CHANGED"
    assert not env.root.exists()


def test_missing_decision_evidence_fails_closed(tmp_path):
    env = Env(tmp_path)
    env.accept()
    fresh = EvidenceLedger(str(tmp_path / "other-ledger.jsonl"))
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap_proposal(env.state, "RP-1", ledger=fresh, target_root=env.root, verifier=_passing)
    assert exc.value.code == "BOOTSTRAP_DECISION_EVIDENCE_MISSING"
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap_proposal(env.state, "RP-1", ledger=None, target_root=env.root, verifier=_passing)
    assert exc.value.code == "BOOTSTRAP_DECISION_EVIDENCE_MISSING"
    assert not env.root.exists()


@pytest.mark.parametrize("plan", [
    {k: v for k, v in PLAN.items() if k != "language"},
    {**PLAN, "language": "cobol"},
    {**PLAN, "deterministic_verification": False},
    {**PLAN, "surprise": 1},
])
def test_malformed_contract_fails_closed(tmp_path, plan):
    env = Env(tmp_path, plan=plan)
    env.accept()
    assert _code(env) == "BOOTSTRAP_CONTRACT_MALFORMED"
    assert not env.root.exists()


@pytest.mark.parametrize("key,value", [
    ("create_remote", True), ("credentials", ["token"]), ("publish", True), ("organization", "acme"),
    ("external_dependencies", ["x"]),
])
def test_authority_beyond_a_local_scaffold_is_refused(tmp_path, key, value):
    env = Env(tmp_path, plan={**PLAN, key: value})
    env.accept()
    assert _code(env) == "BOOTSTRAP_AUTHORITY_REQUIRED"
    assert not env.root.exists() and env.registry().find("normalization") is None


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "x" * 80])
def test_unsafe_repository_names_are_refused(tmp_path, name):
    env = Env(tmp_path)
    env.accept()
    renamed = env.store.load("RP-1")
    renamed.repository_name = name
    env.store.save_object(renamed)
    assert _code(env) == "BOOTSTRAP_CONTRACT_MALFORMED"
    assert not env.root.exists()


def test_existing_target_is_never_overwritten(tmp_path):
    env = Env(tmp_path)
    env.accept()
    (env.root / "normalizer").mkdir(parents=True)
    (env.root / "normalizer" / "keep.txt").write_text("mine")
    assert _code(env) == "BOOTSTRAP_TARGET_CONFLICT"
    assert (env.root / "normalizer" / "keep.txt").read_text() == "mine"


def test_interrupted_run_resumes_only_with_a_matching_marker(tmp_path):
    env = Env(tmp_path)
    env.accept()

    def crash(target, language):
        raise KeyboardInterrupt  # not caught: the run is left in `started`

    with pytest.raises(KeyboardInterrupt):
        env.run(verifier=crash)
    runs = bs.BootstrapRunStore(env.state / "bootstrap_runs")
    interrupted = runs.load("RP-1")
    assert interrupted.state == bs.STARTED
    result = env.run()
    assert result["state"] == bs.COMPLETED and result["execution_id"] == interrupted.execution_id

    env2 = Env(tmp_path / "second")
    env2.accept()
    with pytest.raises(KeyboardInterrupt):
        env2.run(verifier=crash)
    (env2.root / "normalizer" / bs.MARKER_FILE).write_text(json.dumps({"contract_sha256": "other"}))
    assert _code(env2) == "BOOTSTRAP_TARGET_CONFLICT"


def test_status_lists_accepted_proposals_with_the_exact_command(tmp_path):
    env = Env(tmp_path)
    assert factory_cli._bootstrap_ready(env.store, env.state) == []
    env.accept()
    [item] = factory_cli._bootstrap_ready(env.store, env.state)
    argv = shlex.split(item["command"])
    args = cli.build_parser().parse_args(argv[1:])
    assert args.factory_action == "bootstrap" and args.proposal == "RP-1" and Path(args.state_dir) == env.state
    env.run()
    assert factory_cli._bootstrap_ready(env.store, env.state) == []


def test_cli_command_runs_the_consumer_and_reports_errors(tmp_path, capsys):
    env = Env(tmp_path)
    ledger_file = str(env.ledger.ledger_file)
    base = ["factory", "bootstrap", "--proposal", "RP-1", "--state-dir", str(env.state),
            "--target-root", str(env.root), "--ledger-file", ledger_file]
    with pytest.raises(OperatorFailure) as exc:
        cli.cmd_factory(cli.build_parser().parse_args(base))
    assert exc.value.error.code == "BOOTSTRAP_PROPOSAL_NOT_ACCEPTED"
    env.accept()
    capsys.readouterr()
    assert cli.cmd_factory(cli.build_parser().parse_args(base + ["--json"])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "bootstrap_completed" and result["verification"]["status"] == "passed"
    assert (env.root / "normalizer" / ".git").is_dir()


def test_only_the_bootstrap_module_scaffolds_repositories():
    src = Path(bs.__file__).parents[1]
    users = [p.name for p in src.rglob("*.py") if "MARKER_FILE" in p.read_text(encoding="utf-8")]
    assert users == ["bootstrap.py"]


def test_status_text_shows_the_bootstrap_command(tmp_path):
    from howlplane.control_plane.presentation.operator import derive_operator_status, render_operator_text

    env = Env(tmp_path)
    env.accept()
    status = {"state": "idle", "bootstrap_ready": factory_cli._bootstrap_ready(env.store, env.state)}
    text = "\n".join(render_operator_text(derive_operator_status(status), status))
    assert "Accepted, not yet bootstrapped" in text
    assert status["bootstrap_ready"][0]["command"] in text
