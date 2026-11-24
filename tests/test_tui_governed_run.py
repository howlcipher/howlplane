"""TUI-02: a real governed run through `howlplane tui`, with a stand-in agent.

The session is the real command in a pseudo terminal, and the run is the real
`howlplane work --execute` child it starts: routing, implementation, review,
the human boundary and evidence are HowlPlane's own. Only the agent executable
is a stand-in, so no provider is contacted. Evidence goes to a temporary
control plane root (HOWLPLANE_HOME), never to the checkout's ledger.
"""

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.presentation.style import strip_ansi
from tests._tui_support import fake_agent_sandbox, read_until, spawn_tui

pytestmark = pytest.mark.integration

CHECKOUT_LEDGER = Path(__file__).resolve().parents[1] / "logs" / "control_plane" / "evidence_ledger.jsonl"


def checkout_ledger_lines():
    return CHECKOUT_LEDGER.read_text(errors="replace").splitlines() if CHECKOUT_LEDGER.is_file() else []


def unwrap(screen):
    """Rejoins lines the HowlFrame runtime wrapped at the terminal width (continuations start with two spaces)."""
    return re.sub(r"\r*\n  ", "", strip_ansi(screen))


def start_session(box):
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    tmp = box["tmp"]
    root = tmp / "control-plane"
    root.mkdir()
    (root / "AGENTS.md").write_text("# test control plane root\n", encoding="utf-8")
    env = {"HOME": str(tmp), "PATH": f"{box['bindir']}:/usr/bin:/bin", "TERM": "xterm",
           "HOWLPLANE_HOME": str(root), "HOWLPLANE_LOCAL_CONFIG": str(box["config"]),
           "HOWLPLANE_CAPACITY_PATH": str(tmp / "cap.json"), "HOWLPLANE_DIAGNOSTICS_DIR": str(tmp / "diag"),
           "HOWLFRAME_BIN": hf, "XDG_STATE_HOME": str(tmp / "state"),
           "PYTHONPATH": os.pathsep.join(sys.path)}
    pid, fd = spawn_tui(box["repo"], env)
    return pid, fd, root


def confirm_run(fd):
    assert b"codex  READY  usable" in read_until(fd, b"codex  READY  usable")
    os.write(fd, b"Create hello.txt containing hello\r")
    assert b"Route:     codex" in read_until(fd, b"Route:     codex")
    os.write(fd, b"/run\r")
    assert b"[y/N]" in read_until(fd, b"[y/N]")
    os.write(fd, b"y\r")


def finish(pid, fd):
    os.write(fd, b"\x04")
    read_until(fd, b"\x1b[?1049l")
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def session_codes(tmp):
    log = tmp / "state" / "howlplane" / "tui" / "logs" / "tui_events.jsonl"
    return [json.loads(line)["code"] for line in log.read_text().splitlines()]


def task_entries(root, task_id):
    ledger = root / "logs" / "control_plane" / "evidence_ledger.jsonl"
    return [json.loads(line) for line in ledger.read_text().splitlines() if task_id in line]


def test_run_reaches_the_owner_boundary_and_shows_the_commands(tmp_path):
    box = fake_agent_sandbox(tmp_path, agent_body="echo hello > hello.txt\nexit 0")
    before = checkout_ledger_lines()
    pid, fd, root = start_session(box)
    try:
        confirm_run(fd)
        screen = unwrap(read_until(fd, b"Or:        howlplane reject", timeout=240).decode(errors="replace"))
    finally:
        code = finish(pid, fd)
    assert code == 0
    assert "Outcome:   awaiting_human (exit 2)" in screen
    task_id = screen.split("Next:      howlplane approve ")[1].split()[0]
    assert task_id.startswith("TUI-")
    assert f"Then:      howlplane resume {task_id}" in screen
    assert "decision_packet.md" in screen.split("Detail:")[1].splitlines()[0]
    assert (box["repo"] / ".task_runs" / task_id / "decision_packet.md").is_file()
    assert (box["repo"] / "hello.txt").read_text().strip() == "hello"
    codes = session_codes(tmp_path)
    assert "TUI_RUN_STARTED" in codes and "TUI_RUN_FINISHED" in codes
    assert task_entries(root, task_id), "the run's evidence is in the run's control plane ledger"
    assert checkout_ledger_lines() == before, "the checkout ledger is never written"


def test_cancel_stops_the_run_and_records_it(tmp_path):
    box = fake_agent_sandbox(tmp_path, agent_body="sleep 120")
    before = checkout_ledger_lines()
    pid, fd, root = start_session(box)
    try:
        confirm_run(fd)
        assert b"running (/cancel)" in read_until(fd, b"running (/cancel)")
        assert b"Progress:" in read_until(fd, b"Progress:", timeout=60)
        os.write(fd, b"/cancel\r")
        screen = unwrap(read_until(fd, b"Code:      RUN_CANCELLED", timeout=120).decode(errors="replace"))
    finally:
        code = finish(pid, fd)
    assert code == 0
    assert "Retry:     RECONCILE_FIRST" in screen
    stragglers = subprocess.run(["pgrep", "-f", str(box["agent"])], capture_output=True, text=True)
    if stragglers.stdout.strip():
        for pid_text in stragglers.stdout.split():
            os.kill(int(pid_text), signal.SIGKILL)
        pytest.fail("the stand-in agent outlived the cancelled run")
    codes = session_codes(tmp_path)
    assert "TUI_RUN_CANCELLED" in codes
    task_id = next(d.name for d in (box["repo"] / ".task_runs").iterdir() if d.name.startswith("TUI-"))
    actions = [e.get("action") for e in task_entries(root, task_id)]
    assert box["marker"].exists(), "the agent was really running when cancelled"
    assert "implementation_started" in actions and actions[-1] == "task_cancelled", actions
    assert checkout_ledger_lines() == before
