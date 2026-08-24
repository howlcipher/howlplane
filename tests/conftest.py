import os
from pathlib import Path
import shutil
import tempfile
import pytest


# Real HowlFrame compiler integration tests deliberately exercise the genuine
# `HOWLFRAME_BIN` env override -> `command -v howlframe` -> "unavailable" discovery
# contract (see src/control_plane/howlframe_runner.find_howlframe_binary) and
# already call `pytest.skip`/assert on `HOWLFRAME_UNAVAILABLE` when no real
# compiler is provisioned. They must NOT be pointed at the synthesis fixture's
# fake compiler, or they silently exercise a compiler substitute that does not
# implement their expected fidelity (real dogfood tests are the deliberately
# unfaked real-integration layer; only unit/control-plane and synthesis tests
# get the fake compiler injected).
_REAL_COMPILER_INTEGRATION_MODULES = {
    "test_howlframe_dogfood.py",
    "test_launcher.py",
}


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_git_contamination():
    """Remove a stale temp-root .git created by earlier agent/tool runs so that
    repository-discovery tests see a clean temp namespace."""
    tmp_git = Path(tempfile.gettempdir()) / ".git"
    if tmp_git.exists():
        try:
            shutil.rmtree(tmp_git)
        except OSError:
            pass
    yield


@pytest.fixture(autouse=True)
def setup_test_environment(request, monkeypatch):
    """
    Ensures deterministic compiler discovery and fake agent availability
    for all unit, synthesis, and CLI tests across clean CI and local environments.
    """
    repo_root = Path(__file__).resolve().parents[1]
    fake_compiler = repo_root / "tests" / "fake_compiler.py"
    module_name = Path(str(request.node.fspath)).name

    # If HOWLFRAME_BIN is not set and howlframe is not on PATH, point to fake_compiler
    # -- except for the real compiler integration tests, which need the genuine
    # discovery contract to correctly skip/degrade when no real compiler exists.
    if module_name not in _REAL_COMPILER_INTEGRATION_MODULES:
        if not os.environ.get("HOWLFRAME_BIN") and not shutil.which("howlframe"):
            if fake_compiler.is_file():
                monkeypatch.setenv("HOWLFRAME_BIN", str(fake_compiler))

    # In automated test runs, set deterministic baseline mode unless live providers requested
    if not os.environ.get("HOWLPLANE_LIVE_PROVIDERS") and not os.environ.get("HOWLPLANE_SYNTHESIS_MODE"):
        monkeypatch.setenv("HOWLPLANE_SYNTHESIS_MODE", "deterministic_baseline")


@pytest.fixture
def orchestrator_factory():
    """Factory fixture providing Orchestrator instances with automatic shutdown.

    Each test can call ``orchestrator_factory()`` to create a new ``Orchestrator``.
    All created instances are shut down after the test completes.
    """
    from src.core.orchestrator import Orchestrator
    created = []

    def _make(*args, **kwargs):
        instance = Orchestrator(*args, **kwargs)
        created.append(instance)
        return instance

    yield _make

    # Teardown: shut down all created orchestrators
    for orchestrator in created:
        try:
            orchestrator.shutdown()
        except Exception:
            # Suppress shutdown errors to avoid test failures
            pass
