"""`howlplane write`: a prompt -> HowlWriter run, against a fake `howlwriter` on PATH."""

import json
import os
import sys
from pathlib import Path

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.write_pipeline import StageFailed, WriteRun

pytestmark = pytest.mark.contract

# Stands in for the `howlwriter` console script. It records every call (argv, whether stdin
# was a terminal or held data, and the model choices it could see), writes --out like the
# real commands, and exits with FAKE_WRITE_EXIT for the writing command.
FAKE_HOWLWRITER = r'''
import json, os, sys
argv = sys.argv[1:]
stdin_data = "" if sys.stdin is None else sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "stdin_tty": os.isatty(0), "stdin_data": stdin_data,
                          "env_provider": os.environ.get("HOWLWRITER_PROVIDER"),
                          "env_review": os.environ.get("HOWLWRITER_REVIEW_PROVIDER"),
                          "forbid_local": os.environ.get("HOWL_FORBID_LOCAL_INFERENCE")}) + "\n")
if argv == ["--version"]:
    print("howlwriter 0.1.0")
    sys.exit(0)
if argv[0] == "lint":
    print(json.dumps([{"rule_code": "HW001", "message": "fake finding"}]))
    sys.exit(0)
head = argv[:argv.index("--")] if "--" in argv else argv
if "--owner-confirmed" in head or "--allow-remote" in head:
    sys.exit(97)  # an owner-only flag reached HowlWriter as an option
code = int(os.environ.get("FAKE_WRITE_EXIT", "0"))
if code in (2, 4):
    sys.stderr.write("error: owner decision needed; run `howlwriter project authorize-remote demo`\n")
    sys.exit(code)
out = head[head.index("--out") + 1]
open(out, "w").write("Draft text.\n")
if "project" in head:
    verdict = "READY_FOR_HUMAN_REVIEW" if code == 0 else "BLOCKED"
    print(json.dumps({"path": "/private/project/drafts/1.md", "text": "Draft text.\n",
                      "provenance": {"validation": {"verdict": verdict,
                                                    "issue_counts": {"unsupported_claim": 2}}}}))
sys.exit(code)
'''


@pytest.fixture
def fake_writer(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "howlwriter"
    script.write_text(f"#!{sys.executable}\n{FAKE_HOWLWRITER}")
    script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    # Only the fake and system tools: a test must never reach a real installed HowlWriter.
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_WRITE_EXIT", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("HOWL_FORBID_LOCAL_INFERENCE", raising=False)

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]
    return calls


def run_cli(tmp_path, *extra):
    return cli.main(["write", "run", "--run-dir", str(tmp_path / "run"), *extra])


def state(tmp_path):
    return json.loads((tmp_path / "run" / "write-run.json").read_text())


def writing_call(calls):
    return next(c for c in calls() if c["argv"][0] == "--provider")


def test_compose_runs_every_stage_and_credits_howlwriter(tmp_path, fake_writer):
    assert run_cli(tmp_path, "--prompt", "A short note on backups", "--provider", "local",
                   "--local-model", "qwen2.5:7b-instruct", "--target-words", "200") == 0
    record = state(tmp_path)
    assert record["status"] == "COMPLETED"
    assert [record["stages"][s]["status"] for s in ("preflight", "write", "check", "audit")] == ["COMPLETED"] * 4
    out = str(tmp_path / "run" / "write" / "output.md")
    assert writing_call(fake_writer)["argv"] == [
        "--provider", "local", "--local-model", "qwen2.5:7b-instruct", "writer", "--out", out,
        "--target-words", "200", "--", "A short note on backups"]
    assert record["stages"]["check"]["lint_findings"] == 1
    # Unlike `creative`, a write run must not forbid the local model it was asked to use.
    assert all(c["forbid_local"] is None for c in fake_writer())
    audit = json.loads((tmp_path / "run" / "write-audit.json").read_text())
    assert [(c["component"], c["operation"]) for c in audit["contributions"]] == [
        ("howlwriter", "GENERATED"), ("howlwriter", "CHECKED"), ("howlplane", "ORCHESTRATED")]
    assert audit["contributions"][0]["provider"] == "local"
    assert audit["owner_decisions_relayed"] == []


