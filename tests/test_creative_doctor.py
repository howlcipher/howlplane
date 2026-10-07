"""`howlplane doctor --creative`: dependency, interpreter and policy preflight."""

import json
from pathlib import Path
import types

import pytest

from howlplane.control_plane import cli, creative_doctor as cd

pytestmark = pytest.mark.unit

CLIS = ("howldream", "howlwriter", "howlcreate")


def run(tmp_path, *, env=None, interpreter=None, config=None, missing=(), broken=None):
    """Each component CLI is found and answers `--version`, unless named in missing/broken."""
    def which(name):
        return None if name in missing else f"/opt/{name}/bin/{name}"

    def probe(argv):
        name = argv[0].rsplit("/", 1)[-1]
        assert argv[1:] == ["--version"]
        if broken and name in broken:
            return 1, broken[name]
        return 0, f"{name} 1.0.0"

    return cd.summarize(cd.run_checks(
        env=env if env is not None else {"HOWL_FORBID_LOCAL_INFERENCE": "1"},
        command_config=config, workspace=tmp_path, which=which, probe=probe,
        interpreter=interpreter or ("/usr/bin/python3", (3, 14, 5), str(tmp_path / "venv"), "/usr")))


def statuses(report):
    return {c["id"]: c["status"] for c in report["checks"]}


def venv(tmp_path, version="3.14.5", home="/usr/bin"):
    (tmp_path / "venv").mkdir(exist_ok=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = {version}\n")


def test_healthy_environment_has_no_fail(tmp_path):
    venv(tmp_path)
    report = run(tmp_path)
    assert report["counts"]["FAIL"] == 0
    found = statuses(report)
    assert all(found[f"cli.{name}"] == "PASS" for name in CLIS)
    assert found["policy.local"] == "PASS"


@pytest.mark.parametrize("version,home,expected", [
    ("3.13.2", "/usr/bin", "FAIL"),
    ("3.14.5", "/nonexistent/old-home/bin", "FAIL"),
    ("3.14.4", "/usr/bin", "WARN"),
])
def test_stale_interpreter_and_venv(tmp_path, version, home, expected):
    venv(tmp_path, version, home)
    assert statuses(run(tmp_path))["venv"] == expected


def test_local_model_policy(tmp_path):
    venv(tmp_path)
    assert statuses(run(tmp_path, env={}))["policy.local"] == "WARN"
    unsafe = run(tmp_path, env={"OLLAMA_HOST": "http://127.0.0.1:11434"})
    assert statuses(unsafe)["policy.local_config"] == "FAIL"
    blocked = run(tmp_path, env={"OLLAMA_HOST": "x", "HOWL_FORBID_LOCAL_INFERENCE": "1"})
    assert statuses(blocked)["policy.local_config"] == "WARN"
    remote = run(tmp_path, env={"OPENAI_BASE_URL": "https://api.example.com",
                                "HOWL_FORBID_LOCAL_INFERENCE": "1"})
    assert statuses(remote)["policy.local_config"] == "PASS"


class _FakeCommandConfig:
    """Mirrors provider-core's contract: explicit argv, remote only, no local launchers."""

    def __init__(self, argv, remote):
        if remote is not True or argv[0] in {"ollama", "bash"}:
            raise ValueError("command requires operator-declared remote execution")
        self.argv, self.adapter, self.output_format = tuple(argv), None, "text"

    @classmethod
    def read(cls, path):
        value = json.loads(Path(path).read_text())
        return cls(value["argv"], value.get("remote"))


def test_command_config_validated_without_exposing_values(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "howl_provider_core", types.SimpleNamespace(
        CommandConfig=_FakeCommandConfig, ProviderError=ValueError))
    venv(tmp_path)
    good = tmp_path / "remote.json"
    good.write_text(json.dumps({"argv": ["python3", "--api-key=SECRET123"], "remote": True}))
    report = run(tmp_path, config=good)
    assert statuses(report)["provider.remote"] == "PASS"
    assert "SECRET123" not in json.dumps(report)
    bad = tmp_path / "local.json"
    bad.write_text(json.dumps({"argv": ["ollama", "run"], "remote": True}))
    assert statuses(run(tmp_path, config=bad))["provider.remote"] == "FAIL"


def test_doctor_creative_cli_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cd, "run_checks", lambda **kw: [cd.Check("x", "X", cd.PASS, "fine")])
    assert cli.main(["doctor", "--creative", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "howlplane.doctor.creative/v1"
    assert report["status"] == "PASS"
    monkeypatch.setattr(cd, "run_checks", lambda **kw: [cd.Check("x", "X", cd.FAIL, "broken", "fix it")])
    assert cli.main(["doctor", "--creative"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "Suggested action: fix it" in out


def test_every_component_cli_runs_in_its_own_environment(tmp_path):
    venv(tmp_path)
    report = run(tmp_path)
    for name in CLIS:
        row = next(c for c in report["checks"] if c["id"] == f"cli.{name}")
        assert row["status"] == "PASS" and f"/opt/{name}/bin/{name}" in row["detail"]


def test_missing_component_cli_fails_with_install_guidance(tmp_path):
    venv(tmp_path)
    row = next(c for c in run(tmp_path, missing=("howlcreate",))["checks"] if c["id"] == "cli.howlcreate")
    assert row["status"] == "FAIL" and "on PATH" in row["fix"]


def test_stale_component_environment_fails_at_the_cli(tmp_path):
    # Run 5 / run-042: howlcreate's own venv carried a provider-core without classify_failure.
    venv(tmp_path)
    error = "ImportError: cannot import name 'classify_failure' from 'howl_provider_core'"
    row = next(c for c in run(tmp_path, broken={"howlcreate": "Traceback...\n" + error})["checks"]
               if c["id"] == "cli.howlcreate")
    assert row["status"] == "FAIL" and "classify_failure" in row["detail"]


@pytest.mark.parametrize("profile, status", [
    ({"argv": ["python3", "--api-key=SECRET123"], "remote": True}, "PASS"),
    ({"argv": ["python3", "-p", "--tools", ""], "remote": True}, "PASS"),
    ({"argv": ["", "-p"], "remote": True}, "FAIL"),
    ({"argv": ["bash", "-c", "x"], "remote": True}, "FAIL"),
    ({"argv": ["ollama", "run"], "remote": True}, "FAIL"),
    ({"argv": ["python3"], "remote": False}, "FAIL"),
    ({"argv": []}, "FAIL"),
])
def test_command_config_shape_checked_without_provider_core(tmp_path, monkeypatch, profile, status):
    """Provider-core lives in the components' environments now; the preflight checks the profile's
    shape itself and HowlWriter re-validates it before its call."""
    import sys
    monkeypatch.setitem(sys.modules, "howl_provider_core", None)
    venv(tmp_path)
    config = tmp_path / "remote.json"
    config.write_text(json.dumps(profile))
    report = run(tmp_path, config=config)
    assert statuses(report)["provider.remote"] == status
    assert "SECRET123" not in json.dumps(report)


def test_doctor_does_not_mutate_environment(tmp_path):
    venv(tmp_path)
    before = sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    run(tmp_path, env={})
    assert sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before

