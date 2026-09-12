"""
test_howldream_live_integration.py

Closes howldream/issues.md item 2: HowlPlane's other HowlDream tests all
route through DeterministicTestExplorationProvider (directly, or via
HOWLDREAM_PROVIDER=test), so the real `import howldream` / `explore()`
boundary in NativeHowlDreamProvider was never actually exercised by CI.

This module is the real-import counterpart. It is marked `live` (see
tests/conftest.py): deselected by default on every ordinary pytest
invocation, and only run when HOWLPLANE_LIVE_PROVIDERS is set (the nightly/
workflow_dispatch CI job, gated behind the HOWLPLANE_RUN_LIVE_TESTS repo
variable, with `howldream` installed from a pinned commit — see
.github/workflows/test.yml). It requires the real `howldream` package to be
importable; it is not a substitute for the deterministic-provider tests, and
does not replace them.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from src.control_plane.howldream_runner import (
    ExplorationBudget,
    NativeHowlDreamProvider,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contracts" / "howl" / "howl.exploration_result.v1.schema.json"


@pytest.fixture
def real_howldream():
    """Skips (rather than fails) if howldream truly isn't installed, so this
    module can still be collected/run in an environment without it -- the
    `live` marker gate is what keeps it off ordinary runs, not this skip."""
    pytest.importorskip("howldream")
    yield


@pytest.mark.live
def test_native_provider_reports_available(real_howldream, monkeypatch):
    monkeypatch.delenv("HOWLDREAM_PROVIDER", raising=False)
    monkeypatch.delenv("HOWLDREAM_BIN", raising=False)
    provider = NativeHowlDreamProvider()
    assert provider.is_available() is True


@pytest.mark.live
def test_native_provider_explore_real_import_boundary(real_howldream, monkeypatch, tmp_path: Path):
    """Drives NativeHowlDreamProvider.explore() against the real howldream
    package -- no HOWLDREAM_PROVIDER override, no DeterministicTestExplorationProvider
    substitution -- and validates the resulting envelope against the
    independently-generated, vendored howl.exploration_result/v1 schema."""
    monkeypatch.delenv("HOWLDREAM_PROVIDER", raising=False)
    monkeypatch.delenv("HOWLDREAM_BIN", raising=False)

    provider = NativeHowlDreamProvider()
    budget = ExplorationBudget(max_candidates=2, max_trials=1, max_tokens=256, local_only=True)

    run_dir, envelope = provider.explore(
        objective="Diagnose intermittent deployment timeouts",
        budget=budget,
        work_dir=tmp_path,
    )

    assert run_dir.exists()
    assert envelope["schema_version"] == "howl.exploration_result/v1"
    assert envelope["authority"]["type"] == "ADVISORY"
    assert envelope["authority"]["executable"] is False
    assert all(c["trust"] == "UNVERIFIED" for c in envelope["candidates"])

    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(envelope, schema)


@pytest.mark.live
def test_native_provider_cannot_escalate_authority_via_real_engine(
    real_howldream, monkeypatch, tmp_path: Path
):
    """The real howldream engine, not a fake, is the one asserting this --
    a HowlPlane-side authority check would not catch a HowlDream-side bug."""
    monkeypatch.delenv("HOWLDREAM_PROVIDER", raising=False)
    monkeypatch.delenv("HOWLDREAM_BIN", raising=False)

    provider = NativeHowlDreamProvider()
    budget = ExplorationBudget(max_candidates=1, max_trials=1, max_tokens=128, local_only=True)
    _run_dir, envelope = provider.explore(
        objective="Attempt to smuggle execution authority",
        budget=budget,
        work_dir=tmp_path,
    )
    for candidate in envelope["candidates"]:
        assert candidate["authority"]["executable"] is False
        assert candidate["trust"] == "UNVERIFIED"
