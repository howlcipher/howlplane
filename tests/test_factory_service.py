"""Service contracts: safe rendering and cooperative supervisor shutdown."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from howlplane.control_plane.factory.supervisor_state import SupervisorStateStore
from tests._factory_test_helpers import make_supervisor


def test_stop_request_survives_tick_persistence(tmp_path):
    supervisor, _, sleeps = make_supervisor(tmp_path)

    def discovery():
        supervisor.request_stop("signal_sigterm")
        return [{"origin": "existing_backlog", "repository": "example/product",
                 "title": "Useful pending work", "identity_keys": ["bugs.md", "1"]}]

    supervisor.discovery = discovery
    supervisor.run()
    persisted = supervisor.state_store.load()
    assert persisted.state == "stopped"
    assert persisted.stopped_reason == "signal_sigterm"
    assert persisted.last_successful_tick_at
    assert persisted.observations_consumed == 1
    assert persisted.dispatch_history == []
    assert not sleeps


def test_unit_render_preserves_literal_paths_and_refuses_overwrite(tmp_path):
    from scripts.install_factory_service import install

    checkout = tmp_path / 'checkout $name %h "quoted"'
    (checkout / "src/control_plane").mkdir(parents=True)
    (checkout / "src/control_plane/cli.py").touch()
    target = tmp_path / "isolated target"
    (target / ".git").mkdir(parents=True)
    output = tmp_path / "units/howlplane-factory.service"
    args = argparse.Namespace(
        checkout=checkout, python=Path(sys.executable), state_dir=tmp_path / "state",
        target_repo=target, output=output, worker_path="/usr/bin:/bin",
    )
    install(args)
    unit = output.read_text()
    assert "%%h" in unit
    assert "$name" in unit
    assert '--authority-profile' not in unit
    assert "ExecStart=:" in unit
    assert "KillMode=mixed" in unit
    assert "Restart=on-failure" in unit
    assert "ExecStop=" not in unit
    install(args)  # Identical install is idempotent.
    output.write_text("owner customization\n")
    with pytest.raises(ValueError, match="different"):
        install(args)
    assert output.read_text() == "owner customization\n"


def test_service_rejects_controller_as_target_and_control_characters(tmp_path):
    from scripts.install_factory_service import unit_quote, install

    for value in ["line\nbreak", "carriage\rreturn", "nul\0byte", "tab\tvalue"]:
        with pytest.raises(ValueError):
            unit_quote(value)
    checkout = Path(__file__).resolve().parents[1]
    args = argparse.Namespace(
        checkout=checkout, python=Path(sys.executable), state_dir=tmp_path,
        target_repo=checkout, output=tmp_path / "test.service",
        worker_path="/usr/bin:/bin",
    )
    with pytest.raises(ValueError, match="isolated"):
        install(args)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal contract")
def test_real_idle_process_sigterm_releases_lock_and_can_resume(tmp_path):
    """Real CLI/process/state/lock path; an empty repo cannot dispatch providers."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = tmp_path / "state"
    command = [sys.executable, "-m", "howlplane.control_plane.cli", "factory"]
    env = {**os.environ, "PYTHONPATH": f"{Path(__file__).resolve().parents[1] / 'src'}:{Path(__file__).resolve().parents[1]}"}
    store = SupervisorStateStore(state / "supervisor")

    def run_and_stop(resume=False):
        process = subprocess.Popen(
            command + ["run", "--state-dir", str(state), "--target-repo", str(repo)]
            + (["--resume-stopped"] if resume else []),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not store.load().last_successful_tick_at:
                if process.poll() is not None:
                    pytest.fail(str(process.communicate()))
                if time.monotonic() >= deadline:
                    pytest.fail("Factory did not finish its idle tick")
                time.sleep(0.05)
            # Also ensure a resumed process acquired the lock before signalling.
            from howlplane.control_plane.locking import get_supervisor_lock_path
            while not get_supervisor_lock_path(state).exists():
                if time.monotonic() >= deadline:
                    pytest.fail("Factory did not acquire its supervisor lock")
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, (stdout, stderr)
            assert store.load().stopped_reason == "signal_sigterm"
            assert not get_supervisor_lock_path(state).exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    run_and_stop()
    run_and_stop(resume=True)


# ---- start/stop backend contracts (mocked subprocess/systemctl) ----

from types import SimpleNamespace

from howlplane.control_plane.factory import service
from howlplane.control_plane.factory.campaign import CampaignError


def _campaign(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(state_dir=state, target_dir=tmp_path / "target",
                           repository=SimpleNamespace(campaign_id="cid"))


def test_systemd_start_passes_caller_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "_active", lambda c, r: True)
    monkeypatch.setattr(service, "get_systemd_main_pid", lambda unit: 0)
    monkeypatch.setenv("PATH", "/nvm/bin:/usr/bin")
    monkeypatch.setenv("HOWLPLANE_X", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    monkeypatch.setenv("SECRET_TOKEN", "nope")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="0", stderr="")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    started, record = service.start_process(_campaign(tmp_path), None, None, verify_seconds=0)
    assert started and record.backend == "systemd"
    run_cmd = next(c for c in calls if c[0] == "systemd-run")
    assert "--setenv=PATH=/nvm/bin:/usr/bin" in run_cmd
    assert "--setenv=HOWLPLANE_X=1" in run_cmd
    assert "--setenv=XDG_CONFIG_HOME=/cfg" in run_cmd
    assert any(a.startswith("--setenv=PYTHONPATH=") and "/src" in a for a in run_cmd)
    assert not any("SECRET_TOKEN" in a for a in run_cmd)


def test_portable_start_fails_when_child_dies_and_shows_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    campaign = _campaign(tmp_path)

    def fake_popen(cmd, stdout=None, **kw):
        stdout.write("\n".join(f"line{i}" for i in range(30)) + "\nModuleNotFoundError: howlplane\n")
        stdout.flush()
        return SimpleNamespace(pid=999999, poll=lambda: 1)

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    with pytest.raises(CampaignError) as err:
        service.start_process(campaign, None, None, verify_seconds=0.2)
    text = str(err.value)
    assert "ModuleNotFoundError" in text and "line29" in text and "line5\n" not in text
    assert service.load_process(campaign) is None  # no success record persisted


def test_portable_start_succeeds_when_child_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    monkeypatch.setattr(service.subprocess, "Popen",
                        lambda cmd, **kw: SimpleNamespace(pid=os.getpid(), poll=lambda: None))
    started, record = service.start_process(_campaign(tmp_path), None, None, verify_seconds=0.2)
    assert started and record.backend == "process"


def test_systemd_start_fails_when_unit_dies(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "_active", lambda c, r: False)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="journal-line-boom" if cmd[0] == "journalctl" else "0", stderr="")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    with pytest.raises(CampaignError, match="journal-line-boom"):
        service.start_process(_campaign(tmp_path), None, None, verify_seconds=0.2)
    assert any(c[:3] == ["systemctl", "--user", "stop"] for c in calls)


