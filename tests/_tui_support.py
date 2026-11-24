"""Shared helpers for the `howlplane tui` tests: pty reads and a fake-agent sandbox."""

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict


def read_until(fd: int, needle: bytes, timeout: float = 30) -> bytes:
    """Reads a pty until ``needle`` appears, the child closes it, or ``timeout`` passes."""
    data, end = b"", time.monotonic() + timeout
    while needle not in data and time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return data


def scratch_repo(path: Path) -> Path:
    """An empty Git repository with one commit."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return path


def spawn_tui(repo: Path, env: Dict[str, str], engine: str = "python"):
    """Starts `howlplane tui` in ``repo`` on a new pseudo terminal; returns (pid, master fd)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(repo)
        os.execve(sys.executable, [sys.executable, "-m", "howlplane.control_plane", "tui", "--engine", engine], env)
    return pid, fd


def fake_agent_sandbox(tmp_path: Path, agent_body: str = "exit 97") -> Dict[str, Path]:
    """A Git repository, a `codex` stand-in that records any launch, and a config enabling it.

    ``agent_body`` is the shell the stand-in runs after recording the launch.
    """
    repo = scratch_repo(tmp_path / "repo")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "launched"
    agent = bindir / "codex"
    agent.write_text(f"#!/bin/sh\ntouch {marker}\n{agent_body}\n", encoding="utf-8")
    agent.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('[ai_resources]\noperating_mode = "connected"\n'
                      '[ai_resources.providers.codex]\nenabled = true\n', encoding="utf-8")
    return {"repo": repo, "bindir": bindir, "agent": agent, "marker": marker, "config": config, "tmp": tmp_path}
