"""Deterministic streamed-provider watchdog contracts."""

import sys
import time
from pathlib import Path

from howlplane.control_plane.agent_execution import (
    PROVIDER_RETRY_AFTER_SECONDS_KEY,
    WATCHDOG_TERMINATION_KEY,
    WATCHDOG_TERMINATION_EXHAUSTION,
    WATCHDOG_TERMINATION_STALL,
    ProviderStreamObserver,
    SubprocessAgentBackend,
    trusted_terminal_provider_signal,
)
from howlplane.control_plane.resource_models import ProviderFailureClass
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec


def _backend(program: str) -> SubprocessAgentBackend:
    return SubprocessAgentBackend(
        "agy", sys.executable,
        lambda *_args, **_kwargs: [sys.executable, "-u", "-c", program],
    )


def _task() -> TaskSpec:
    return TaskSpec(task_id="WATCHDOG", repository="test", objective="test")


def test_session_limit_screen_terminates_live_process_promptly(tmp_path: Path):
    backend = _backend(
        "import sys,time; print('session limit reached', file=sys.stderr, flush=True); time.sleep(10)"
    )
    start = time.monotonic()
    result = backend.execute(
        _task(), tmp_path, watchdog_callback=lambda out, err, _: trusted_terminal_provider_signal(out, err),
    )

    assert time.monotonic() - start < 2
    assert result.metadata[WATCHDOG_TERMINATION_KEY] == WATCHDOG_TERMINATION_EXHAUSTION
    assert ProviderPoolManager().classify_failure("agy", result) == ProviderFailureClass.SESSION_LIMIT


def test_retry_after_from_trusted_terminal_control_is_retained(tmp_path: Path):
    backend = _backend(
        "import sys,time; print('quota exceeded\\nretry-after 7 seconds', file=sys.stderr, flush=True); time.sleep(10)"
    )
    result = backend.execute(
        _task(), tmp_path, watchdog_callback=lambda out, err, _: trusted_terminal_provider_signal(out, err),
    )

    assert result.metadata[PROVIDER_RETRY_AFTER_SECONDS_KEY] == 7
    pool = ProviderPoolManager(state_path=tmp_path / "capacity.json")
    pool.record_result("agy", result, task_id="WATCHDOG")
    assert pool.get_resource_status("agy").retry_after is not None


def test_stall_needs_no_output_and_no_repository_delta(tmp_path: Path):
    backend = _backend("import time; time.sleep(10)")
    last_activity = time.monotonic()

    def observer(_out, _err, _elapsed):
        if time.monotonic() - last_activity >= 0.2:
            return {"reason": WATCHDOG_TERMINATION_STALL}
        return None

    result = backend.execute(_task(), tmp_path, watchdog_callback=observer)

    assert result.metadata[WATCHDOG_TERMINATION_KEY] == WATCHDOG_TERMINATION_STALL
    assert ProviderPoolManager().classify_failure("agy", result) == ProviderFailureClass.PROVIDER_STALLED


def test_healthy_silent_process_is_not_killed_before_conservative_window(tmp_path: Path):
    backend = _backend("import time; time.sleep(.1)")
    started = time.monotonic()
    result = backend.execute(
        _task(), tmp_path,
        watchdog_callback=lambda *_: (
            {"reason": WATCHDOG_TERMINATION_STALL}
            if time.monotonic() - started > 0.5 else None
        ),
        watchdog_interval_seconds=0.05,
    )

    assert result.success is True


def test_normal_prose_quoting_quota_is_not_terminal_signal():
    signal = trusted_terminal_provider_signal(
        "I considered the phrase 'quota exceeded' in a proposed user message.", ""
    )
    assert signal is None


def test_spinner_and_unique_noise_do_not_reset_semantic_stall_timer():
    now = [0.0]
    observer = ProviderStreamObserver("agy", 120, lambda: "clean", clock=lambda: now[0])
    for second, frame in enumerate(("⠋ Working", "⠙ Working", "random 19a", "⠸ Working"), 1):
        now[0] = float(second * 30)
        result = observer(frame, "", now[0])
    assert result is not None
    assert result["reason"] == WATCHDOG_TERMINATION_STALL
    assert result["diagnostics"]["raw_output_activity"] is True
    assert result["diagnostics"]["last_semantic_event"] == "provider_started"


def test_ansi_redraw_does_not_reset_semantic_stall_timer():
    now = [0.0]
    observer = ProviderStreamObserver("agy", 120, lambda: "clean", clock=lambda: now[0])
    for second, frame in enumerate(("\x1b[2K\rWorking 00:01", "\x1b[2K\rWorking 00:02"), 1):
        now[0] = float(second * 59)
        assert observer(frame, "", now[0]) is None
    now[0] = 120.0
    result = observer("\x1b[2K\rWorking 00:03", "", now[0])
    assert result is not None
    assert result["reason"] == WATCHDOG_TERMINATION_STALL


def test_session_limit_countdown_is_terminal_without_newline():
    now = [0.0]
    observer = ProviderStreamObserver("agy", 120, lambda: "clean", clock=lambda: now[0])
    result = observer("\r\x1b[2KSession limit reached. Retry in 59:59", "", 0)
    assert result is not None
    assert result["reason"] == WATCHDOG_TERMINATION_EXHAUSTION


def test_semantic_output_and_source_delta_reset_stall_timer():
    now = [0.0]
    source = ["clean"]
    observer = ProviderStreamObserver("agy", 120, lambda: source[0], clock=lambda: now[0])
    now[0] = 80.0
    assert observer("Tool invocation: write_file src/feature.py", "", 80) is None
    now[0] = 170.0
    source[0] = "feature-delta"
    assert observer("", "", 170) is None
    assert observer.last_meaningful_progress_at == 170.0


def test_control_plane_only_activity_does_not_reset_semantic_timer():
    now = [0.0]
    observer = ProviderStreamObserver("agy", 120, lambda: "task-delta-excludes-control-plane", clock=lambda: now[0])
    now[0] = 120.0
    result = observer("", "", 120)
    assert result is not None
    assert result["reason"] == WATCHDOG_TERMINATION_STALL
