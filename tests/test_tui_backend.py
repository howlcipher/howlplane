"""`HowlPlaneBackend`: the TUI's only path into HowlPlane.

Inventory and preview run the real provider pool against a private config; a
fake agent executable records any launch, so the tests prove readiness is
probed (TUI-00 HP-F2) without anything being run. Run, result and cancel are
exercised with stand-in child processes; no governed run or provider starts.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from howlplane.control_plane import config_loader
from howlplane.control_plane.tui import backend as backend_mod
from howlplane.control_plane.tui.backend import HowlPlaneBackend, RunHandle
from tests._tui_support import fake_agent_sandbox

pytestmark = pytest.mark.contract


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    box = fake_agent_sandbox(tmp_path)
    monkeypatch.setenv("HOWLPLANE_LOCAL_CONFIG", str(box["config"]))
    monkeypatch.setenv("HOWLPLANE_CAPACITY_PATH", str(tmp_path / "capacity.json"))
    # The pool reads the process-wide loader; pin it to this config so the
    # result cannot depend on which earlier test imported it first.
    monkeypatch.setattr(config_loader, "default_loader",
                        config_loader.ConfigLoader(local_config_path=str(box["config"])), raising=False)
    return box


def codex_readiness(document):
    return {r["identity"]["resource_id"]: r["readiness"] for r in document["resources"]}["codex"]


def test_inventory_and_preview_follow_readiness(sandbox, monkeypatch):
    backend = HowlPlaneBackend(sandbox["repo"], sandbox["tmp"] / "private")
    monkeypatch.setenv("PATH", f"{sandbox['bindir']}:/usr/bin:/bin")
    assert codex_readiness(backend.inventory()) == "READY"
    assert backend.route("add a health endpoint", None)["selected"]["resource_id"] == "codex"

    sandbox["agent"].unlink()
    assert codex_readiness(backend.inventory()) == "MISSING_EXECUTABLE"
    blocked = backend.route("add a health endpoint", None)
    assert blocked["status"] == "BLOCKED"
    assert ("codex", "MISSING_EXECUTABLE") in [(x["resource_id"], x["reason"]) for x in blocked["exclusions"]]
    assert not sandbox["marker"].exists(), "readiness must never launch the agent"
    assert not (sandbox["tmp"] / "capacity.json").exists(), "preview is read only"


def test_explicit_unconfigured_provider_is_blocked(sandbox, monkeypatch):
    monkeypatch.setenv("PATH", f"{sandbox['bindir']}:/usr/bin:/bin")
    decision = HowlPlaneBackend(sandbox["repo"], sandbox["tmp"] / "p").route("x", "gemini_cli")
    assert decision["status"] == "BLOCKED" and decision["explicit_override"] is True


def test_run_is_the_governed_work_command(sandbox, monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            self.pid, self.stdout = 0, None

    monkeypatch.setattr(backend_mod.subprocess, "Popen", Recorder)
    backend = HowlPlaneBackend(sandbox["repo"], sandbox["tmp"] / "private")
    handle = backend.start_run('add /health; $(touch x) "q"', "codex", "TUI-1")
    argv = seen["argv"]
    assert argv[:5] == [sys.executable, "-m", "howlplane.control_plane", "work", 'add /health; $(touch x) "q"']
    assert {"--execute", "--json"} <= set(argv)
    assert argv[argv.index("--task-id") + 1] == "TUI-1"
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--repo") + 1] == str(sandbox["repo"])
    assert "shell" not in seen["kwargs"] and seen["kwargs"]["start_new_session"] is True
    assert handle.stderr_path.parent == sandbox["tmp"] / "private"
    assert oct(handle.stderr_path.stat().st_mode & 0o777) == "0o600"


def child(code):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)


def test_result_reads_the_work_result_document(sandbox, tmp_path):
    doc = {"schema": "howlplane.work_result/v1", "final_state": "complete", "exit_code": 0}
    process = child(f"print({json.dumps(json.dumps(doc))})")
    process.wait()
    outcome = HowlPlaneBackend(sandbox["repo"], tmp_path).result(RunHandle("T", process, None))
    assert outcome == {"event": "work_result", "document": doc}


def test_result_falls_back_to_the_howlplane_error(sandbox, tmp_path):
    stderr = tmp_path / "err"
    stderr.write_text("[HowlPlane] ROUTING\n" + json.dumps({"schema": "howlplane.error/v1", "code": "LOCKED",
                                                            "message": "locked"}) + "\n")
    process = child("import sys; sys.exit(1)")
    process.wait()
    outcome = HowlPlaneBackend(sandbox["repo"], tmp_path).result(RunHandle("T", process, stderr))
    assert outcome["event"] == "failure" and outcome["document"]["code"] == "LOCKED"


def test_unreadable_result_is_a_failure_never_success(sandbox, tmp_path):
    process = child("print('not json')")
    process.wait()
    outcome = HowlPlaneBackend(sandbox["repo"], tmp_path).result(RunHandle("T", process, tmp_path / "none"))
    assert outcome["event"] == "failure" and outcome["document"]["code"] == "RUN_OUTPUT_UNREADABLE"


def test_cancel_stops_the_process_group_and_records_it(sandbox, tmp_path, monkeypatch):
    recorded = {}

    class Outcome:
        exit_code = 0

    def fake_cancel(**kwargs):
        recorded.update(kwargs)
        return Outcome()

    monkeypatch.setattr("howlplane.control_plane.human_boundary.HumanLifecycleManager.cancel", fake_cancel)
    process = child("import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)")
    handle = RunHandle("TUI-9", process, None)
    document = HowlPlaneBackend(sandbox["repo"], tmp_path).cancel(handle, grace_seconds=0.5)
    assert process.poll() is not None, "an ignored SIGINT escalates until the child is gone"
    assert recorded["task_id"] == "TUI-9" and recorded["target_repo"] == str(sandbox["repo"])
    assert document["code"] == "RUN_CANCELLED" and document["retry_safe"] == "RECONCILE_FIRST"


def test_progress_reads_the_task_progress_snapshot(sandbox, tmp_path):
    run_dir = Path(sandbox["repo"]) / ".task_runs" / "TUI-2"
    run_dir.mkdir(parents=True)
    snapshot = {"schema": "howlplane.task_progress/v1", "task_id": "TUI-2", "phase": "VERIFYING"}
    (run_dir / "progress.json").write_text(json.dumps(snapshot))
    backend = HowlPlaneBackend(sandbox["repo"], tmp_path)
    assert backend.progress(RunHandle("TUI-2", None)) == snapshot
    assert backend.progress(RunHandle("TUI-3", None)) is None
