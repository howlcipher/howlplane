"""`howlplane creative`: native Dream -> Writer -> Create orchestration (hermetic fakes)."""

import json
import sys
import os
from pathlib import Path

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.creative_pipeline import CreativeRun

pytestmark = pytest.mark.contract

FAKES = Path(__file__).parent / "fixtures" / "creative_fakes"


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    counters = tmp_path / "counters"
    counters.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(FAKES))
    # The pipeline runs each component's console script from PATH (DOG-032); these shims stand in
    # for them, each running its fake module the way the real entry point runs the package.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command, module in (("howldream", "howldream.cli"), ("howlwriter", "howlwriter"),
                            ("howlcreate", "howlcreate.cli")):
        shim = bin_dir / command
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m {module} "$@"\n')
        shim.chmod(0o755)
    # Only the shims and system tools: a test must never reach a real installed component.
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.setenv("FAKE_COUNTER_DIR", str(counters))
    monkeypatch.delenv("FAKE_FAIL_STAGE", raising=False)
    monkeypatch.delenv("FAKE_REVIEW_ITEM", raising=False)
    return counters


def inputs(tmp_path, **overrides):
    ideas = tmp_path / "ideas.json"
    ideas.write_text("{}")
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    config = tmp_path / "remote.json"
    config.write_text("{}")
    value = {
        "dream_source": {"kind": "external", "path": str(ideas)},
        "candidate_id": None,
        "copy_spec": str(spec),
        "command_config": str(config),
        "create_mode": "develop",
        "sandbox": str(tmp_path / "sandbox"),
        "skip_doctor": True,
    }
    value.update(overrides)
    return value


def test_happy_path_preserves_lineage_and_credit(tmp_path, fake_env):
    run = CreativeRun(tmp_path / "run", inputs(tmp_path))
    state = run.execute()
    assert state["status"] == "COMPLETED", state
    assert [s for s in state["stages"] if state["stages"][s]["status"] == "COMPLETED"] == [
        "preflight", "dream", "writer_request", "writer_write", "create_develop", "materialize", "audit"]
    audit = json.loads((tmp_path / "run" / "contribution-audit.json").read_text())
    assert audit["lineage_intact"] is True
    assert audit["lineage"]["dream_candidate_id"] == "hdx-fake/ext-1/idea/1"
    assert audit["lineage"]["writer_proposal_id"] == "wp-000000000001"
    assert audit["lineage"]["create_development_id"] == "dev-fake"
    assert audit["lineage"]["artifacts"] == [{"artifact_id": "art-1", "path": "index.html"}]
    operations = [(c["component"], c["operation"]) for c in audit["contributions"]]
    assert operations == [
        ("howldream", "CLUSTERED"), ("howldream", "SELECTED"),
        ("howlwriter", "TRANSFORMED_COPY"), ("howlcreate", "DESIGNED"),
        ("howlcreate", "MATERIALIZED"), ("howlplane", "ORCHESTRATED")]
    assert "GENERATED" not in [op for _, op in operations]


