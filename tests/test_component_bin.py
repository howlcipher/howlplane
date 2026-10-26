"""component_bin: the one place HowlPlane finds and describes other Howl components."""

import json
import os
from pathlib import Path

from howlplane.control_plane import component_bin


def _exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, **extra: str) -> dict:
    return {"HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "data"), "PATH": str(tmp_path / "path"),
            **extra}


def test_env_var_name():
    assert component_bin.env_var("howlchangeops") == "HOWLCHANGEOPS_BIN"
    assert component_bin.env_var("howlplane-engine") == "HOWLPLANE_ENGINE_BIN"


def test_discovery_order(tmp_path):
    env = _env(tmp_path)
    assert component_bin.resolve("howlframe", env=env) is None

    local = _exe(tmp_path / "home" / ".local" / "bin" / "howlframe")
    assert component_bin.resolve("howlframe", env=env) == str(local)

    release = _exe(tmp_path / "data" / "howl" / "components" / "howlframe" / "releases" / "0.1.0" / "howlframe")
    (tmp_path / "data" / "howl" / "components" / "howlframe" / "current").write_text("0.1.0\n")
    assert component_bin.resolve("howlframe", env=env) == str(release)

    on_path = _exe(tmp_path / "path" / "howlframe")
    assert component_bin.resolve("howlframe", env=env) == str(on_path)

    override = _exe(tmp_path / "custom" / "frame")
    assert component_bin.resolve("howlframe", env={**env, "HOWLFRAME_BIN": str(override)}) == str(override.resolve())


def test_managed_release_through_a_symlink(tmp_path):
    env = _env(tmp_path)
    component = tmp_path / "data" / "howl" / "components" / "howlchangeops"
    release = _exe(component / "releases" / "0.2.0" / "howlchangeops")
    (component / "current").symlink_to(component / "releases" / "0.2.0")
    assert component_bin.resolve("howlchangeops", env=env) == str(component / "current" / "howlchangeops")
    assert release.is_file()


def test_an_explicit_override_that_is_not_executable_is_not_replaced(tmp_path):
    env = _env(tmp_path, HOWLFRAME_BIN=str(tmp_path / "missing"))
    _exe(tmp_path / "path" / "howlframe")
    assert component_bin.resolve("howlframe", env=env) is None


def test_legacy_variable_and_alias(tmp_path):
    env = _env(tmp_path)
    alias = _exe(tmp_path / "path" / "changeops")
    assert component_bin.resolve("howlchangeops", env=env, aliases=("changeops",)) == str(alias)
    legacy = _exe(tmp_path / "legacy" / "co")
    assert component_bin.resolve(
        "howlchangeops", env={**env, "CHANGEOPS_BIN": str(legacy)}, legacy_env=("CHANGEOPS_BIN",)
    ) == str(legacy.resolve())


DESCRIPTOR = {
    "schema": "howl.interop/v1", "component": "howlchangeops", "version": "0.2.0", "exit_codes": {"0": "ok"},
    "commands": [{"path": ["evaluate"], "flags": ["--json"], "json": True}, {"path": [], "flags": ["--version"], "json": False}],
}


def test_describe_reads_and_caches_the_descriptor(tmp_path):
    calls = tmp_path / "calls"
    exe = _exe(tmp_path / "hco", f"echo x >> {calls}\ncat <<'JSON'\n{json.dumps(DESCRIPTOR)}\nJSON")
    first = component_bin.describe(str(exe))
    assert first == DESCRIPTOR
    assert component_bin.describe(str(exe)) == DESCRIPTOR
    assert calls.read_text().count("x") == 1
    assert component_bin.offers(first, ["evaluate"], "--json")
    assert component_bin.offers(first, [], "version")
    assert not component_bin.offers(first, ["evaluate"], "--deadline")
    assert not component_bin.offers(first, ["approve"], "--json")
    assert not component_bin.offers(None, ["evaluate"])


def test_describe_returns_none_for_a_release_without_the_command(tmp_path):
    assert component_bin.describe(str(_exe(tmp_path / "old", "echo usage >&2; exit 1"))) is None
    assert component_bin.describe(str(_exe(tmp_path / "chatty", "echo not json"))) is None
    assert component_bin.describe(str(tmp_path / "absent")) is None


def test_parse_result_checks_schema_and_component():
    envelope = {"schema": "howl.result/v1", "component": "howlchangeops", "command": ["evaluate"],
                "status": "awaiting_human", "data": {}, "error": None}
    assert component_bin.parse_result(json.dumps(envelope), "howlchangeops") == envelope
    assert component_bin.parse_result(json.dumps(envelope), "howlframe") is None
    assert component_bin.parse_result("Decision: ALLOW", "howlchangeops") is None
    assert component_bin.parse_result(json.dumps({**envelope, "schema": "x/v1"}), "howlchangeops") is None


def test_default_environment_is_the_process_environment(tmp_path, monkeypatch):
    exe = _exe(tmp_path / "bin" / "howldream")
    monkeypatch.setenv("HOWLDREAM_BIN", str(exe))
    assert component_bin.resolve("howldream") == str(exe.resolve())
    assert os.environ["HOWLDREAM_BIN"] == str(exe)
