"""Ordinary test runs never resolve the operator's real agent CLIs."""

import os
import shutil
import stat

import pytest

from howlplane.control_plane import agent_readiness, orchestration
from tests._provider_isolation import path_without, provider_binaries

pytestmark = pytest.mark.unit


def _executable(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_provider_binaries_come_from_the_routing_registries():
    names = provider_binaries()
    assert {spec.binary for spec in agent_readiness.SPECS.values()} <= names
    assert set(orchestration.BINARIES.values()) <= names
    assert "ollama" in names


def test_path_without_hides_providers_and_keeps_their_neighbours(tmp_path):
    tools, local, plain = tmp_path / "tools", tmp_path / "local", tmp_path / "plain"
    go = _executable(tools, "go")
    _executable(tools, "agy")
    _executable(local, "ollama")
    _executable(plain, "make")
    (tools / "agy-notes").mkdir()
    path = os.pathsep.join([str(tools), "", str(local), str(plain), str(tools), str(tmp_path / "missing")])

    hidden = path_without(frozenset({"agy", "ollama"}), path, tmp_path / "mirrors")

    assert shutil.which("agy", path=hidden) is None
    assert shutil.which("ollama", path=hidden) is None
    assert os.path.realpath(shutil.which("go", path=hidden)) == os.path.realpath(go)
    assert shutil.which("make", path=hidden) == str(plain / "make")
    entries = hidden.split(os.pathsep)
    assert entries[1] == "" and entries[3] == str(plain) and entries[5] == str(tmp_path / "missing")
    assert entries[0] == entries[4], "a repeated directory reuses one mirror"
    assert (tmp_path / "mirrors" / "0" / "agy-notes").is_dir()


def test_path_without_leaves_a_clean_path_unchanged(tmp_path):
    _executable(tmp_path / "bin", "go")
    path = os.pathsep.join([str(tmp_path / "bin"), "/usr/bin"])
    assert path_without(frozenset({"agy"}), path, tmp_path / "mirrors") == path


def test_ordinary_tests_resolve_no_provider_cli():
    for name in sorted(provider_binaries()):
        assert shutil.which(name) is None, f"{name} resolves to {shutil.which(name)} during an ordinary test"