def test_stage_failure_is_reported_and_partial_outputs_kept(tmp_path, fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STAGE", "writer_write")
    state = CreativeRun(tmp_path / "run", inputs(tmp_path)).execute()
    assert state["status"] == "FAILED"
    assert state["failed_stage"] == "writer_write"
    failed = state["stages"]["writer_write"]
    assert failed["exit_code"] == 3
    assert failed["writer_failure"] == {"category": "RATE_LIMIT"}
    assert (tmp_path / "run" / "dream" / "candidate.json").exists()
    assert (tmp_path / "run" / "writer" / "request.json").exists()
    assert "create_develop" not in state["stages"]


def test_resume_continues_from_failed_stage_without_rerunning(tmp_path, fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STAGE", "materialize")
    first = CreativeRun(tmp_path / "run", inputs(tmp_path)).execute()
    assert first["failed_stage"] == "materialize"
    monkeypatch.delenv("FAKE_FAIL_STAGE")
    resumed = CreativeRun(tmp_path / "run").execute()
    assert resumed["status"] == "COMPLETED"
    assert (fake_env / "create-develop").read_text() == "1"  # completed stage not re-run
    assert (fake_env / "create-materialize").read_text() == "2"


def test_tampered_stage_output_is_rerun_on_resume(tmp_path, fake_env):
    run = CreativeRun(tmp_path / "run", inputs(tmp_path))
    run.execute()
    (tmp_path / "run" / "create" / "development.json").write_text("{}")
    state = CreativeRun(tmp_path / "run").execute()
    assert state["status"] == "COMPLETED"
    assert (fake_env / "create-develop").read_text() == "2"


def test_children_inherit_local_inference_prohibition(tmp_path, fake_env, monkeypatch):
    monkeypatch.delenv("HOWL_FORBID_LOCAL_INFERENCE", raising=False)
    state = CreativeRun(tmp_path / "run", inputs(tmp_path)).execute()
    assert state["status"] == "COMPLETED"  # the fake Writer asserts the flag is "1"


def test_preflight_failure_blocks_before_any_component(tmp_path, fake_env, monkeypatch):
    from howlplane.control_plane import creative_doctor as cd
    failing = [cd.Check("package.howlcreate", "howlcreate", cd.FAIL, "lacks materialize", "reinstall")]
    monkeypatch.setattr(cd, "run_checks", lambda **kw: failing)
    state = CreativeRun(tmp_path / "run", inputs(tmp_path, skip_doctor=False)).execute()
    assert state["failed_stage"] == "preflight"
    assert "howlcreate: lacks materialize" in state["stages"]["preflight"]["failures"]
    assert "dream" not in state["stages"]


def test_mismatched_inputs_refused_for_existing_run(tmp_path, fake_env):
    CreativeRun(tmp_path / "run", inputs(tmp_path))
    with pytest.raises(ValueError, match="different inputs"):
        CreativeRun(tmp_path / "run", inputs(tmp_path, create_mode="scaffold"))


def test_explore_source_and_scaffold_without_sandbox(tmp_path, fake_env):
    state = CreativeRun(tmp_path / "run", inputs(
        tmp_path, dream_source={"kind": "explore", "objective": "neutral"}, create_mode="scaffold",
        sandbox=None)).execute()
    assert state["status"] == "COMPLETED"
    assert state["stages"]["materialize"]["skipped"] is True
    assert state["stages"]["dream"]["selected"] == "hd-fake/c/idea/1"
    assert (fake_env / "create-scaffold").read_text() == "1"


def run_cli(tmp_path):
    for name in ("ideas.json", "spec.json", "remote.json"):
        (tmp_path / name).write_text("{}")
    return cli.main(["creative", "run", "--run-dir", str(tmp_path / "run"),
                     "--external-ideas", str(tmp_path / "ideas.json"),
                     "--copy-spec", str(tmp_path / "spec.json"),
                     "--command-config", str(tmp_path / "remote.json"),
                     "--sandbox", str(tmp_path / "sandbox"), "--skip-doctor"])


def test_cli_run_and_status(tmp_path, fake_env, capsys):
    code = run_cli(tmp_path)
    assert code == 0, capsys.readouterr()
    assert "COMPLETED" in capsys.readouterr().out
    assert cli.main(["creative", "status", "--run-dir", str(tmp_path / "run"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "COMPLETED"


def test_cli_requires_exactly_one_dream_source(tmp_path, capsys):
    (tmp_path / "spec.json").write_text("{}")
    code = cli.main(["creative", "run", "--run-dir", str(tmp_path / "run"),
                     "--copy-spec", str(tmp_path / "spec.json"), "--command-config", "x"])
    assert code == 2
    assert "exactly one Dream source" in capsys.readouterr().err


def test_completed_run_names_copy_that_needs_review(tmp_path, fake_env, monkeypatch, capsys):
    """DOG-033: run-042 printed only COMPLETED while Writer's subhead failed its fidelity check and the
    page kept the original placeholder text for it."""
    monkeypatch.setenv("FAKE_REVIEW_ITEM", "subhead")

    assert run_cli(tmp_path) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out
    assert "Review required: 1 copy item(s) could not be verified against the evidence." in out
    assert "subhead: FACTUAL_REVIEW_REQUIRED" in out
    assert '"8" counts check' in out
    assert "The materialized page keeps the original copy for: subhead." in out
    state = json.loads((tmp_path / "run" / "creative-run.json").read_text())
    assert state["stages"]["writer_write"]["review_required"][0]["item_id"] == "subhead"
    assert state["stages"]["materialize"]["withheld_items"] == ["subhead"]


def test_fully_verified_run_prints_no_review_section(tmp_path, fake_env, capsys):
    assert run_cli(tmp_path) == 0
    assert "Review required" not in capsys.readouterr().out


def test_components_run_as_their_own_clis(tmp_path, fake_env):
    """DOG-032: stages run each component's console script, not `python -m` in HowlPlane's interpreter."""
    (tmp_path / "bin" / "howlcreate").unlink()
    state = CreativeRun(tmp_path / "run", inputs(tmp_path)).execute()

    assert state["status"] == "FAILED" and state["failed_stage"] == "create_develop"
    assert "howlcreate is not on PATH" in state["stages"]["create_develop"]["error"]
    writer = state["stages"]["writer_write"]
    assert writer["commands"][0][0] == "howlwriter"
    assert writer["executables"]["howlwriter"] == str(tmp_path / "bin" / "howlwriter")
