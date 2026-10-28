"""Ordinary test runs never resolve the operator's real agent CLIs."""

import os
import shutil
import stat
import subprocess
import sys

import pytest

from howlplane.control_plane import agent_readiness, orchestration
from howlplane.control_plane.agent_execution import AgentBackendRegistry, SubprocessAgentBackend
from tests._provider_isolation import path_without, provider_binaries

pytestmark = pytest.mark.unit


def _executable(directory, name, body="exit 0"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_provider_binaries_come_from_every_routing_registry():
    names = provider_binaries()
    assert {spec.binary for spec in agent_readiness.SPECS.values()} <= names
    assert set(orchestration.BINARIES.values()) <= names
    backends = AgentBackendRegistry._instances.values()
    assert {b.binary_name for b in backends if isinstance(b, SubprocessAgentBackend)} <= names
    assert {"gemini", "ollama"} <= names


def test_path_without_hides_providers_and_keeps_their_neighbours(tmp_path):
    tools, local, plain = tmp_path / "tools", tmp_path / "local", tmp_path / "plain"
    go = _executable(tools, "go")
    _executable(tools, "agy")
    _executable(local, "ollama")
    _executable(plain, "make")
    (tools / "agy-notes").mkdir()
    path = os.pathsep.join([str(tools), str(local), str(plain), str(tools), str(tmp_path / "missing")])

    hidden = path_without(frozenset({"agy", "ollama"}), path, tmp_path / "mirrors")

    assert shutil.which("agy", path=hidden) is None
    assert shutil.which("ollama", path=hidden) is None
    assert os.path.realpath(shutil.which("go", path=hidden)) == os.path.realpath(go)
    assert shutil.which("make", path=hidden) == str(plain / "make")
    entries = hidden.split(os.pathsep)
    assert entries[2] == str(plain) and entries[4] == str(tmp_path / "missing")
    assert entries[0] == entries[3], "a repeated directory reuses one mirror"
    assert (tmp_path / "mirrors" / "0" / "agy-notes").is_dir()


def test_path_without_drops_empty_and_relative_entries(tmp_path, monkeypatch):
    _executable(tmp_path / "bin", "agy")
    _executable(tmp_path, "agy")
    monkeypatch.chdir(tmp_path)
    hidden = path_without(frozenset({"agy"}), os.pathsep.join(["", "bin", ".", "/usr/bin"]), tmp_path / "mirrors")
    assert hidden == "/usr/bin"
    assert shutil.which("agy", path=hidden) is None


def test_only_executable_files_named_like_a_provider_are_hidden(tmp_path):
    tools = tmp_path / "tools"
    _executable(tools, "codex")
    (tools / "agent").mkdir()
    (tools / "devin").write_text("notes\n", encoding="utf-8")
    hidden = path_without(frozenset({"codex", "agent", "devin"}), str(tools), tmp_path / "mirrors")
    assert (tmp_path / "mirrors" / "0" / "agent").is_dir()
    assert (tmp_path / "mirrors" / "0" / "devin").is_file()
    assert shutil.which("codex", path=hidden) is None


@pytest.mark.parametrize("interpreter", ["python3", "python3.14t"])
@pytest.mark.parametrize("through_symlink", [False, True])
def test_a_mirrored_virtualenv_keeps_its_interpreter(tmp_path, interpreter, through_symlink):
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    _executable(venv / "bin", "agy")
    if not (venv / "bin" / interpreter).exists():
        (venv / "bin" / interpreter).symlink_to("python3")
    tool = venv / "bin" / "venvtool"
    tool.write_text(f"#!/usr/bin/env {interpreter}\nimport sys\nprint(sys.prefix)\n", encoding="utf-8")
    tool.chmod(0o755)
    bin_dir = venv / "bin"
    if through_symlink:
        (tmp_path / "links").mkdir()
        bin_dir = tmp_path / "links" / "env-bin"
        bin_dir.symlink_to(venv / "bin")

    real = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    hidden = path_without(frozenset({"agy"}), real, tmp_path / "mirrors")

    def prefix(path):
        run = subprocess.run(["venvtool"], env={**os.environ, "PATH": path}, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        return os.path.realpath(run.stdout.strip())

    assert shutil.which("agy", path=hidden) is None
    # The mirror behaves as the real PATH does. CPython finds pyvenv.cfg next
    # to the unresolved interpreter path, so a venv bin reached through a
    # symlinked directory is not a venv even without the mirror.
    assert prefix(hidden) == prefix(real)
    if not through_symlink:
        assert prefix(hidden) == os.path.realpath(venv)


def test_ordinary_tests_resolve_no_provider_cli():
    for name in sorted(provider_binaries()):
        assert shutil.which(name) is None, f"{name} resolves to {shutil.which(name)} during an ordinary test"
