"""TUI-02 live acceptance: one real provider run through `howlplane tui`.

Deselected unless HOWLPLANE_LIVE_PROVIDERS is set (tests/conftest.py). It uses
the operator's own environment, so a signed-in provider CLI does the work, on
a scratch repository under tmp_path with a trivial objective. Evidence goes to
a temporary control plane root, never to the checkout's ledger. Runbook:
documentation/journals/2026-11-23_tui01_interactive_terminal.md.
"""

import json
import os
import subprocess
import sys

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from tests._tui_support import read_until, scratch_repo, spawn_tui
from tests.test_tui_governed_run import checkout_ledger_lines, session_codes, unwrap

pytestmark = [pytest.mark.live, pytest.mark.acceptance]

FINAL_STATES = ("complete", "awaiting_human", "failed", "blocked")


def usable_resource(env):
    out = subprocess.run([sys.executable, "-m", "howlplane.control_plane", "providers", "--json"],
                         capture_output=True, text=True, timeout=120, env=env)
    if out.returncode != 0:
        return None
    rows = json.loads(out.stdout)["resources"]
    usable = [r["identity"]["resource_id"] for r in rows
              if r["configured"] and r["enabled"] and r["readiness"] == "READY"]
    return usable[0] if usable else None


def test_a_real_provider_run_reaches_an_outcome(tmp_path):
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    repo = scratch_repo(tmp_path / "repo")
    root = tmp_path / "control-plane"
    root.mkdir()
    (root / "AGENTS.md").write_text("# live acceptance control plane root\n", encoding="utf-8")
    env = dict(os.environ, HOWLPLANE_HOME=str(root), HOWLFRAME_BIN=hf, TERM="xterm",
               XDG_STATE_HOME=str(tmp_path / "state"), PYTHONPATH=os.pathsep.join(sys.path))
    resource = usable_resource(env)
    if resource is None:
        pytest.skip("no configured provider is READY in this environment")
    before = checkout_ledger_lines()
    pid, fd = spawn_tui(repo, env)
    try:
        assert b"usable" in read_until(fd, b"usable", timeout=120)
        os.write(fd, b"Create a file named hello.txt containing the single word hello\r")
        assert b"Route:" in read_until(fd, b"Route:", timeout=120)
        os.write(fd, b"/run\r")
        assert b"[y/N]" in read_until(fd, b"[y/N]", timeout=60)
        os.write(fd, b"y\r")
        screen = unwrap(read_until(fd, b"Outcome:", timeout=1800).decode(errors="replace"))
        screen += unwrap(read_until(fd, b"objective or /help", timeout=60).decode(errors="replace"))
    finally:
        os.write(fd, b"\x04")
        read_until(fd, b"\x1b[?1049l", timeout=120)
        os.waitpid(pid, 0)
    outcome = screen.split("Outcome:", 1)[1].split()[0]
    assert outcome in FINAL_STATES, screen[-2000:]
    codes = session_codes(tmp_path)
    assert "TUI_RUN_STARTED" in codes and "TUI_RUN_FINISHED" in codes
    assert checkout_ledger_lines() == before
