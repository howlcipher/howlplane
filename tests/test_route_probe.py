"""`howlplane route --probe` (TUI-00 HP-C3): the preview sees a missing executable."""

import json

import pytest

from howlplane.control_plane import cli, config_loader
from tests._tui_support import fake_agent_sandbox

pytestmark = pytest.mark.contract


@pytest.fixture
def box(tmp_path, monkeypatch):
    box = fake_agent_sandbox(tmp_path)
    monkeypatch.setenv("HOWLPLANE_LOCAL_CONFIG", str(box["config"]))
    monkeypatch.setenv("HOWLPLANE_CAPACITY_PATH", str(tmp_path / "capacity.json"))
    monkeypatch.setenv("PATH", f"{box['bindir']}:/usr/bin:/bin")
    monkeypatch.setattr(config_loader, "default_loader",
                        config_loader.ConfigLoader(local_config_path=str(box["config"])), raising=False)
    box["agent"].unlink()  # the executable is gone, but nothing has recorded that yet
    return box


def route(capsys, box, *flags):
    code = cli.main(["route", "add a health endpoint", "--repo", str(box["repo"]), "--json", *flags])
    return code, json.loads(capsys.readouterr().out)


def test_default_route_is_unchanged_and_does_not_probe(capsys, box):
    code, decision = route(capsys, box)
    assert code == 0 and decision["selected"]["resource_id"] == "codex"


def test_probe_excludes_a_missing_executable_without_writing_state(capsys, box):
    code, decision = route(capsys, box, "--probe")
    assert code == 3 and decision["status"] == "BLOCKED"
    assert ("codex", "MISSING_EXECUTABLE") in [(x["resource_id"], x["reason"]) for x in decision["exclusions"]]
    assert not box["marker"].exists() and not (box["tmp"] / "capacity.json").exists()
