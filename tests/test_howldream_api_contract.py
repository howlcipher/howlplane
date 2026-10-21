"""Real offline Dream API boundary, without enabling remote provider tests."""

import json
from pathlib import Path
import tomllib

import jsonschema
import pytest

from howlplane.control_plane.howldream_runner import ExplorationBudget, NativeHowlDreamProvider


def test_optional_dream_api_dependency_is_pinned():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    requirements = project["project"]["optional-dependencies"].get("dream-api", [])
    assert requirements == [
        "howldream @ git+https://github.com/howlcipher/howldream.git@9594c98b25df624733c9609fa5743aef091e59f5"
    ]


def test_native_dream_api_executes_real_offline_engine(tmp_path, monkeypatch):
    pytest.importorskip("howldream")
    from howlplane.control_plane import howldream_runner

    def no_cli(*args, **kwargs):
        pytest.fail("the installed Dream API must execute without a CLI fallback")

    monkeypatch.setattr(howldream_runner.component_bin, "resolve", no_cli)
    provider = NativeHowlDreamProvider()
    run_dir, result = provider.explore(
        "Compare synthetic queue retry strategies",
        ExplorationBudget(max_candidates=1, max_trials=1, max_tokens=128), tmp_path,
    )
    assert run_dir.is_dir()
    assert result["schema_version"] == "howl.exploration_result/v1"
    assert result["authority"] == {"type": "ADVISORY", "executable": False}
    assert all(candidate["provenance"]["observation_kind"] == "SIMULATED"
               for candidate in result["candidates"])
    schema = json.loads((Path(__file__).parents[1] / "contracts/howl/howl.exploration_result.v1.schema.json").read_text())
    jsonschema.validate(result, schema)
