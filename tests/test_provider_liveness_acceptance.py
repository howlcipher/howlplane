"""Disposable end-to-end acceptance for provider liveness (Grocery Mission 001).

Real subprocess "providers" run through the governed orchestrator, the real
streamed backend, the real watchdog, a real git repository, and a disposable
provider pool. Thresholds are scaled down (1s silence stands in for 180s) but
keep the production ratios: silence < no-progress < post-change grace < hard
budget. No real mission, repository, or Factory state is touched.
"""

import sys
import time
from pathlib import Path
from typing import Dict

import pytest

from howlplane.control_plane.agent_execution import SubprocessAgentBackend
from tests.test_provider_failover import (
    _first_then_healthy,
    _init_test_repo,
    _read_file,
    _retained,
    _run_failover_task,
)

pytestmark = [pytest.mark.acceptance, pytest.mark.slow]

SILENCE = 1.0
NO_PROGRESS = 4.0
POST_CHANGE_GRACE = 5.0
HARD_BUDGET = 12

WRITE_FEATURE = (
    "import pathlib\n"
    "pathlib.Path('src/feature.py').write_text('def run():\\n    return True\\n')\n"
)
UNIQUE_WORD = "''.join(chr(97 + (i // 26 ** k) % 26) for k in range(4))"


def _provider(resource_id: str, program: str) -> SubprocessAgentBackend:
    return SubprocessAgentBackend(
        resource_id, sys.executable,
        lambda *_args, **_kwargs: [sys.executable, "-u", "-c", program],
    )


def _run(tmp_path: Path, first: SubprocessAgentBackend, **overrides):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _first_then_healthy(first)
    settings: Dict = dict(
        provider_stall_timeout_seconds=SILENCE,
        provider_no_progress_timeout_seconds=NO_PROGRESS,
        provider_post_change_grace_seconds=POST_CHANGE_GRACE,
        provider_watchdog_interval_seconds=0.05,
        timeout_seconds=HARD_BUDGET,
    )
    settings.update(overrides)
    started = time.monotonic()
    result = _run_failover_task(repo, resolver, max_attempts=2, **settings)
    return repo, result, time.monotonic() - started


def _providers(result):
    return [attempt["resource_id"] for attempt in result.implementation_attempts]


def test_active_output_provider_runs_past_the_old_stall_window(tmp_path: Path):
    """AGY/Claude shape: new unrecognized output, no delta, longer than silence."""
    program = (
        "import time\n"
        f"for i in range(14):\n    print('reading ' + {UNIQUE_WORD}, flush=True); time.sleep(.2)\n"
        + WRITE_FEATURE
    )
    repo, result, elapsed = _run(tmp_path, _provider("resource_a", program))

    assert elapsed > 2 * SILENCE
    assert result.final_state == "complete"
    assert _providers(result) == ["resource_a"]


def test_post_edit_validating_provider_is_not_stalled(tmp_path: Path):
    """Codex shape: edit, then quiet validation well past the silence window."""
    program = (
        "import pathlib,time\n"
        "pathlib.Path('src/pricing.py').write_text('def cheapest(prices):\\n    return min(prices)\\n')\n"
        "time.sleep(3 * %r)\n" % SILENCE
    )
    repo, result, elapsed = _run(tmp_path, _provider("resource_a", program))

    assert elapsed > 3 * SILENCE
    assert result.final_state == "complete"
    assert _providers(result) == ["resource_a"]
    assert (repo / "src" / "pricing.py").exists()


def test_silent_hung_provider_is_terminated(tmp_path: Path):
    repo, result, elapsed = _run(tmp_path, _provider("resource_a", "import time; time.sleep(60)"))

    first = result.implementation_attempts[0]
    assert first["failure_class"] == "PROVIDER_STALLED"
    assert first["watchdog_diagnostics"]["threshold_fired"] == "silence"
    assert _providers(result) == ["resource_a", "resource_b"]
    assert result.final_state == "complete"
    assert elapsed < HARD_BUDGET


@pytest.mark.parametrize("noise, threshold", [
    ("'⠋ Working 00:0' + str(i % 10) + ' ' + str(i) + '%'", "silence"),
    ("'thinking ' + " + UNIQUE_WORD, "no_progress"),
])
def test_noisy_hung_provider_is_eventually_terminated(tmp_path: Path, noise: str, threshold: str):
    program = (
        "import itertools,time\n"
        f"for i in itertools.count():\n    print({noise}, flush=True); time.sleep(.05)\n"
    )
    repo, result, elapsed = _run(tmp_path, _provider("resource_a", program))

    first = result.implementation_attempts[0]
    assert first["failure_class"] == "PROVIDER_STALLED"
    assert first["watchdog_diagnostics"]["threshold_fired"] == threshold
    assert result.final_state == "complete"
    assert elapsed < HARD_BUDGET


def test_terminal_quota_provider_fails_over_immediately(tmp_path: Path):
    program = (
        "import sys,time\n"
        "sys.stderr.write('Error: Agent error: Your daily usage quota has been exhausted.\\n"
        '{\\n  \"cognition.ai/errorKind\": \"resource_exhausted\"\\n}\\n\')\n'
        "sys.stderr.flush(); time.sleep(60)\n"
    )
    repo, result, _elapsed = _run(tmp_path, _provider("resource_a", program))

    first = result.implementation_attempts[0]
    assert first["failure_class"] == "QUOTA_EXHAUSTED"
    assert first["duration_seconds"] < SILENCE
    assert _providers(result) == ["resource_a", "resource_b"]


def test_hard_budget_terminates_an_active_provider(tmp_path: Path):
    program = (
        "import itertools,time\n"
        f"for i in itertools.count():\n    print('step ' + {UNIQUE_WORD}, flush=True); time.sleep(.05)\n"
    )
    repo, result, elapsed = _run(
        tmp_path, _provider("resource_a", program),
        timeout_seconds=2, provider_no_progress_timeout_seconds=30,
        provider_post_change_grace_seconds=30,
    )

    first = result.implementation_attempts[0]
    assert first["failure_class"] == "EXECUTION_BUDGET_EXCEEDED"
    assert first["duration_seconds"] < 4
    assert _providers(result) == ["resource_a", "resource_b"]


def test_failover_with_partial_changes_preserves_and_isolates(tmp_path: Path):
    program = (
        "import pathlib,time\n"
        "pkg = pathlib.Path('frontend/src'); pkg.mkdir(parents=True)\n"
        "(pkg / 'App.tsx').write_text('export const App = () => null;\\n')\n"
        "time.sleep(60)\n"
    )
    repo, result, _elapsed = _run(tmp_path, _provider("resource_a", program))

    first = result.implementation_attempts[0]
    assert first["failure_class"] == "PROVIDER_STALLED"
    retained = _retained(result, "01-resource_a")
    patch = (Path(result.run_dir) / retained["patch_path"]).read_text(encoding="utf-8")
    assert "frontend/src/App.tsx" in patch
    assert result.final_state == "complete"
    assert not (repo / "frontend").exists()
    assert _read_file(repo, "src/feature.py").endswith("True\n")
