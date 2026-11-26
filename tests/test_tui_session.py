"""A real `howlplane tui` session in a pseudo terminal: real command, runtime and pool.

The only agent executable is a fake that records any launch. Each session
previews an objective and never confirms a run, so nothing may be launched,
and the terminal must be restored.
"""

import contextlib
import json
import os
import signal
import sys
import termios

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from tests._tui_support import fake_agent_sandbox, read_until, spawn_tui

pytestmark = pytest.mark.integration


def start_session(tmp_path):
    """A sandbox and `howlplane tui` on a new pty, once it shows the inventory: (box, pid, fd)."""
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    box = fake_agent_sandbox(tmp_path)
    env = {"HOME": str(tmp_path), "PATH": f"{box['bindir']}:/usr/bin:/bin", "TERM": "xterm",
           "HOWLPLANE_LOCAL_CONFIG": str(box["config"]), "HOWLPLANE_CAPACITY_PATH": str(tmp_path / "cap.json"),
           "HOWLFRAME_BIN": hf, "XDG_STATE_HOME": str(tmp_path / "state"),
           "PYTHONPATH": os.pathsep.join(sys.path)}
    pid, fd = spawn_tui(box["repo"], env)
    box["started"] = read_until(fd, b"codex  READY  usable")
    assert b"codex  READY  usable" in box["started"]
    return box, pid, fd


@contextlib.contextmanager
def reaped(pid):
    """Kills the session when the block fails, so a failed check never leaves it running."""
    try:
        yield
    except BaseException:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise


def exit_of(pid, fd):
    """The session's exit code and its last output, through leaving the alternate screen."""
    tail = read_until(fd, b"\x1b[?1049l")
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), tail


def open_confirmation(fd):
    os.write(fd, b"/run\r")
    assert b"Run with codex? [y/N]" in read_until(fd, b"[y/N]")


def decline(fd):
    os.write(fd, b"n\r")
    assert b"Run not started." in read_until(fd, b"Run not started.")


def assert_nothing_launched(box):
    assert not box["marker"].exists(), "no agent may be launched by a preview or a declined run"
    assert not (box["repo"] / ".task_runs").exists(), "a declined run creates no task"


def test_interactive_session_previews_and_declines_without_launching(tmp_path):
    box, pid, fd = start_session(tmp_path)
    with reaped(pid):
        os.write(fd, b"Add a /health endpoint\r")
        assert b"Route:     codex for implementation" in read_until(fd, b"Route:     codex")
        open_confirmation(fd)
        decline(fd)
        os.write(fd, b"\x04")
        code, tail = exit_of(pid, fd)
    assert code == 0
    assert b"\x1b[?1049l" in tail, "the alternate screen is left on exit"
    assert_nothing_launched(box)
    log = tmp_path / "state" / "howlplane" / "tui" / "logs" / "tui_events.jsonl"
    codes = [json.loads(line)["code"] for line in log.read_text().splitlines()]
    assert codes[0] == "TUI_STARTED" and codes[-1] == "TUI_STOPPED"
    assert "TUI_RUN_STARTED" not in codes


def test_sigterm_restores_the_terminal(tmp_path):
    box, pid, fd = start_session(tmp_path)
    with reaped(pid):
        assert b"\x1b[?1049h" in box["started"], "raw mode enters the alternate screen"
        assert not termios.tcgetattr(fd)[3] & termios.ICANON, "the runtime holds raw mode"
        os.kill(pid, signal.SIGTERM)
        code, tail = exit_of(pid, fd)
    assert code == 143
    assert b"\x1b[?1049l" in tail
    assert termios.tcgetattr(fd)[3] & termios.ICANON, "canonical mode is back after SIGTERM"


def test_a_pasted_yes_cannot_start_a_run(tmp_path):
    """The runtime marks pasted input; the TUI refuses it as a run confirmation."""
    box, pid, fd = start_session(tmp_path)
    with reaped(pid):
        assert b"\x1b[?2004h" in box["started"], "bracketed paste is on"
        os.write(fd, b"\x1b[200~Add a /health endpoint\x1b[201~\r")  # a pasted objective is fine
        routed = read_until(fd, b"Route:     codex for implementation")
        assert b"\x1b[32mRoute:     codex for implementation\x1b[0m" in routed, "styles are drawn on a colour terminal"
        open_confirmation(fd)
        os.write(fd, b"\x1b[200~y\n\x1b[201~\r")
        assert b"A pasted answer cannot start a run." in read_until(fd, b"A pasted answer cannot start a run.")
        decline(fd)
        os.write(fd, b"\x04")
        code, tail = exit_of(pid, fd)
    assert code == 0
    assert b"\x1b[?2004l" in tail, "bracketed paste is turned off on exit"
    assert_nothing_launched(box)