def test_project_draft_that_is_not_ready_is_saved_and_exits_3(tmp_path, fake_writer, monkeypatch):
    monkeypatch.setenv("FAKE_WRITE_EXIT", "3")
    assert run_cli(tmp_path, "--prompt", "Discuss the results", "--project", "demo",
                   "--provider", "codex", "--voice", "academic") == 3
    record = state(tmp_path)
    assert record["status"] == "NOT_READY"
    write = record["stages"]["write"]
    assert write["status"] == "COMPLETED" and write["ready"] is False
    assert write["verdict"] == "BLOCKED" and write["issue_counts"] == {"unsupported_claim": 2}
    argv = writing_call(fake_writer)["argv"]
    assert argv[:4] == ["--provider", "codex", "project", "draft"]
    assert "--instructions=Discuss the results" in argv and "--fail-if-blocked" in argv
    assert argv[-2:] == ["--", "demo"]
    assert (tmp_path / "run" / "write" / "output.md").read_text() == "Draft text.\n"
    # Resuming a finished run changes nothing and keeps reporting it as not ready.
    before = len(fake_writer())
    assert cli.main(["write", "resume", "--run-dir", str(tmp_path / "run")]) == 3
    assert len(fake_writer()) == before


@pytest.mark.parametrize("code,status", [(4, "AWAITING_OWNER"), (2, "NEEDS_INPUT")])
def test_an_owner_decision_parks_the_run_until_resume(tmp_path, fake_writer, monkeypatch, capsys,
                                                      code, status):
    monkeypatch.setenv("FAKE_WRITE_EXIT", str(code))
    assert run_cli(tmp_path, "--prompt", "Discuss the results", "--project", "demo",
                   "--provider", "claude") == code
    record = state(tmp_path)
    assert record["status"] == status and record["failed_stage"] == "write"
    assert record["stages"]["write"]["status"] == "BLOCKED"
    assert "authorize-remote" in record["stages"]["write"]["stderr_tail"]
    printed = capsys.readouterr().out
    assert "howlplane write resume --run-dir" in printed and "authorize-remote" in printed

    # The owner acts at a terminal; resume retries only the blocked stage onward.
    monkeypatch.setenv("FAKE_WRITE_EXIT", "0")
    preflights = sum(1 for c in fake_writer() if c["argv"] == ["--version"])
    assert cli.main(["write", "resume", "--run-dir", str(tmp_path / "run")]) == 0
    assert state(tmp_path)["status"] == "COMPLETED"
    assert sum(1 for c in fake_writer() if c["argv"] == ["--version"]) == preflights


@pytest.mark.parametrize("operation", ["compose", "project-draft"])
def test_a_prompt_can_never_become_an_option(tmp_path, fake_writer, operation):
    extra = ["--project", "demo"] if operation == "project-draft" else []
    # Plane's own parser needs the `=` form for such a prompt; HowlWriter must still see text.
    assert run_cli(tmp_path, "--prompt=--owner-confirmed", *extra, "--provider", "local") == 0
    argv = writing_call(fake_writer)["argv"]
    head = argv[:argv.index("--")]
    assert "--owner-confirmed" not in head and "--allow-remote" not in head
    assert "--owner-confirmed" in argv[argv.index("--"):] or "--instructions=--owner-confirmed" in head


def test_owner_only_flags_are_refused_before_howlwriter_runs(tmp_path, fake_writer):
    run = WriteRun(tmp_path / "run", {"operation": "compose", "prompt": "x", "provider": "local",
                                      "timeout_seconds": 60})
    for flag in ("--owner-confirmed", "--allow-remote"):
        with pytest.raises(StageFailed, match="owner-only"):
            run._howlwriter("write", ["project", "draft", flag, "--", "demo"])
    assert fake_writer() == []


