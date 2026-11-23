"""Shared helpers for the `howlplane tui` tests: pty reads and a fake-agent sandbox."""

import os
import select
import subprocess
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


def fake_agent_sandbox(tmp_path: Path) -> Dict[str, Path]:
    """A Git repository, a `codex` stand-in that records any launch, and a config enabling it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "launched"
    agent = bindir / "codex"
    agent.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n", encoding="utf-8")
    agent.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('[ai_resources]\noperating_mode = "connected"\n'
                      '[ai_resources.providers.codex]\nenabled = true\n', encoding="utf-8")
    return {"repo": repo, "bindir": bindir, "agent": agent, "marker": marker, "config": config, "tmp": tmp_path}
