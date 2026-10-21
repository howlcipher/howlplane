"""HowlPlane's side of the Howl interop contract (docs/INTEROP.md in howlcipher/howl).

HowlChangeOps is driven through its howl.result/v1 envelopes, never its prose;
HowlDream's PARTIAL exit is a result, not a crash; the descriptor matches the
parser; and the data files other components' edges need ship in the wheel.
"""

import builtins
import json
from pathlib import Path

import pytest

from howlplane.control_plane import cli, evidence_ledger, resources
from howlplane.control_plane.executor import HowlChangeOpsExecutor
from howlplane.control_plane.howldream_runner import ExplorationBudget, NativeHowlDreamProvider
from howlplane.control_plane.interop_descriptor import CONSUMES
from howlplane.control_plane.proposed_action import ProposedAction

DESCRIPTOR = {
    "schema": "howl.interop/v1", "component": "howlchangeops", "version": "0.2.0", "exit_codes": {"0": "ok"},
    "commands": [{"path": [c], "flags": ["--json"], "json": True} for c in ("evaluate", "execute")]
    + [{"path": ["approve"], "flags": ["--json", "--owner-confirmed", "--approver"], "json": True}],
}


def _envelope(command, status, data=None, error=None):
    return {"schema": "howl.result/v1", "component": "howlchangeops", "command": [command],
            "status": status, "data": data, "error": error}


def fake_changeops(tmp_path: Path, responses: dict, descriptor=DESCRIPTOR) -> Path:
    """A howlchangeops that answers `interop` and prints a canned envelope per command."""
    script = tmp_path / "bin" / "howlchangeops"
    script.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh", 'echo "$@" >> "$0.calls"', 'case "$1" in']
    if descriptor is not None:
        lines.append(f"  interop) cat <<'JSON'\n{json.dumps(descriptor)}\nJSON\n    exit 0;;")
    for command, (code, payload) in responses.items():
        body = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"  {command}) cat <<'JSON'\n{body}\nJSON\n    exit {code};;")
    lines += ["  *) echo \"Unknown command: $1\"; exit 1;;", "esac"]
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def calls(script: Path) -> list:
    return Path(f"{script}.calls").read_text().splitlines()


def _action():
    return ProposedAction(action_type="create_release_candidate", target_repo="repo")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    (path / "run").mkdir(parents=True)
    return path


def test_evaluate_reads_the_verdict_and_decision_from_the_envelope(tmp_path, repo):
    script = fake_changeops(tmp_path, {"evaluate": (0, _envelope("evaluate", "awaiting_human", {
        "decision_id": "decision-abc", "decision_saved": True, "verdict": "REQUIRE_APPROVAL",
        "reason": "approval required"}))})
    verdict, decision_id, reason = HowlChangeOpsExecutor(script).evaluate(_action(), repo, repo / "run")
    assert (verdict, decision_id, reason) == ("REQUIRE_APPROVAL", "decision-abc", "approval required")
    assert calls(script)[-1].startswith("evaluate ") and calls(script)[-1].endswith(" --json")


def test_evaluate_deny_is_a_verdict_not_an_error(tmp_path, repo):
    script = fake_changeops(tmp_path, {"evaluate": (0, _envelope("evaluate", "denied", {
        "decision_id": "decision-x", "decision_saved": False, "verdict": "DENY", "reason": "dirty tree"}))})
    assert HowlChangeOpsExecutor(script).evaluate(_action(), repo, repo / "run") == ("DENY", None, "dirty tree")


def test_an_unsaved_require_approval_is_an_error_not_a_guess(tmp_path, repo):
    # The prose parser used to fall back to the newest file in decisions/.
    (repo / ".howlchangeops" / "decisions").mkdir(parents=True)
    (repo / ".howlchangeops" / "decisions" / "decision-stale.json").write_text("{}")
    script = fake_changeops(tmp_path, {"evaluate": (0, _envelope("evaluate", "awaiting_human", {
        "decision_id": "decision-new", "decision_saved": False, "verdict": "REQUIRE_APPROVAL"}))})
    verdict, decision_id, _ = HowlChangeOpsExecutor(script).evaluate(_action(), repo, repo / "run")
    assert (verdict, decision_id) == ("ERROR", None)


def test_unavailable_changeops_reports_its_code(tmp_path, repo):
    script = fake_changeops(tmp_path, {"evaluate": (1, _envelope("evaluate", "unavailable", None, {
        "code": "HOWLFRAME_INCOMPATIBLE", "message": "found 0.2.0"}))})
    verdict, _, reason = HowlChangeOpsExecutor(script).evaluate(_action(), repo, repo / "run")
    assert verdict == "ERROR" and reason.startswith("HOWLFRAME_INCOMPATIBLE")


def test_a_changeops_without_json_is_told_to_update_and_never_parsed(tmp_path, repo):
    script = fake_changeops(tmp_path, {"evaluate": (0, "Decision: ALLOW\nReason: ok")}, descriptor=None)
    executor = HowlChangeOpsExecutor(script)
    verdict, _, reason = executor.evaluate(_action(), repo, repo / "run")
    assert verdict == "ERROR" and "howl update howlchangeops" in reason
    assert not any(c.startswith("evaluate") for c in calls(script))
    assert executor.version() == "unknown"