def test_howlwriter_cannot_prompt_or_inherit_an_unrecorded_model(tmp_path, fake_writer, monkeypatch):
    monkeypatch.setenv("HOWLWRITER_PROVIDER", "claude")
    monkeypatch.setenv("HOWLWRITER_REVIEW_PROVIDER", "agy")
    assert run_cli(tmp_path, "--prompt", "Notes", "--provider", "local") == 0
    for call in fake_writer():
        assert call["stdin_tty"] is False and call["stdin_data"] == ""
        assert call["env_provider"] is None and call["env_review"] is None


def test_humanize_rewrites_the_input_file(tmp_path, fake_writer):
    source = tmp_path / "draft.md"
    source.write_text("Original.\n")
    assert run_cli(tmp_path, "--input", str(source), "--provider", "agy", "--voice", "plain") == 0
    argv = writing_call(fake_writer)["argv"]
    assert argv[:3] == ["--provider", "agy", "humanize"]
    assert "--voice=plain" in argv and argv[-2:] == ["--", str(source.resolve())]
    assert state(tmp_path)["inputs"]["operation"] == "humanize"


def test_claude_inside_a_claude_code_session_needs_allow_nested(tmp_path, fake_writer, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run_cli(tmp_path, "--prompt", "Notes", "--provider", "claude") == 2
    assert state(tmp_path)["status"] == "NEEDS_INPUT"
    assert fake_writer() == []

    assert cli.main(["write", "run", "--run-dir", str(tmp_path / "other"), "--prompt", "Notes",
                     "--provider", "local", "--review-provider", "claude"]) == 2
    assert cli.main(["write", "run", "--run-dir", str(tmp_path / "local"), "--prompt", "Notes",
                     "--provider", "local"]) == 0
    assert cli.main(["write", "run", "--run-dir", str(tmp_path / "nested"), "--prompt", "Notes",
                     "--provider", "claude", "--allow-nested"]) == 0


@pytest.mark.parametrize("args,message", [
    (["--prompt", "x"], "--provider is required"),
    (["--provider", "local"], "compose needs --prompt"),
    (["--project", "demo", "--provider", "local"], "project-draft needs --prompt"),
    (["--operation", "humanize", "--prompt", "x", "--provider", "local"], "takes no instructions"),
    (["--operation", "compose", "--prompt", "x", "--project", "demo", "--provider", "local"],
     "--project applies only"),
    (["--prompt", "x", "--provider", "claude", "--local-model", "m"], "--local-model applies only"),
    (["--prompt", "x", "--provider", "local", "--mode", "academic"], "--mode applies only"),
    (["--input", "/nonexistent/file.md", "--provider", "local"], "is not a file"),
])
def test_inconsistent_inputs_are_rejected_before_anything_runs(tmp_path, fake_writer, capsys, args, message):
    assert run_cli(tmp_path, *args) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "run").exists()
    assert fake_writer() == []


def test_missing_howlwriter_fails_with_an_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert run_cli(tmp_path, "--prompt", "x", "--provider", "local") == 1
    record = state(tmp_path)
    assert record["status"] == "FAILED" and record["failed_stage"] == "preflight"
    assert "howl install howlwriter" in record["stages"]["preflight"]["error"]


def test_a_run_directory_holds_one_request(tmp_path, fake_writer, capsys):
    assert run_cli(tmp_path, "--prompt", "First", "--provider", "local") == 0
    assert run_cli(tmp_path, "--prompt", "Second", "--provider", "local") == 2
    assert "different inputs" in capsys.readouterr().err
    assert cli.main(["write", "status", "--run-dir", str(tmp_path / "run"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["inputs"]["prompt"] == "First"
    assert cli.main(["write", "status", "--run-dir", str(tmp_path / "missing")]) == 2


def test_writes_stay_in_the_run_directory(tmp_path, fake_writer):
    assert run_cli(tmp_path, "--prompt", "Notes", "--provider", "local") == 0
    created = {p.relative_to(tmp_path).parts[0] for p in Path(tmp_path).rglob("*")}
    assert created <= {"bin", "calls.jsonl", "run"}
