"""Service contracts: safe rendering and cooperative supervisor shutdown."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from src.control_plane.factory.supervisor_state import SupervisorStateStore
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
    command = [sys.executable, "-m", "src.control_plane.cli", "factory"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
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
            from src.control_plane.locking import get_supervisor_lock_path
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