def test_approve_relays_only_an_owner_decision(tmp_path, repo):
    script = fake_changeops(tmp_path, {"approve": (0, _envelope("approve", "ok", {"approval_id": "app-1"}))})
    executor = HowlChangeOpsExecutor(script)
    ok, message = executor.approve("decision-abc", repo, repo / "run")
    assert not ok and message.startswith("OWNER_CONFIRMATION_REQUIRED")
    assert "howlchangeops approve decision-abc" in message
    assert not any(c.startswith("approve") for c in calls(script)) if Path(f"{script}.calls").exists() else True

    ok, message = executor.approve("decision-abc", repo, repo / "run", owner_confirmed=True, approver="alice")
    assert ok and "app-1" in message
    assert calls(script)[-1] == "approve decision-abc --owner-confirmed --approver alice --json"


def test_approve_key_errors_are_reported(tmp_path, repo):
    script = fake_changeops(tmp_path, {"approve": (1, _envelope("approve", "blocked", None, {
        "code": "APPROVAL_KEY_MISSING", "message": "DENIED: HOWLCHANGEOPS_APPROVAL_KEY_FILE not set"}))})
    ok, message = HowlChangeOpsExecutor(script).approve("decision-abc", repo, repo / "run", owner_confirmed=True)
    assert not ok and message.startswith("APPROVAL_KEY_MISSING")


def test_execute_maps_stale_evidence_and_success(tmp_path, repo):
    stale = fake_changeops(tmp_path, {"execute": (1, _envelope("execute", "denied", {"decision_id": "d"}, {
        "code": "STALE_REMOTE_EVIDENCE", "message": "remote moved"}))})
    assert HowlChangeOpsExecutor(stale).execute("d", repo, repo / "run", _action(), "task-1").status == "stale"

    receipts = repo / ".howlchangeops" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "d.json").write_text(json.dumps({"schema": "howlchangeops.execution_receipt/v1", "verification": "PASS"}))
    ok = fake_changeops(tmp_path / "ok", {"execute": (0, _envelope("execute", "ok", {"verification": "PASS"}))})
    result = HowlChangeOpsExecutor(ok).execute("d", repo, repo / "run", _action(), "task-1")
    assert result.status == "success"
    assert result.receipt.executor_version == "0.2.0"  # read from the installed descriptor


def test_howldream_cli_partial_exploration_is_a_result(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    script = tmp_path / "howldream"
    script.write_text("#!/bin/sh\n"
                      "echo '{\"exploration_id\": \"hd-20261020-abc123\", \"status\": \"PARTIAL\"}'\n"
                      "exit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("HOWLDREAM_BIN", str(script))
    real_import = builtins.__import__

    def no_howldream(name, *args, **kwargs):
        if name.startswith("howldream"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_howldream)
    run_dir, data = NativeHowlDreamProvider().explore("objective", ExplorationBudget(), work)
    # Same layout as the in-process engine: work_dir/<exploration_id>.
    assert run_dir == work / "hd-20261020-abc123"
    assert data["status"] == "PARTIAL"

    script.write_text("#!/bin/sh\necho '{\"error\": \"bad\"}' >&2\nexit 2\n")
    with pytest.raises(RuntimeError, match="exited 2"):
        NativeHowlDreamProvider().explore("objective", ExplorationBudget(), work)


def test_descriptor_matches_the_parser(capsys):
    assert cli.main(["interop", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert (d["schema"], d["component"]) == ("howl.interop/v1", "howlplane")
    described = {tuple(c["path"]): set(c["flags"]) for c in d["commands"]}
    for path in (("orchestrate",), ("agents", "doctor"), ("factory", "prepare"), ("reconcile",), ("project", "validate")):
        assert path in described, path
    assert {"--findings-file", "--output"} <= described[("reconcile",)]
    assert d["consumes"] == CONSUMES
    for use in CONSUMES:
        assert set(use) <= {"component", "command", "flags", "schemas", "min_version"}
        assert all(flag.startswith("-") for flag in use["flags"])


def test_packaged_data_matches_the_repository_files():
    for parts in resources.PACKAGED_FILES:
        canonical = resources.CHECKOUT_ROOT.joinpath(*parts)
        packaged = resources.PACKAGED_ROOT.joinpath(*parts)
        assert packaged.read_bytes() == canonical.read_bytes(), "/".join(parts)


def test_wheel_installs_find_packaged_data_and_keep_the_ledger_out_of_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "CHECKOUT_ROOT", tmp_path / "not-a-checkout")
    assert resources.data_file("integrations", "howlframe", "candidate_evaluator.hfbc").is_file()
    monkeypatch.setattr(evidence_ledger, "is_checkout", lambda: False)
    monkeypatch.setenv("HOWLPLANE_HOME", str(tmp_path / "home"))
    assert evidence_ledger.default_ledger_path() == tmp_path / "home" / "logs" / "control_plane" / "evidence_ledger.jsonl"
    monkeypatch.delenv("HOWLPLANE_HOME")
    monkeypatch.delenv("HOWLPLANE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert evidence_ledger.default_ledger_path().is_relative_to(tmp_path / "state" / "howlplane")
