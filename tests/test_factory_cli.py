#!/usr/bin/env python3
"""Tests for the factory CLI subcommands."""

from datetime import datetime, timezone

from src.control_plane.cli import main
from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore


class FakeSupervisor:
    def __init__(self):
        self.state = SupervisorState.IDLE
        self.stopped = False
        self.resumed = False

    def tick(self):
        class Result:
            selected_work_item_id = None
            reason = "tick"
        self.state = SupervisorState.WAITING_FOR_WORK
        return Result()

    def run_once(self):
        return self.tick()

    def status(self):
        return {
            "state": self.state,
            "last_tick_at": None,
            "next_wake_at": datetime.now(timezone.utc).isoformat(),
            "current_work_item_id": None,
            "current_task_id": None,
            "current_dispatch_id": None,
            "observations_consumed": 0,
            "failure_count": 0,
            "stopped_reason": None,
            "dispatch_history_count": 0,
            "transition_history_count": 0,
            "admission_decisions_count": 0,
            "proposals_awaiting_authority": [],
            "recent_completed": [],
            "recent_failed": [],
            "provider_wake_conditions": {},
            "parked_items": [],
        }

    def stop(self, reason="operator_stop"):
        self.stopped = True
        self.state = SupervisorState.STOPPED

    def resume(self):
        self.resumed = True
        self.state = SupervisorState.IDLE

    def run(self, until=None):
        self.state = SupervisorState.STOPPED


def test_factory_status_creates_default_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: FakeSupervisor(),
    )
    code = main(["factory", "status", "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    assert code == 0
    assert "idle" in captured.out


def test_factory_stop_and_resume(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    code = main(["factory", "stop", "--state-dir", str(state_dir)])
    assert code == 0
    store = SupervisorStateStore(state_dir / "supervisor")
    assert store.load().state == SupervisorState.STOPPED

    code = main(["factory", "resume", "--state-dir", str(state_dir)])
    assert code == 0
    assert store.load().state == SupervisorState.IDLE


def test_factory_run_once_invokes_tick(tmp_path, monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: supervisor,
    )
    code = main(["factory", "run-once", "--state-dir", str(tmp_path / "state")])
    assert code == 0
    assert supervisor.state == SupervisorState.WAITING_FOR_WORK


def test_factory_run_invokes_loop(tmp_path, monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(
        "src.control_plane.cli._build_factory_supervisor",
        lambda args, sleep=None: supervisor,
    )
    code = main(["factory", "run", "--state-dir", str(tmp_path / "state"), "--until", "0.1"])
    assert code == 0
    assert supervisor.state == SupervisorState.STOPPED


def test_build_factory_supervisor_binds_authority_envelope_once(tmp_path):
    from types import SimpleNamespace

    from src.control_plane.cli import _build_factory_supervisor

    args = SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        target_repo=".",
        authority_profile="strict",
    )
    supervisor = _build_factory_supervisor(args)
    engine = supervisor.dispatcher._engine_factory()
    assert engine.authority_envelope is not None
    assert engine.authority_envelope.profile_id == "strict"
    # Authority is bound once on the same engine instance; repeated factory calls
    # reuse it rather than renewing.
    engine2 = supervisor.dispatcher._engine_factory()
    assert engine2 is engine
    assert engine2.authority_envelope is engine.authority_envelope
