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


def _stall_after(seconds: float):
    """A watchdog that declares a stall once `seconds` of wall time pass."""
    started = time.monotonic()
    return lambda *_: (
        {"reason": WATCHDOG_TERMINATION_STALL} if time.monotonic() - started > seconds else None
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
    result = backend.execute(
        _task(), tmp_path, watchdog_callback=_stall_after(0.5), watchdog_interval_seconds=0.05,
    )

    assert result.success is True


def test_normal_prose_quoting_quota_is_not_terminal_signal():
    signal = trusted_terminal_provider_signal(
        "I considered the phrase 'quota exceeded' in a proposed user message.", ""
    )
    assert signal is None


class _Stream:
    """Cumulative provider stdout, the way the streamed backend delivers it."""

    def __init__(self, observer: ProviderStreamObserver, now: list):
        self.observer, self.now, self.out = observer, now, ""

    def at(self, second: float, text: str = ""):
        self.now[0] = float(second)
        self.out += text
        return self.observer(self.out, "", second)

    def run(self, until: float, step: float = 5.0, text=lambda second: ""):
        second = self.now[0]
        while second < until:
            second = min(until, second + step)
            result = self.at(second, text(second))
            if result is not None:
                return second, result
        return None, None


def _observer(now, source=None, **kwargs):
    source = source if source is not None else ["clean"]
    kwargs.setdefault("no_progress_timeout_seconds", 360)
    kwargs.setdefault("post_change_grace_seconds", 420)
    kwargs.setdefault("hard_budget_seconds", 600)
    return ProviderStreamObserver(
        "agy", 180, lambda: source[0], clock=lambda: now[0], cpu_reader=lambda _pid: None, **kwargs,
    )


def test_repeated_spinner_and_counter_noise_is_not_liveness():
    now = [0.0]
    stream = _Stream(_observer(now), now)
    frames = ("\x1b[2K\r⠋ Working 00:01", "\r⠙ Working 00:02", "\rWorking 3%", "\rWorking 4%")
    stall_at, result = stream.run(600, 10, lambda second: frames[int(second / 10) % len(frames)] + "\n")
    assert result is not None and result["reason"] == WATCHDOG_TERMINATION_STALL
    assert result["diagnostics"]["threshold_fired"] == "silence"
    # Each distinct normalized frame counts once; after that it is noise.
    assert stall_at <= 30 + 180


def test_unique_noise_cannot_keep_a_provider_alive_past_no_progress_window():
    now = [0.0]
    stream = _Stream(_observer(now), now)
    def word(second):
        index = int(second) // 10
        return "".join(chr(97 + (index // 26 ** k) % 26) for k in range(3))

    stall_at, result = stream.run(600, 10, lambda second: f"thinking about {word(second)}\n")
    assert result is not None
    assert result["diagnostics"]["threshold_fired"] == "no_progress"
    assert stall_at == 360


def test_agy_claude_real_case_novel_output_survives_two_minutes():
    """Real run: killed at 120s with last_semantic_event=provider_started."""
    now = [0.0]
    stream = _Stream(_observer(now), now)
    lines = iter(f"Reading docs/section_{name}.md and planning" for name in "abcdefghijklmnop")
    stall_at, result = stream.run(90, 30, lambda _second: next(lines) + "\n")
    assert result is None and stream.observer.liveness_state() == "OUTPUT ACTIVE"
    stall_at, result = stream.run(300, 30, lambda _second: next(lines) + "\n")
    assert result is None
    assert stream.observer.last_progress_event == "provider_started"
    # Alive but not advancing: reported, not terminated, until no-progress.
    assert stream.observer.liveness_state() == "NO MEANINGFUL PROGRESS"


def test_codex_real_case_post_edit_validation_is_not_stalled_at_120s():
    """Real run: source_delta_changed, then killed 120s later inside a 600s budget."""
    now = [0.0]
    source = ["clean"]
    stream = _Stream(_observer(now, source), now)
    assert stream.at(60, "Planning backend layout\n") is None
    source[0] = "backend-written"
    assert stream.at(100, "") is None
    assert stream.observer.last_progress_event == "source_delta_changed"
    # No recognized tool event, no further file change: running a long local
    # validation that prints only unrecognized output.
    stall_at, result = stream.run(
        400, 15, lambda second: f"collecting module_{int(second)} ... ok\n" if int(second) % 45 == 0 else "",
    )
    assert result is None
    assert stream.observer.liveness_state() in {"POST-CHANGE VALIDATION", "OUTPUT ACTIVE"}


def test_post_change_grace_is_bounded():
    now = [0.0]
    source = ["clean"]
    stream = _Stream(_observer(now, source), now)
    source[0] = "edited"
    assert stream.at(10) is None
    stall_at, result = stream.run(600, 5)
    assert result is not None
    assert result["diagnostics"]["threshold_fired"] in {"silence", "post_change_grace"}
    assert stall_at == 10 + 420
    assert result["diagnostics"]["source_changed"] is True


def test_silent_provider_is_stalled_with_full_diagnostics():
    now = [0.0]
    stream = _Stream(_observer(now), now)
    stall_at, result = stream.run(600, 5)
    assert stall_at == 180
    diagnostics = result["diagnostics"]
    assert diagnostics["threshold_fired"] == "silence"
    assert diagnostics["threshold_seconds"] == 180
    assert diagnostics["elapsed_seconds"] == 180
    assert diagnostics["hard_budget_seconds"] == 600
    assert diagnostics["seconds_since_novel_output"] is None
    assert diagnostics["seconds_since_process_activity"] == "unavailable"
    assert diagnostics["seconds_since_progress"] == 180
    assert diagnostics["last_progress_event"] == "provider_started"
    assert diagnostics["source_changed"] is False
    assert diagnostics["raw_output_activity"] is False


def test_empty_polls_are_not_output_activity():
    """Regression: joining empty stdout and stderr with a newline read as output."""
    now = [0.0]
    observer = _observer(now)
    for second in (30, 60, 90):
        now[0] = float(second)
        observer("", "", second)
    assert observer.last_novel_output_at is None


def _cpu_only_run(cpu_per_step: float, step: int):
    """A provider with no output and no delta whose process tree burns CPU."""
    now = [0.0]
    cpu = [0.0]
    observer = ProviderStreamObserver(
        "claude_code", 180, lambda: "clean", clock=lambda: now[0],
        no_progress_timeout_seconds=360, cpu_reader=lambda _pid: cpu[0],
    )
    observer.bind_process(4242)
    for second in range(step, 601, step):
        now[0], cpu[0] = float(second), cpu[0] + cpu_per_step
        result = observer("", "", second)
        if result is not None:
            return second, result
    return None, None


def test_process_cpu_is_liveness_but_not_progress():
    stall_at, result = _cpu_only_run(2.0, 10)
    assert stall_at == 360
    assert result["diagnostics"]["threshold_fired"] == "no_progress"
    assert result["diagnostics"]["seconds_since_process_activity"] == 0


def test_idle_process_cpu_does_not_count_as_liveness():
    stall_at, result = _cpu_only_run(0.01, 10)
    assert stall_at == 180 and result["diagnostics"]["threshold_fired"] == "silence"


def test_idle_cpu_trickle_below_rate_is_not_liveness():
    """Reviewer finding: 2% idle CPU used to disable the silence timeout."""
    stall_at, result = _cpu_only_run(0.1, 5)
    assert stall_at == 180 and result["diagnostics"]["threshold_fired"] == "silence"


def test_recognized_codex_exec_and_test_summaries_are_progress():
    now = [0.0]
    stream = _Stream(_observer(now), now)
    assert stream.at(100, "exec\n/bin/bash -lc 'pytest -q backend/tests'\n") is None
    assert stream.observer.last_progress_at == 100
    assert stream.at(200, " succeeded in 2317ms:\n8 passed, 8 skipped in 0.05s\n") is None
    assert stream.observer.last_progress_at == 200



def test_a_looping_control_record_stops_counting_as_progress():
    """Reviewer finding: a reprinted record with a counter kept a hang alive."""
    now = [0.0]
    stream = _Stream(_observer(now), now)
    cycle = "exec\n/bin/bash -lc 'pytest -q'\n succeeded in {n}ms:\n3 passed in 0.{n}s\n"
    progress_times = []
    for second in range(20, 600, 20):
        before = stream.observer.last_progress_at
        result = stream.at(second, cycle.format(n=second))
        if stream.observer.last_progress_at != before:
            progress_times.append(second)
        if result is not None:
            break
    assert len(progress_times) <= 2
    assert result is not None and result["diagnostics"]["threshold_fired"] in {"silence", "no_progress"}
    assert second <= 360 + 20  # well before the 600s hard budget


def test_ordinary_prose_is_not_a_progress_marker():
    from howlplane.control_plane.agent_execution import _TOOL_EVENT

    for prose in (
        "I will improve the tooling", "stool", "toolkit docs", "tools unavailable",
        "We need a checkpoint later", "Waiting for 2 failed requests",
        "Retrying tool call (attempt 3)", "Retrying request (attempt 3), 0 passed",
    ):
        assert not _TOOL_EVENT.search(prose), prose
    for record in (
        "exec", "succeeded in 5258ms:", "8 passed, 7 skipped in 0.03s",
        "===== 3 passed in 0.10s =====", "All checks passed!", "Tool invocation: write_file x",
        "*** Update File: backend/app/main.py", "exited 1 in 230ms:",
    ):
        assert _TOOL_EVENT.search(record), record


def test_process_cpu_reader_measures_this_process():
    import os

    from howlplane.control_plane.agent_execution import process_tree_cpu_seconds

    measured = process_tree_cpu_seconds(os.getpid())
    if not Path("/proc/self/stat").exists():
        assert measured is None
    else:
        assert measured is not None and measured >= 0


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


def test_hard_budget_wins_over_active_liveness(tmp_path: Path):
    """Constant novel output and CPU cannot extend delegated execution authority."""
    backend = _backend(
        "import itertools,time\n"
        "for i in itertools.count():\n"
        "    print('step ' + ''.join(chr(97 + (i // 26 ** k) % 26) for k in range(4)), flush=True)\n"
        "    time.sleep(.02)\n"
    )
    observer = ProviderStreamObserver(
        "agy", 60, lambda: "clean", no_progress_timeout_seconds=60, hard_budget_seconds=1,
    )
    start = time.monotonic()
    result = backend.execute(
        _task(), tmp_path, timeout_seconds=1,
        watchdog_callback=observer, watchdog_interval_seconds=0.05,
    )

    assert time.monotonic() - start < 5
    assert WATCHDOG_TERMINATION_KEY not in (result.metadata or {})
    assert ProviderPoolManager().classify_failure("agy", result) == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED


def test_provider_exiting_during_stall_verdict_keeps_its_real_result(tmp_path: Path):
    backend = _backend("print('done', flush=True)")

    def slow_verdict(_out, _err, _elapsed):
        time.sleep(1.0)  # the provider exits while the verdict is formed
        return {"reason": WATCHDOG_TERMINATION_STALL}

    result = backend.execute(_task(), tmp_path, watchdog_callback=slow_verdict)

    assert result.success is True
    assert WATCHDOG_TERMINATION_KEY not in (result.metadata or {})
    assert "done" in result.stdout


def test_streamed_backend_binds_provider_pid_for_process_liveness(tmp_path: Path):
    bound = []

    class Observer:
        def bind_process(self, pid):
            bound.append(pid)

        def __call__(self, *_):
            return None

    result = _backend("pass").execute(_task(), tmp_path, watchdog_callback=Observer())
    assert result.success is True and len(bound) == 1 and bound[0] > 0


def test_stalled_provider_gets_bounded_cooldown(tmp_path: Path):
    backend = _backend("import time; time.sleep(10)")
    result = backend.execute(
        _task(), tmp_path,
        watchdog_callback=lambda *_: {"reason": WATCHDOG_TERMINATION_STALL},
    )
    pool = ProviderPoolManager(state_path=tmp_path / "capacity.json")
    assert pool.record_result("agy", result, task_id="WATCHDOG") == ProviderFailureClass.PROVIDER_STALLED
    assert pool.get_resource_status("agy").retry_after is not None


def test_terminal_quota_envelope_is_immediate_through_the_observer():
    now = [0.0]
    observer = ProviderStreamObserver("devin_cli", 180, lambda: "clean", clock=lambda: now[0])
    now[0] = 1.7
    result = observer(
        "", 'Error: Agent error: Your daily usage quota has been exhausted.\n'
        '{\n  "cognition.ai/errorKind": "resource_exhausted"\n}\n', 1.7,
    )
    assert result is not None and result["reason"] == WATCHDOG_TERMINATION_EXHAUSTION


def test_from_config_capacity_state_honors_isolation_override(tmp_path: Path, monkeypatch):
    target = tmp_path / "isolated" / "provider_capacity.json"
    monkeypatch.setenv("HOWLPLANE_PROVIDER_CAPACITY_FILE", str(target))
    pool = ProviderPoolManager.from_config(probe_on_start=False)
    pool.record_result(
        "agy",
        SubprocessAgentBackend("agy", sys.executable, lambda *_a, **_k: [sys.executable, "-c", "import time; time.sleep(5)"]).execute(
            _task(), tmp_path, watchdog_callback=lambda *_: {"reason": WATCHDOG_TERMINATION_STALL},
        ),
        task_id="WATCHDOG",
    )
    assert target.exists()


def _late_writer(tmp_path: Path, marker: Path, delay: float) -> Path:
    child = tmp_path / "late_writer.py"
    child.write_text(
        f"import pathlib,time\ntime.sleep({delay})\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    return child


def test_descendants_do_not_survive_watchdog_termination(tmp_path: Path):
    """Reviewer finding: a leftover child could write into the next attempt."""
    marker = tmp_path / "late_write.txt"
    child = _late_writer(tmp_path, marker, delay=1.5)
    backend = _backend(
        f"import subprocess,sys,time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(30)\n"
    )
    result = backend.execute(
        _task(), tmp_path, watchdog_callback=_stall_after(0.3), watchdog_interval_seconds=0.05,
    )
    assert result.metadata[WATCHDOG_TERMINATION_KEY] == WATCHDOG_TERMINATION_STALL
    time.sleep(2.0)
    assert not marker.exists()


def test_descendants_do_not_survive_a_provider_that_exits(tmp_path: Path):
    marker = tmp_path / "late_write.txt"
    child = _late_writer(tmp_path, marker, delay=1.0)
    backend = _backend(
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )
    result = backend.execute(_task(), tmp_path, watchdog_callback=lambda *_: None)
    assert result.success is True
    time.sleep(1.5)
    assert not marker.exists()


def test_quota_screen_at_the_deadline_is_not_recorded_as_a_budget_timeout(tmp_path: Path):
    """Reviewer finding: the budget check must not hide a terminal quota."""
    backend = _backend(
        "import sys,time; time.sleep(.6); print('quota exceeded', file=sys.stderr, flush=True); time.sleep(10)"
    )
    result = backend.execute(
        _task(), tmp_path, timeout_seconds=1,
        watchdog_callback=lambda *_: None,  # an observer that never recognizes it
        watchdog_interval_seconds=0.05,
    )
    assert result.metadata[WATCHDOG_TERMINATION_KEY] == WATCHDOG_TERMINATION_EXHAUSTION
    assert ProviderPoolManager().classify_failure("agy", result) == ProviderFailureClass.QUOTA_EXHAUSTED

