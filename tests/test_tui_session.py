"""A real `howlplane tui` session in a pseudo terminal: real command, reducer and pool.

The only agent executable is a fake that records any launch. The session types
an objective, opens the run confirmation, declines it and leaves, so nothing
may be launched and the terminal must be restored.
"""

import os
import sys

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from tests._tui_support import fake_agent_sandbox, read_until, spawn_tui

pytestmark = pytest.mark.integration


def test_interactive_session_previews_and_declines_without_launching(tmp_path):
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    box = fake_agent_sandbox(tmp_path)
    repo, bindir, marker, config = box["repo"], box["bindir"], box["marker"], box["config"]
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "TERM": "xterm",
           "HOWLPLANE_LOCAL_CONFIG": str(config), "HOWLPLANE_CAPACITY_PATH": str(tmp_path / "cap.json"),
           "HOWLFRAME_BIN": hf, "XDG_STATE_HOME": str(tmp_path / "state"),
           "PYTHONPATH": os.pathsep.join(sys.path)}
    pid, fd = spawn_tui(repo, env)
    try:
        assert b"codex  READY  usable" in read_until(fd, b"codex  READY  usable")
        os.write(fd, b"Add a /health endpoint\r")
        assert b"Route:     codex for implementation" in read_until(fd, b"Route:     codex")
        os.write(fd, b"/run\r")
        assert b"Run with codex? [y/N]" in read_until(fd, b"[y/N]")
        os.write(fd, b"n\r")
        assert b"Run not started." in read_until(fd, b"Run not started.")
        os.write(fd, b"\x04")
        tail = read_until(fd, b"\x1b[?1049l")
    finally:
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert b"\x1b[?1049l" in tail, "the alternate screen is left on exit"
    assert not marker.exists(), "no agent may be launched by a preview or a declined run"
    assert not (repo / ".task_runs").exists(), "a declined run creates no task"
    log = tmp_path / "state" / "howlplane" / "tui" / "logs" / "tui_events.jsonl"
    codes = [line.split('"code": "')[1].split('"')[0] for line in log.read_text().splitlines()]
    assert codes[0] == "TUI_STARTED" and codes[-1] == "TUI_STOPPED"
    assert "TUI_RUN_STARTED" not in codes