def _record(backend, pid=4242, status="running"):
    return service.FactoryProcessRecord(pid, 1.0, __import__("socket").gethostname(), backend, ["x"],
                                        "now", "log", "unit-x" if backend == "systemd" else None, status)


def test_systemd_stop_always_stops_unit_even_when_not_active(tmp_path, monkeypatch):
    """During auto-restart delay is-active is non-zero; stop must still run."""
    campaign = _campaign(tmp_path)
    service._save_process(campaign, _record("systemd"))
    monkeypatch.setattr(service, "_active", lambda c, r: False)
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert "stopped" in service.stop_process(campaign).lower()
    assert ["systemctl", "--user", "stop", "unit-x"] in calls
    assert ["systemctl", "--user", "reset-failed", "unit-x"] in calls
    assert service.load_process(campaign).status == "stopped"


def test_portable_stop_escalates_to_sigkill(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    service._save_process(campaign, _record("process"))
    sent = []
    alive = {"v": True}

    def fake_killpg(pid, sig):
        sent.append(sig)
        if sig == signal.SIGKILL:
            alive["v"] = False

    monkeypatch.setattr(service.os, "killpg", fake_killpg)
    monkeypatch.setattr(service, "_active", lambda c, r: alive["v"])
    service.stop_process(campaign, timeout_seconds=0.2)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert service.load_process(campaign).status == "stopped"


def test_portable_stop_does_not_persist_stopped_when_process_survives(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    service._save_process(campaign, _record("process"))
    monkeypatch.setattr(service.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(service, "_active", lambda c, r: True)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(ticks) * 1.0)
    with pytest.raises(CampaignError):
        service.stop_process(campaign, timeout_seconds=3)
    assert service.load_process(campaign).status == "running"


def test_supervisor_lock_is_free_while_start_verifies_the_child(tmp_path, monkeypatch):
    """The child supervisor takes the same lock file; holding it during verification starves it."""
    from howlplane.control_plane.locking import SupervisorLock

    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    campaign = _campaign(tmp_path)
    monkeypatch.setattr(service.subprocess, "Popen",
                        lambda cmd, **kw: SimpleNamespace(pid=os.getpid(), poll=lambda: None))
    seen = {}

    def probe(camp, record, process, seconds):
        child = SupervisorLock(camp.state_dir, command="child supervisor")
        child.acquire()  # raises LockError if start still holds the launch lock
        child.release()
        seen["free"] = True

    monkeypatch.setattr(service, "_verify_started", probe)
    service.start_process(campaign, None, None, verify_seconds=0)
    assert seen == {"free": True}
