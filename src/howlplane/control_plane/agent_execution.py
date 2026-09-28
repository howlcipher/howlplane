#!/usr/bin/env python3
"""
agent_execution.py

Normalized execution abstraction for AI agents across implementation,
remediation, and review roles. Supports CLI backends, local execution,
and deterministic mock/fake backends for testing and CI.
"""

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib, inspect, json, os, re, selectors, shlex, shutil, subprocess, time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Union

from howlplane.control_plane import workspace_trust
from howlplane.control_plane.locking import LocalInferenceLock, LockError
from howlplane.control_plane.provider_execution_profile import (
    MUTATION_TOOLS,
    ProviderExecutionProfile,
    build_execution_profile,
    is_mutating_role,
)
from howlplane.control_plane.resource_models import (
    AuthenticationStatus,
    BackendReadiness,
    ReadinessStatus,
)
from howlplane.control_plane.task_spec import TaskSpec, DataClassSerializationMixin

AGENT_EXECUTION_SCHEMA_VERSION = "howlplane.agent_execution/v1"

# Structural launch evidence (SLOPFIX-03). Failure classification must be able
# to tell "the provider executable is missing" apart from "the provider ran and
# something inside its session failed". Only the backend that spawns the process
# knows which happened, so it stamps the outcome here instead of leaving the
# distinction to be guessed from transcript text.
LAUNCH_OUTCOME_KEY = "launch_outcome"
# The executable was absent before any spawn was attempted.
LAUNCH_OUTCOME_NOT_INSTALLED = "not_installed"
# A spawn was attempted and the OS refused it (ENOENT, EACCES, ENOTDIR).
LAUNCH_OUTCOME_SPAWN_FAILED = "spawn_failed"
# The process started. Anything it printed is session output, not launch evidence.
LAUNCH_OUTCOME_LAUNCHED = "launched"

# Where a timeout verdict came from. "harness" means this process enforced the
# deadline and killed the provider, which is structural fact. "transcript" means
# the verdict was inferred from provider output and is only as good as that text.
# "budget" means the provider self-terminated at a deadline derived directly
# from the harness's timeout_seconds (e.g. agy's --print-timeout), preserving
# its diagnostic transcript without penalizing reachability as an outage.
TIMEOUT_SOURCE_KEY = "timeout_source"
TIMEOUT_SOURCE_HARNESS = "harness"
TIMEOUT_SOURCE_TRANSCRIPT = "transcript"
TIMEOUT_SOURCE_BUDGET = "budget"
BUDGET_DERIVED_KEY = "budget_derived"

# Whether the provider was denied a tool the task required. A provider that
# exits 0 after reporting "I need approval before I can edit" has not
# implemented anything, and must not be recorded as a successful
# implementation. Like the launch and timeout markers above, this is stamped by
# the backend that owns the invocation rather than inferred downstream.
TOOL_PERMISSION_KEY = "tool_permission_outcome"
TOOL_PERMISSION_DENIED = "denied"

# Exact provider errors from structured result envelopes are terminal evidence.
# Keeping this separate from the transcript lets classification distinguish a
# current provider stop from similar words quoted during ordinary reasoning.
TERMINAL_PROVIDER_ERROR_KEY = "terminal_provider_error"
WATCHDOG_TERMINATION_KEY = "watchdog_termination"
WATCHDOG_TERMINATION_EXHAUSTION = "terminal_exhaustion"
WATCHDOG_TERMINATION_STALL = "stall"
PROVIDER_RETRY_AFTER_SECONDS_KEY = "provider_retry_after_seconds"

# These expressions intentionally match a complete provider control line, not
# arbitrary model prose.  The streamed watchdog only reads CLI stdout/stderr,
# never task files or a provider's generated answer as a semantic signal.
_TRUSTED_TERMINAL_CONTROL_LINES = (
    re.compile(r"(?:error:\s*)?(?:you(?:'|’)ve hit your )?session limit(?: reached)?(?:\s*(?:[·.]\s*)?(?:resets?|retry\s+(?:in|after))\s+(?:.+|<elapsed>))?[.!]?", re.I),
    re.compile(r"(?:error:\s*)?(?:usage limit reached|quota (?:exceeded|exhausted)|credits exhausted|resource exhausted|capacity unavailable|provider unavailable|account limit reached|rate limit(?: exceeded)?|rate limited)[.!?]?", re.I),
    re.compile(r"(?:error:\s*)?(?:authentication required|not authenticated|login required|authorization required|credentials? (?:missing|expired))[.!]?", re.I),
)
_TRUSTED_ERROR_KIND = re.compile(
    r'"[A-Za-z0-9_.\-/]*error[_ ]?kind"\s*:\s*"(?:resource_exhausted|quota_exhausted|rate_limited|rate_limit_exceeded|session_limit|unauthenticated|unavailable|service_unavailable)"', re.I,
)
_RETRY_AFTER_SECONDS = re.compile(r"\bretry[- ]after\s*[:=]?\s*(\d+)\s*(?:seconds?|s)?\b", re.I)

_TIMEOUT_MARKERS = (
    "error: timeout waiting for response",
    "timeout waiting for response",
    "timed out",
    "request timed out",
    "connection timed out",
    "gateway timeout",
    "operation timed out",
    "deadline exceeded",
    "timeout after",
)


def trusted_terminal_provider_signal(stdout: str, stderr: str) -> Optional[Dict[str, Any]]:
    """Return only deterministic terminal provider-control evidence.

    Complete stderr lines and machine error envelopes are provider control
    surfaces.  stdout is accepted only for a complete exact control line,
    which supports terminal-oriented adapters without treating ordinary prose
    as authority.  A partial or embedded phrase is deliberately ignored.
    """
    # PTY applications redraw a control screen with cursor escapes and a
    # changing timer.  Collapse only terminal presentation noise before the
    # narrow control-line match; raw output remains retained by the caller.
    combined = normalize_terminal_text("\n".join((stderr or "", stdout or "")))
    retry = _RETRY_AFTER_SECONDS.search(combined)
    retry_payload = (
        {PROVIDER_RETRY_AFTER_SECONDS_KEY: int(retry.group(1))}
        if retry else {}
    )
    if _TRUSTED_ERROR_KIND.search(combined):
        return {"reason": WATCHDOG_TERMINATION_EXHAUSTION, **retry_payload}
    for line in combined.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized and any(pattern.fullmatch(normalized) for pattern in _TRUSTED_TERMINAL_CONTROL_LINES):
            result: Dict[str, Any] = {"reason": WATCHDOG_TERMINATION_EXHAUSTION}
            result.update(retry_payload)
            return result
    return None


_ANSI = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])")
_SPINNER = re.compile(r"[⠁⠂⠄⡀⢀⠠⠐⠈⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]")
_ELAPSED = re.compile(r"\b(?:\d{1,2}:){1,2}\d{2}\b")
# Deterministic engineering-progress markers. Each pattern is anchored to the
# start of a complete normalized control line from a provider CLI -- tool and
# patch records, Codex `exec` records and their results, and test, lint, or
# build summaries -- so ordinary prose ("tooling", "a checkpoint later",
# "2 failed requests") never matches. A match is progress, not completion;
# unrecognized output is only liveness.
_TOOL_EVENT = re.compile(
    r"^(?:"
    r"tool (?:invocation|call|result|completion)\b"
    r"|(?:writing|wrote|edited|updated|created|modified) (?:file|source)\b"
    r"|\*\*\* (?:add|update|delete) file: "
    r"|apply[_ ]patch\b"
    r"|exec$"
    r"|succeeded in \d+ ?m?s\b"
    r"|exited -?\d+ in \d+"
    r"|running (?:tests?|build)\b"
    r"|(?:=+ )?\d+ (?:passed|failed)(?:,| in |$| =)"
    r"|tests? +\d+ (?:passed|failed)\b"
    r"|all checks passed!?$"
    r"|build (?:succeeded|failed|completed?)\b"
    r")",
    re.I,
)
# Counters, percentages, byte counts, and hashes change on every redraw of a
# stuck screen.  Collapsing digits before deduplication stops a ticking
# heartbeat from registering as genuinely new output.
_DIGITS = re.compile(r"\d+")


def normalize_terminal_text(text: str) -> str:
    """Reduce redraw and countdown noise to a stable terminal representation."""
    reduced = _ANSI.sub("", text or "").replace("\b", "").replace("\r", "\n")
    reduced = _SPINNER.sub("", reduced)
    reduced = _ELAPSED.sub("<elapsed>", reduced)
    return "\n".join(" ".join(line.split()) for line in reduced.splitlines() if line.strip())


def process_tree_cpu_seconds(pid: int) -> Optional[float]:
    """Total CPU seconds consumed by a process and its descendants, or None.

    Linux-only (`/proc`).  Reaped-children time (cutime/cstime) is included so
    a finished test or build subprocess still counts.  Returns None when the
    measurement is unavailable, which callers must treat as "unknown", never as
    activity.
    """
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return None
    total, pending, seen = 0.0, [pid], set()
    measured = False
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            total += sum(int(value) for value in fields[11:15]) / ticks
            measured = True
            tasks = list(Path(f"/proc/{current}/task").iterdir())
        except (OSError, ValueError, IndexError):
            continue
        for task in tasks:
            try:
                pending.extend(int(child) for child in (task / "children").read_text().split())
            except (OSError, ValueError):
                continue
    return total if measured else None


LIVENESS_OUTPUT_ACTIVE = "OUTPUT ACTIVE"
LIVENESS_SOURCE_CHANGED = "SOURCE CHANGED"
LIVENESS_POST_CHANGE_VALIDATION = "POST-CHANGE VALIDATION"
LIVENESS_NO_OUTPUT = "NO OUTPUT"
LIVENESS_NO_MEANINGFUL_PROGRESS = "NO MEANINGFUL PROGRESS"
LIVENESS_HARD_BUDGET = "HARD BUDGET"
STALL_THRESHOLD_SILENCE = "silence"
STALL_THRESHOLD_NO_PROGRESS = "no_progress"
STALL_THRESHOLD_POST_CHANGE_GRACE = "post_change_grace"


class ProviderStreamObserver:
    """Separates provider liveness from engineering progress.

    Liveness (is the provider responsive?) comes from genuinely new output and
    measured process-tree CPU.  Progress (is work advancing?) comes from
    repository deltas and recognized tool/test/build events.  A provider is
    stalled when it shows no liveness for `stall_timeout_seconds` (silence), or
    no progress for `no_progress_timeout_seconds`, extended to
    `post_change_grace_seconds` after a source change so post-edit validation
    is not mistaken for a hang.  Neither threshold can extend the hard
    execution budget, which the backend enforces before asking this observer.
    """

    #: CPU seconds the process tree must accrue to count as one unit of liveness.
    CPU_LIVENESS_SECONDS = 2.0
    #: ...within this trailing window: a sustained rate of about 3.3%, above an
    #: idle event loop or spinner, below any real tool, build, or test run.
    CPU_WINDOW_SECONDS = 60.0
    #: `capture_delta` runs git and reads new files; bound how often.
    FINGERPRINT_INTERVAL_SECONDS = 2.0
    _SEEN_LIMIT = 512

    def __init__(self, provider_id: str, stall_timeout_seconds: float,
                 repository_fingerprint: Callable[[], str], clock: Callable[[], float] = time.monotonic,
                 *, no_progress_timeout_seconds: Optional[float] = None,
                 post_change_grace_seconds: Optional[float] = None,
                 hard_budget_seconds: Optional[float] = None,
                 cpu_reader: Callable[[int], Optional[float]] = process_tree_cpu_seconds) -> None:
        self.provider_id, self.repository_fingerprint, self.clock = provider_id, repository_fingerprint, clock
        self.stall_timeout_seconds = float(stall_timeout_seconds)
        self.no_progress_timeout_seconds = float(
            no_progress_timeout_seconds if no_progress_timeout_seconds is not None
            else max(self.stall_timeout_seconds, 3 * self.stall_timeout_seconds)
        )
        self.post_change_grace_seconds = float(
            post_change_grace_seconds if post_change_grace_seconds is not None
            else self.no_progress_timeout_seconds
        )
        self.hard_budget_seconds = hard_budget_seconds
        self.cpu_reader = cpu_reader
        now = clock()
        self.started_at = self.last_observed_at = now
        self.last_novel_output_at: Optional[float] = None
        self.last_process_activity_at: Optional[float] = None
        self.last_progress_at = now
        self.last_source_change_at: Optional[float] = None
        self.last_progress_event = "provider_started"
        self._pid: Optional[int] = None
        self._cpu_mark: Optional[float] = None
        self._cpu_mark_at = now
        self._cpu_measurable = False
        self._stdout_length = self._stderr_length = 0
        self._repository_state = self._safe_fingerprint()
        self._fingerprint_checked_at = now
        self._output_seen: set = set()
        self._output_order: Deque[str] = deque()
        self._progress_seen: set = set()
        self._progress_order: Deque[str] = deque()
        self._terminal_buffer: Deque[str] = deque(maxlen=32)
        self._previous_shape = ""

    # Legacy attribute names retained for readers of earlier evidence.
    @property
    def last_meaningful_progress_at(self) -> float:
        return self.last_progress_at

    @property
    def last_semantic_event(self) -> str:
        return self.last_progress_event

    def bind_process(self, pid: int) -> None:
        """Attach the launched provider pid so process-tree CPU can be measured."""
        self._pid = pid
        self._cpu_mark, self._cpu_mark_at = self.cpu_reader(pid), self.clock()
        self._cpu_measurable = self._cpu_mark is not None

    def _safe_fingerprint(self) -> Optional[str]:
        try:
            return self.repository_fingerprint()
        except Exception:
            # An unreadable tree is not evidence of change in either direction.
            return None

    def _remember(self, key: str, seen: set, order: Deque[str]) -> bool:
        if key in seen:
            return False
        seen.add(key)
        order.append(key)
        if len(order) > self._SEEN_LIMIT:
            seen.discard(order.popleft())
        return True

    def _last_liveness_at(self) -> float:
        return max(t for t in (self.started_at, self.last_novel_output_at,
                               self.last_process_activity_at, self.last_progress_at) if t is not None)

    def _progress_limit(self) -> tuple:
        if self.last_source_change_at is not None and self.last_progress_at == self.last_source_change_at:
            if self.post_change_grace_seconds > self.no_progress_timeout_seconds:
                return self.post_change_grace_seconds, STALL_THRESHOLD_POST_CHANGE_GRACE
        return self.no_progress_timeout_seconds, STALL_THRESHOLD_NO_PROGRESS

    def _in_post_change_grace(self, now: float) -> bool:
        return (self.last_source_change_at is not None
                and now - self.last_source_change_at < self.post_change_grace_seconds)

    def liveness_state(self, now: Optional[float] = None) -> str:
        """One operator-facing label for the current provider condition."""
        now = self.clock() if now is None else now
        if self.hard_budget_seconds is not None and now - self.started_at >= self.hard_budget_seconds:
            return LIVENESS_HARD_BUDGET
        if now - self.last_progress_at >= self.stall_timeout_seconds:
            if self._in_post_change_grace(now):
                return LIVENESS_POST_CHANGE_VALIDATION
            if now - self._last_liveness_at() >= self.stall_timeout_seconds:
                return LIVENESS_NO_OUTPUT
            return LIVENESS_NO_MEANINGFUL_PROGRESS
        if self.last_progress_event == "source_delta_changed":
            return LIVENESS_SOURCE_CHANGED
        if self._in_post_change_grace(now):
            return LIVENESS_POST_CHANGE_VALIDATION
        if self.last_novel_output_at is not None and now - self.last_novel_output_at < self.stall_timeout_seconds:
            return LIVENESS_OUTPUT_ACTIVE
        return LIVENESS_NO_OUTPUT

    def _diagnostics(self, now: float, threshold: Optional[str] = None,
                     threshold_seconds: Optional[float] = None) -> Dict[str, Any]:
        def since(t: Optional[float]) -> Optional[float]:
            return None if t is None else round(now - t, 3)
        return {
            "elapsed_seconds": round(now - self.started_at, 3),
            "hard_budget_seconds": self.hard_budget_seconds,
            "seconds_since_novel_output": since(self.last_novel_output_at),
            "seconds_since_process_activity": (
                since(self.last_process_activity_at) if self._cpu_measurable else "unavailable"
            ),
            "seconds_since_progress": since(self.last_progress_at),
            "seconds_since_source_change": since(self.last_source_change_at),
            "last_progress_event": self.last_progress_event,
            "source_changed": self.last_source_change_at is not None,
            "threshold_fired": threshold,
            "threshold_seconds": threshold_seconds,
            "silence_timeout_seconds": self.stall_timeout_seconds,
            "no_progress_timeout_seconds": self.no_progress_timeout_seconds,
            "post_change_grace_seconds": self.post_change_grace_seconds,
            "liveness_state": self.liveness_state(now),
            # Earlier keys, kept so older readers of attempt evidence still work.
            "last_observed_at": self.last_observed_at,
            "last_output_activity_at": self.last_novel_output_at,
            "last_meaningful_progress_at": self.last_progress_at,
            "semantic_stall_age_seconds": round(now - self.last_progress_at, 3),
            "last_semantic_event": self.last_progress_event,
            "raw_output_activity": (
                self.last_novel_output_at is not None and self.last_novel_output_at > self.last_progress_at
            ),
        }

    def _observe_output(self, now: float, new_text: str) -> Optional[Dict[str, Any]]:
        normalized = normalize_terminal_text(new_text)
        if not normalized:
            return None
        self._terminal_buffer.extend(normalized.splitlines())
        terminal = trusted_terminal_provider_signal("\n".join(self._terminal_buffer), "")
        if terminal is not None:
            terminal["diagnostics"] = self._diagnostics(now)
            return terminal
        for line in normalized.splitlines():
            shape = _DIGITS.sub("#", line)[:200]
            if self._remember(shape, self._output_seen, self._output_order):
                self.last_novel_output_at = now
            if _TOOL_EVENT.search(line):
                # Progress identity ignores counters but keeps the preceding
                # line: a rerun of a different command is new progress, while
                # a loop reprinting the same record ("attempt N") is not.
                key = f"{self._previous_shape}\n{shape}"
                if self._remember(key, self._progress_seen, self._progress_order):
                    self.last_progress_at = now
                    self.last_progress_event = f"{self.provider_id}_semantic_output:{line[:160]}"
            self._previous_shape = shape
        return None

    def _observe_process(self, now: float) -> None:
        if self._pid is None:
            return
        cpu = self.cpu_reader(self._pid)
        if cpu is None:
            return
        self._cpu_measurable = True
        if self._cpu_mark is None or cpu < self._cpu_mark:
            self._cpu_mark, self._cpu_mark_at = cpu, now
        elif cpu - self._cpu_mark >= self.CPU_LIVENESS_SECONDS:
            self._cpu_mark, self._cpu_mark_at, self.last_process_activity_at = cpu, now, now
        elif now - self._cpu_mark_at >= self.CPU_WINDOW_SECONDS:
            # Idle trickle (an event loop, a spinner) never accrues a full CPU
            # second inside the window, so it never counts as liveness.
            self._cpu_mark, self._cpu_mark_at = cpu, now

    def _observe_repository(self, now: float, force: bool = False) -> None:
        if not force and now - self._fingerprint_checked_at < self.FINGERPRINT_INTERVAL_SECONDS:
            return
        self._fingerprint_checked_at = now
        state = self._safe_fingerprint()
        if state is not None and state != self._repository_state:
            self._repository_state = state
            self.last_progress_at = self.last_source_change_at = now
            self.last_progress_event = "source_delta_changed"

    def __call__(self, stdout: str, stderr: str, _elapsed: float) -> Optional[Dict[str, Any]]:
        now = self.clock()
        self.last_observed_at = now
        new_stdout, new_stderr = stdout[self._stdout_length:], stderr[self._stderr_length:]
        self._stdout_length, self._stderr_length = len(stdout), len(stderr)
        if new_stdout or new_stderr:
            terminal = self._observe_output(now, new_stdout + "\n" + new_stderr)
            if terminal is not None:
                return terminal
        self._observe_process(now)
        self._observe_repository(now)
        if self._stall_verdict(now) is None:
            return None
        # Never terminate on a stale view of the tree: an edit made since the
        # last throttled fingerprint is progress and must be seen first.
        self._observe_repository(now, force=True)
        return self._stall_verdict(now)

    def _stall_verdict(self, now: float) -> Optional[Dict[str, Any]]:
        silence_age = now - self._last_liveness_at()
        if silence_age >= self.stall_timeout_seconds and not self._in_post_change_grace(now):
            return {"reason": WATCHDOG_TERMINATION_STALL,
                    "diagnostics": self._diagnostics(now, STALL_THRESHOLD_SILENCE, self.stall_timeout_seconds)}
        limit, threshold = self._progress_limit()
        if now - self.last_progress_at >= limit:
            return {"reason": WATCHDOG_TERMINATION_STALL,
                    "diagnostics": self._diagnostics(now, threshold, limit)}
        return None


def _read_ready_pipes(selector: "selectors.BaseSelector", output: Dict[str, bytearray], timeout: float) -> bool:
    """Append whatever the provider's pipes have ready; unregister closed ones.

    Returns False when nothing was ready within `timeout`.
    """
    ready = selector.select(timeout=timeout)
    for key, _ in ready:
        data = os.read(key.fileobj.fileno(), 65536)
        if data:
            output[key.data].extend(data)
        else:
            selector.unregister(key.fileobj)
    return bool(ready)


_PROCESS_GROUPS_SUPPORTED = hasattr(os, "killpg") and hasattr(os, "getpgid")


def _terminate_process_group(process: "subprocess.Popen", grace_seconds: float = 2.0) -> None:
    """Stop a provider and every process in its group: TERM, then KILL.

    Safe to call repeatedly and after the leader has exited, when surviving
    descendants may still hold the group. Falls back to the leader alone where
    process groups are unavailable.
    """
    import signal

    def signal_group(sig: int) -> bool:
        try:
            os.killpg(process.pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    if not _PROCESS_GROUPS_SUPPORTED:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return
    if not signal_group(signal.SIGTERM):
        if process.poll() is None:
            process.kill()
        process.wait()
        return
    deadline = time.time() + grace_seconds
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    # Descendants get the remainder of the grace period, then KILL.
    while time.time() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, OSError):
            break
        time.sleep(0.05)
    signal_group(signal.SIGKILL)
    process.wait()

# Fallback phrases for a provider that reports an approval or trust block in
# prose without populating a structured denial record. Deliberately narrow:
# this can only ever demote a claimed success, never manufacture one.
_PERMISSION_BLOCK_MARKERS = (
    "requires approval",
    "require approval",
    "requires permission",
    "needs approval",
    "permission denied by user",
    "not permitted to use",
    "approve bash execution",
    "workspace trust required",
    "trust the current workspace",
    "pass --trust",
)


def _parse_claude_result_envelope(stdout: str) -> Optional[Dict[str, Any]]:
    """Returns the `--output-format json` result object, or None if absent."""
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("type") == "result":
        return parsed
    return None


def _reports_permission_block(text: Optional[str]) -> bool:
    """Returns True when transcript text states a tool was blocked on approval."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PERMISSION_BLOCK_MARKERS)


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+\S+"),
    re.compile(r"(?i)(--?(?:password|token|api[-_]?key|secret|auth)[= ])\S+"),
    re.compile(r"(?i)\b([A-Za-z0-9_]*(?:token|secret|passwd|password|api[-_]?key)[A-Za-z0-9_]*=)\S+"),
    re.compile(r"\b(sk-|ghp_|gho_|github_pat_)[A-Za-z0-9_\-]{8,}"),
)

# A denied command is evidence, and evidence is written to the ledger and shown
# to operators. A refused command can carry a credential in its arguments, so
# it is redacted and bounded before it is persisted anywhere.
_MAX_RECORDED_COMMAND_CHARS = 200


def _redact_command(command: str) -> str:
    """Removes credential-shaped arguments from a command before recording it."""
    redacted = command
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: f"{m.group(1)}<redacted>" if m.lastindex else "<redacted>",
            redacted,
        )
    if len(redacted) > _MAX_RECORDED_COMMAND_CHARS:
        redacted = redacted[:_MAX_RECORDED_COMMAND_CHARS] + "...(truncated)"
    return redacted


def _denied_command(entry: Dict[str, Any]) -> Optional[str]:
    """Extracts the shell command a Bash permission denial actually refused.

    A denial records only `tool_name`, so a refused `gofmt -l .` and a refused
    `rm -rf /` both reduce to "Bash". In HOWLFRAM-SLOPFIX-07 that left the
    denied command recoverable only from the agent's prose, which is not
    evidence a later reader can rely on. The command is part of the denial, so
    it is recorded with it.
    """
    tool_input = entry.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return _redact_command(command.strip())
    return None

# Local Ollama defaults (milestone #58). Intentionally conservative: a single
# 7B-instruct model, a bounded 8K context window, and one inference at a time
# on modest consumer hardware. See documentation/LOCAL_MODEL.md.
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_DEFAULT_CONTEXT_LENGTH = 8192
OLLAMA_DEFAULT_MIN_AVAILABLE_RAM_GIB = 8.0
OLLAMA_DEFAULT_HOST = "http://127.0.0.1:11434"
OLLAMA_DEFAULT_TIMEOUT_SECONDS = 300
# Matches Ollama's own implicit default when the field is omitted; sent
# explicitly now that the payload carries a keep_alive key at all (#59
# Phase 17). Overnight-safe campaigns use 0 (unload immediately) instead.
OLLAMA_DEFAULT_KEEP_ALIVE: Union[int, str] = "5m"
# Conservative overnight-safe launch threshold (#59 Phase 16) -- separate
# from OLLAMA_DEFAULT_MIN_AVAILABLE_RAM_GIB, which remains the interactive
# default. Real target-machine evidence from #58 showed ~9.287 GiB before /
# ~8.817 GiB after a local task -- not a huge margin for an unattended
# desktop, hence the higher floor for unattended overnight operation.
OLLAMA_OVERNIGHT_MIN_AVAILABLE_RAM_GIB = 9.0


class OllamaAvailabilityReason:
    """Fine-grained local Ollama availability reasons (#58 Phase 2)."""

    NOT_INSTALLED = "OLLAMA_NOT_INSTALLED"
    SERVICE_UNAVAILABLE = "OLLAMA_SERVICE_UNAVAILABLE"
    MODEL_NOT_INSTALLED = "MODEL_NOT_INSTALLED"
    RESOURCE_CONSTRAINED = "RESOURCE_CONSTRAINED"
    AVAILABLE = "AVAILABLE"


def read_available_memory_gib(meminfo_path: Union[str, Path] = "/proc/meminfo") -> Optional[float]:
    """
    Reads currently available system memory from /proc/meminfo (Linux-native,
    no extra dependency). Returns None if unavailable (e.g. non-Linux host).
    """
    try:
        with open(meminfo_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 3)
    except Exception:
        return None
    return None


@dataclass
class OllamaDiagnostics:
    """Result of a local Ollama availability probe."""

    reason: str
    available: bool
    available_ram_gib: Optional[float] = None
    detail: str = ""


def _default_ollama_binary_present() -> bool:
    return shutil.which("ollama") is not None


def _default_ollama_service_health(host: str = OLLAMA_DEFAULT_HOST, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed local Ollama endpoint
            return 200 <= resp.status < 300
    except Exception:
        return False


def _default_ollama_model_installed(
    model: str = OLLAMA_DEFAULT_MODEL, host: str = OLLAMA_DEFAULT_HOST, timeout: float = 3.0
) -> Optional[bool]:
    """Returns True/False if determinable, or None if the service could not be queried."""
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed local Ollama endpoint
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    names = {m.get("name") for m in data.get("models", []) if isinstance(m, dict)}
    if model in names:
        return True
    base = model.split(":")[0]
    return any(n and n.split(":")[0] == base for n in names)


def diagnose_ollama(
    model: str = OLLAMA_DEFAULT_MODEL,
    min_available_ram_gib: float = OLLAMA_DEFAULT_MIN_AVAILABLE_RAM_GIB,
    host: str = OLLAMA_DEFAULT_HOST,
    binary_check: Callable[[], bool] = _default_ollama_binary_present,
    service_check: Callable[[str], bool] = _default_ollama_service_health,
    model_check: Callable[[str, str], Optional[bool]] = _default_ollama_model_installed,
    memory_reader: Callable[[], Optional[float]] = read_available_memory_gib,
) -> OllamaDiagnostics:
    """
    Deterministically diagnoses local Ollama availability without ever launching
    inference. Distinguishes "binary missing" from "service down" from "model not
    pulled" from "not enough RAM right now" (#58 Phase 2 / Phase 5).
    """
    if not binary_check():
        return OllamaDiagnostics(
            reason=OllamaAvailabilityReason.NOT_INSTALLED,
            available=False,
            detail="'ollama' binary not found on PATH.",
        )
    if not service_check(host):
        return OllamaDiagnostics(
            reason=OllamaAvailabilityReason.SERVICE_UNAVAILABLE,
            available=False,
            detail=f"Ollama service did not respond at {host}.",
        )
    installed = model_check(model, host)
    if installed is not True:
        return OllamaDiagnostics(
            reason=OllamaAvailabilityReason.MODEL_NOT_INSTALLED,
            available=False,
            detail=f"Model '{model}' is not pulled. Run: ollama pull {model}",
        )
    ram_gib = memory_reader()
    if ram_gib is not None and ram_gib < min_available_ram_gib:
        return OllamaDiagnostics(
            reason=OllamaAvailabilityReason.RESOURCE_CONSTRAINED,
            available=False,
            available_ram_gib=ram_gib,
            detail=f"Available RAM {ram_gib} GiB is below the required minimum {min_available_ram_gib} GiB.",
        )
    return OllamaDiagnostics(reason=OllamaAvailabilityReason.AVAILABLE, available=True, available_ram_gib=ram_gib)


class AgentExecutionError(RuntimeError):
    """Raised when an agent execution fails or encounters an unrecoverable error."""
    pass


class AgentUnavailableError(AgentExecutionError):
    """Raised when an explicitly requested agent backend is not installed or available."""
    pass


def _launch_metadata(
    launch_outcome: str,
    timeout_source: Optional[str] = None,
    budget_derived: bool = False,
) -> Dict[str, Any]:
    """Builds the structural execution markers classification relies on."""
    metadata: Dict[str, Any] = {LAUNCH_OUTCOME_KEY: launch_outcome}
    if timeout_source:
        metadata[TIMEOUT_SOURCE_KEY] = timeout_source
    if budget_derived:
        metadata[BUDGET_DERIVED_KEY] = True
    return metadata


@dataclass
class AgentExecutionResult(DataClassSerializationMixin):
    """Normalized result of an agent execution invocation."""

    agent_id: str
    role: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    success: bool
    timed_out: bool = False
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema: str = AGENT_EXECUTION_SCHEMA_VERSION


class AgentBackend(ABC):
    """Abstract base class for all agent execution backends."""

    def probe_readiness(self) -> BackendReadiness:
        """Checks safe runtime readiness without consuming generation capacity."""
        available = self.is_available()
        return BackendReadiness(
            status=(ReadinessStatus.READY if available else ReadinessStatus.UNAVAILABLE),
            installed=available,
            reachable=None,
            authentication=AuthenticationStatus.UNKNOWN,
            reason=None if available else "BACKEND_UNAVAILABLE",
        )

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def execute(
        self,
        task: TaskSpec,
        cwd: Union[str, Path],
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = 300,
        env_vars: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> AgentExecutionResult:
        pass


class SubprocessAgentBackend(AgentBackend):
    """Base backend for executing CLI agents via deterministic subprocess execution."""

    def __init__(
        self,
        agent_id: str,
        binary_name: str,
        cmd_builder: Optional[Callable[..., List[str]]] = None,
    ):
        self.agent_id = agent_id
        self.binary_name = binary_name
        self._builder = cmd_builder


    def _build_result(
        self,
        cmd_str: str,
        role: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        elapsed: float,
        *,
        timeout_source: Optional[str] = None,
    ) -> AgentExecutionResult:
        """Normalize a completed subprocess result into a provider execution record."""
        combined_err = f"{stderr}\n{stdout}".lower()
        is_timeout = any(marker in combined_err for marker in _TIMEOUT_MARKERS)
        error_message = None
        if exit_code != 0:
            error_message = (
                stderr.strip()
                if is_timeout and stderr.strip()
                else "Error: timeout waiting for response"
                if is_timeout
                else f"Process exited with code {exit_code}"
            )
        return AgentExecutionResult(
            agent_id=self.agent_id,
            role=role,
            command=cmd_str,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=elapsed,
            success=exit_code == 0,
            timed_out=is_timeout,
            error_message=error_message,
            metadata=_launch_metadata(
                LAUNCH_OUTCOME_LAUNCHED,
                timeout_source if is_timeout else None,
            ),
        )

    def _build_timeout_result(
        self,
        cmd_str: str,
        role: str,
        timeout_seconds: int,
        start_t: float,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> AgentExecutionResult:
        """Build a harness-timeout execution record from captured byte buffers."""
        return AgentExecutionResult(
            agent_id=self.agent_id,
            role=role,
            command=cmd_str,
            exit_code=-1,
            stdout=stdout.decode("utf-8", errors="replace") if stdout else "",
            stderr=stderr.decode("utf-8", errors="replace") + f"\nTimeout after {timeout_seconds}s." if stderr else f"Timeout after {timeout_seconds}s.",
            duration_seconds=round(time.time() - start_t, 3),
            success=False,
            timed_out=True,
            error_message=f"Timeout after {timeout_seconds}s",
            metadata=_launch_metadata(LAUNCH_OUTCOME_LAUNCHED, TIMEOUT_SOURCE_HARNESS),
        )

    def is_available(self) -> bool:
        return shutil.which(self.binary_name) is not None

    def probe_readiness(self) -> BackendReadiness:
        """Reports executable presence without contacting the hosted provider."""
        installed = self.is_available()
        native_support = self.agent_id in {"codex", "agy", "devin_cli"}
        return BackendReadiness(
            status=(
                ReadinessStatus.READY
                if installed
                else ReadinessStatus.MISSING_EXECUTABLE
            ),
            installed=installed,
            reachable=None,
            authentication=AuthenticationStatus.UNKNOWN,
            reason=None if installed else "MISSING_EXECUTABLE",
            evidence=f"executable:{self.binary_name}",
            unattended_mutation_capable=installed if native_support else (False if installed and self.agent_id == "gemini_cli" else None),
            capability_reason="native_role_support" if native_support and installed else (
                "headless non-interactive mutation unsupported" if self.agent_id == "gemini_cli" and installed else (
                    "MISSING_EXECUTABLE" if not installed else None
                )
            ),
        )

    def build_command(
        self,
        task: TaskSpec,
        cwd: Path,
        role: str,
        prompt: str,
        timeout_seconds: int = 300,
    ) -> List[str]:
        if self._builder:
            sig = inspect.signature(self._builder)
            params = sig.parameters
            if "timeout_seconds" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                return self._builder(
                    task, cwd, role, prompt, timeout_seconds=timeout_seconds
                )
            return self._builder(task, cwd, role, prompt)
        return [self.binary_name, prompt]

    def execute(
        self,
        task: TaskSpec,
        cwd: Union[str, Path],
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = 300,
        env_vars: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> AgentExecutionResult:
        target_cwd = Path(cwd).resolve()
        if not self.is_available():
            return AgentExecutionResult(
                agent_id=self.agent_id,
                role=role,
                command=self.binary_name,
                exit_code=127,
                stdout="",
                stderr=f"Agent binary '{self.binary_name}' is not installed or available on PATH.",
                duration_seconds=0.0,
                success=False,
                error_message=f"Agent '{self.agent_id}' unavailable",
                metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_NOT_INSTALLED},
            )

        prompt = prompt_override or f"Execute task {task.task_id}: {task.objective}"
        cmd_args = self.build_command(
            task, target_cwd, role, prompt, timeout_seconds=timeout_seconds
        )
        model_id = kwargs.get("model_id")
        if model_id and model_id != "UNKNOWN" and self.agent_id in {
            "claude_code", "codex", "cursor", "agy", "devin_cli"
        }:
            cmd_args[1:1] = ["--model", model_id]
        # Workspace trust policy is resolved in one place and applied here, for
        # every caller. Only an audited vendor flag is ever added (see
        # workspace_trust.ADAPTERS); nothing answers a prompt, and stdin stays closed.
        invocation_policy = kwargs.get("workspace_trust_policy") or workspace_trust.resolve_policy()["policy"]
        cmd_args += workspace_trust.invocation_argv(self.agent_id, invocation_policy)
        result = self._launch(cmd_args, target_cwd, role, timeout_seconds, env_vars, **kwargs)
        if result.metadata is None:
            result.metadata = {}
        result.metadata["workspace_trust"] = {
            "policy": invocation_policy, "mechanism": workspace_trust.mechanism(self.agent_id, invocation_policy)}
        return result

    def _launch(
        self,
        cmd_args: List[str],
        target_cwd: Path,
        role: str,
        timeout_seconds: int,
        env_vars: Optional[Dict[str, str]],
        **kwargs,
    ) -> AgentExecutionResult:
        cmd_str = _redact_command(" ".join(shlex.quote(c) for c in cmd_args))

        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        start_t = time.time()
        watchdog_callback = kwargs.get("watchdog_callback")
        watchdog_interval = max(0.05, float(kwargs.get("watchdog_interval_seconds", 0.25)))
        # Preserve the established blocking path for callers that do not opt
        # into observation (including adapter-level structured-envelope tests).
        # Factory implementation attempts pass a watchdog callback below.
        if watchdog_callback is None:
            try:
                # stdin is closed: an unattended CLI must never wait on a prompt
                # (workspace trust, permissions) typed at the operator's terminal.
                completed = subprocess.run(
                    args=cmd_args, cwd=str(target_cwd), capture_output=True,
                    text=True, env=env, timeout=timeout_seconds, stdin=subprocess.DEVNULL,
                )
                elapsed = round(time.time() - start_t, 3)
                return self._build_result(
                    cmd_str, role, completed.returncode,
                    completed.stdout, completed.stderr, elapsed,
                    timeout_source=TIMEOUT_SOURCE_TRANSCRIPT,
                )
            except subprocess.TimeoutExpired as exc:
                return self._build_timeout_result(
                    cmd_str, role, timeout_seconds, start_t,
                    exc.stdout, exc.stderr,
                )
        try:
            # `communicate(timeout=...)` cannot expose a terminal quota screen
            # until the process exits.  Polling pipes lets a trusted watchdog
            # terminate a known-dead session promptly while retaining exactly
            # the output used to make that decision.
            # The provider leads its own process group so termination reaches
            # every descendant it spawned (test watchers, dev servers, builds).
            # A survivor could otherwise keep writing to the repository after
            # rollback and contaminate the next provider's attempt.
            process = subprocess.Popen(
                args=cmd_args, cwd=str(target_cwd), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, stdin=subprocess.DEVNULL,
                start_new_session=_PROCESS_GROUPS_SUPPORTED,
            )
            selector = selectors.DefaultSelector()
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            output = {"stdout": bytearray(), "stderr": bytearray()}
            watchdog_result: Optional[Dict[str, Any]] = None
            budget_exceeded = False
            bind_process = getattr(watchdog_callback, "bind_process", None)
            try:
                if callable(bind_process):
                    bind_process(process.pid)
                while process.poll() is None:
                    _read_ready_pipes(selector, output, watchdog_interval)
                    # The hard budget is the delegated authority ceiling. It is
                    # checked before any liveness verdict, so an overrun is
                    # classified as a budget timeout rather than a stall and no
                    # liveness evidence can extend it.
                    if time.time() - start_t >= timeout_seconds:
                        if process.poll() is not None:
                            break  # it finished on its own at the deadline
                        # A terminal provider screen that arrived with the
                        # deadline is still the more specific truth: a quota
                        # must not be recorded as a short transient timeout.
                        terminal = trusted_terminal_provider_signal(
                            output["stdout"][-8192:].decode("utf-8", errors="replace"),
                            output["stderr"][-8192:].decode("utf-8", errors="replace"),
                        )
                        _terminate_process_group(process)
                        if terminal is not None:
                            watchdog_result = terminal
                        else:
                            budget_exceeded = True
                        break
                    if watchdog_callback is not None:
                        observed = watchdog_callback(
                            output["stdout"].decode("utf-8", errors="replace"),
                            output["stderr"].decode("utf-8", errors="replace"),
                            time.time() - start_t,
                        )
                        if observed:
                            # A provider that exited while the verdict was
                            # being formed finished on its own; its real exit
                            # status wins over a termination it never received.
                            if process.poll() is not None:
                                break
                            watchdog_result = dict(observed)
                            _terminate_process_group(process)
                            break
            finally:
                # Normal exit, verdict, budget, or an exception in this loop
                # (including an operator interrupt): no descendant outlives
                # the attempt.
                _terminate_process_group(process)
            # Drain residual bytes after normal or watchdog termination. Read
            # until each pipe is exhausted, not one chunk, so a provider that
            # exits with more than one buffer of output keeps all of it.
            for _ in range(4096):
                if not _read_ready_pipes(selector, output, 0):
                    break
            if budget_exceeded:
                selector.close()
                raise subprocess.TimeoutExpired(cmd_args, timeout_seconds,
                                                output=bytes(output["stdout"]),
                                                stderr=bytes(output["stderr"]))
            selector.close()
            completed_stdout = output["stdout"].decode("utf-8", errors="replace")
            completed_stderr = output["stderr"].decode("utf-8", errors="replace")
            elapsed = round(time.time() - start_t, 3)
            if watchdog_result is not None:
                metadata = _launch_metadata(LAUNCH_OUTCOME_LAUNCHED)
                metadata[WATCHDOG_TERMINATION_KEY] = watchdog_result["reason"]
                if PROVIDER_RETRY_AFTER_SECONDS_KEY in watchdog_result:
                    metadata[PROVIDER_RETRY_AFTER_SECONDS_KEY] = watchdog_result[PROVIDER_RETRY_AFTER_SECONDS_KEY]
                if isinstance(watchdog_result.get("diagnostics"), dict):
                    metadata["watchdog_diagnostics"] = watchdog_result["diagnostics"]
                return AgentExecutionResult(
                    agent_id=self.agent_id, role=role, command=cmd_str,
                    exit_code=-1, stdout=completed_stdout, stderr=completed_stderr,
                    duration_seconds=elapsed, success=False,
                    error_message=f"Provider watchdog terminated attempt: {watchdog_result['reason']}",
                    metadata=metadata,
                )
            return self._build_result(
                cmd_str, role, process.returncode,
                completed_stdout, completed_stderr, elapsed,
                timeout_source=TIMEOUT_SOURCE_TRANSCRIPT,
            )
        except subprocess.TimeoutExpired as exc:
            return self._build_timeout_result(
                cmd_str, role, timeout_seconds, start_t,
                exc.stdout, exc.stderr,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            # The OS refused the spawn, so the executable really is missing or
            # unusable. This is the only in-flight error that means that.
            dur = round(time.time() - start_t, 3)
            return AgentExecutionResult(
                agent_id=self.agent_id,
                role=role,
                command=cmd_str,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_seconds=dur,
                success=False,
                error_message=str(exc),
                metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_SPAWN_FAILED},
            )
        except Exception as exc:
            dur = round(time.time() - start_t, 3)
            return AgentExecutionResult(
                agent_id=self.agent_id,
                role=role,
                command=cmd_str,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_seconds=dur,
                success=False,
                error_message=str(exc),
            )


# Built-in specialized CLI agent backend instances
class ClaudeCodeBackend(SubprocessAgentBackend):
    """Headless Claude Code with bounded, per-invocation tool permissions.

    The invocation used to be a bare `claude -p <prompt>`. In the
    HOWLFRAM-SLOPFIX-04 canary that run diagnosed the defect correctly, reported
    that every Bash attempt "requires approval", produced no repository delta,
    and exited 0 -- which was recorded as a successful implementation.

    Permissions are passed as command-line flags for one invocation only. No
    global Claude Code configuration is written, so interactive sessions are
    unaffected. `--dangerously-skip-permissions` and
    `--permission-mode bypassPermissions` are never emitted.
    """

    def __init__(self, operator_settings: Optional[Any] = None):
        super().__init__("claude_code", "claude")
        self.operator_settings = operator_settings

    def build_execution_profile(
        self,
        task: Optional[TaskSpec] = None,
        cwd: Optional[Union[str, Path]] = None,
        role: str = "implementation",
    ) -> ProviderExecutionProfile:
        """Derives this invocation's permissions from the target project."""
        project_context = None
        verification_plan = None
        if cwd is not None:
            try:
                from howlplane.control_plane.project_adapter import ProjectAdapter

                project_context = ProjectAdapter.discover(Path(cwd))
                verification_plan = ProjectAdapter.create_verification_plan(
                    project_context, getattr(task, "task_id", "unknown") if task else "unknown"
                )
            except Exception:
                # Permission derivation must never block execution; without a
                # discovered project the profile degrades to tools plus read-only
                # git, which is bounded rather than broadened.
                pass
        return build_execution_profile(
            role=role,
            task=task,
            project_context=project_context,
            verification_plan=verification_plan,
            operator_settings=self.operator_settings,
        )

    def probe_readiness(self) -> BackendReadiness:
        readiness = super().probe_readiness()
        if not readiness.installed:
            readiness.unattended_mutation_capable = False
            readiness.capability_reason = "MISSING_EXECUTABLE"
            return readiness
        profile = self.build_execution_profile(None, None, "implementation")
        readiness.unattended_mutation_capable = profile.mutation_capable
        readiness.capability_reason = (
            profile.permission_mode
            if profile.mutation_capable
            else "Mutation tools disallowed by configuration"
        )
        return readiness

    def build_command(
        self,
        task: TaskSpec,
        cwd: Path,
        role: str,
        prompt: str,
        timeout_seconds: int = 300,
        **kwargs,
    ) -> List[str]:
        profile = self.build_execution_profile(task, cwd, role)
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        allowed = profile.allowed_tools()
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if profile.disallowed_tools:
            cmd += ["--disallowedTools", *profile.disallowed_tools]
        if profile.permission_mode:
            cmd += ["--permission-mode", profile.permission_mode]
        return cmd

    def execute(self, task: TaskSpec, cwd, role: str = "implementation", **kwargs):
        """Runs Claude Code and interprets its structured result envelope.

        `--output-format json` makes stdout a result object rather than prose.
        The human-readable `result` is restored as stdout so downstream evidence
        and classification see what the agent actually said, and any reported
        `permission_denials` are recorded structurally.
        """
        result = super().execute(task=task, cwd=cwd, role=role, **kwargs)
        envelope = _parse_claude_result_envelope(result.stdout)
        if envelope is None:
            if _reports_permission_block(result.stdout):
                result.metadata[TOOL_PERMISSION_KEY] = TOOL_PERMISSION_DENIED
                result.metadata["denied_tools"] = ["unreported"]
                if result.success:
                    result.success = False
                    result.error_message = (
                        "Required tool permissions were unavailable: unreported"
                    )
            return result

        text = envelope.get("result")
        if isinstance(text, str) and text:
            result.stdout = text
        denials = envelope.get("permission_denials") or []
        result.metadata["claude_session_id"] = envelope.get("session_id")
        result.metadata["claude_subtype"] = envelope.get("subtype")
        if isinstance(envelope.get("is_error"), bool):
            result.metadata["claude_is_error"] = envelope["is_error"]
        if envelope.get("is_error") is True:
            result.success = False
            if isinstance(text, str) and text.strip():
                result.metadata[TERMINAL_PROVIDER_ERROR_KEY] = text.strip()
                result.error_message = text.strip()
            elif not result.error_message:
                result.error_message = "Claude Code reported a terminal error"

        denied_tools = sorted(
            {
                str(entry.get("tool_name") or entry.get("tool") or "unknown")
                for entry in denials
                if isinstance(entry, dict)
            }
        )
        denied_commands = sorted(
            {
                command
                for entry in denials
                if isinstance(entry, dict)
                for command in (_denied_command(entry),)
                if command
            }
        )

        if not denied_tools and _reports_permission_block(text):
            # The CLI did not populate `permission_denials`, but the agent
            # explicitly said it was blocked on approval. Reported honestly as
            # a permission failure rather than a silent empty success.
            denied_tools = ["unreported"]

        if denied_tools:
            result.metadata[TOOL_PERMISSION_KEY] = TOOL_PERMISSION_DENIED
            result.metadata["denied_tools"] = denied_tools
            if denied_commands:
                result.metadata["denied_commands"] = denied_commands
            if result.success:
                result.success = False
                # Name the command when one is known: "Bash" alone does not say
                # whether the bound was wrong or the request was illegitimate.
                detail = ", ".join(denied_commands or denied_tools)
                result.error_message = (
                    "Required tool permissions were unavailable: " + detail
                )
        return result


class CodexBackend(SubprocessAgentBackend):
    def __init__(self):
        def _codex_cmd(t, c, r, p, timeout_seconds: int = 300, **kwargs):
            # Codex defaults to a read-only sandbox; implementation and
            # remediation roles must be able to edit files in the workspace.
            # Review roles keep the default read-only sandbox.
            if r and (r.endswith("-reviewer") or r in ("review", "planning", "acceptance") or r.startswith("writing:")):
                return ["codex", "exec", "--skip-git-repo-check", p]
            return [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                p,
            ]
        super().__init__("codex", "codex", _codex_cmd)


class GeminiCLIBackend(SubprocessAgentBackend):
    def __init__(self):
        def _gemini_cmd(t, c, r, p, timeout_seconds: int = 300, **kwargs):
            return ["gemini", "-p", p]
        super().__init__("gemini_cli", "gemini", _gemini_cmd)


# Wall-clock headroom between agy's own print deadline and the harness kill.
# Handing agy the harness budget verbatim races the two: agy needs time after
# its deadline to emit its timeout message and exit, so subprocess.TimeoutExpired
# normally wins and the run is classified EXECUTION_BUDGET_EXCEEDED (harness)
# instead of by transport (transcript), nondeterministically and with agy's own
# diagnostic output discarded by the kill.
#
# To satisfy both intents (preserving agy's diagnostic transcript without
# misclassifying a budget timeout as a transport failure that benches a healthy
# provider), AgyBackend tags timeouts attributable to this derived print-timeout
# as budget-derived (EXECUTION_BUDGET_EXCEEDED). Genuine provider-reported
# transport errors (e.g. gateway timeout, connection refused) remain classified
# as TRANSPORT_UNAVAILABLE.
AGY_PRINT_TIMEOUT_HEADROOM_SECONDS = 15


class AgyBackend(SubprocessAgentBackend):
    def __init__(self):
        def _agy_cmd(t, c, r, p, timeout_seconds: int = 300, **kwargs):
            print_timeout = max(1, timeout_seconds - AGY_PRINT_TIMEOUT_HEADROOM_SECONDS)
            return [
                "agy",
                "-p", p,
                "--mode", "plan" if r in ("planning", "review", "acceptance") else "accept-edits",
                "--print-timeout", f"{print_timeout}s",
            ]
        super().__init__("agy", "agy", _agy_cmd)

    def execute(
        self,
        task: TaskSpec,
        cwd: Union[str, Path],
        role: str = "implementation",
        timeout_seconds: int = 300,
        env_vars: Optional[Dict[str, str]] = None,
        prompt_override: Optional[str] = None,
        **kwargs,
    ) -> AgentExecutionResult:
        result = super().execute(
            task=task,
            cwd=cwd,
            role=role,
            timeout_seconds=timeout_seconds,
            env_vars=env_vars,
            prompt_override=prompt_override,
            **kwargs,
        )
        # agy can exit zero while its turn is still running. That acknowledges
        # delivery of partial output, not completion of engineering work. Keep
        # the actual exit code, but send this result through the existing
        # timeout salvage/failover path instead of starting review/remediation.
        partial_timeout = next((line.strip() for line in result.stderr.splitlines()
                                if re.fullmatch(
                                    r"\[agy\] print timeout after (?:\d+(?:\.\d+)?(?:ms|h|m|s))+"
                                    r" with turn in progress; returning partial output",
                                    line.strip(),
                                )), None)
        if partial_timeout:
            result.success = False
            result.timed_out = True
            result.error_message = partial_timeout
        if result.timed_out and result.metadata.get(TIMEOUT_SOURCE_KEY) != TIMEOUT_SOURCE_HARNESS:
            combined = f"{result.stderr}\n{result.stdout}".lower()
            genuine_transport_markers = (
                "connection refused",
                "transport unavailable",
                "network unreachable",
                "gateway timeout",
                "read timeout",
                "connection timed out",
                "connection reset",
            )
            if not any(marker in combined for marker in genuine_transport_markers):
                if result.metadata is None:
                    result.metadata = {}
                result.metadata[TIMEOUT_SOURCE_KEY] = TIMEOUT_SOURCE_BUDGET
                result.metadata[BUDGET_DERIVED_KEY] = True
        return result


class DevinCLIBackend(SubprocessAgentBackend):
    def __init__(self):
        def _devin_cmd(t, c, r, p, timeout_seconds: int = 300, **kwargs):
            return ["devin", "-p", p, "--permission-mode", "auto" if r in ("planning", "review", "acceptance") else "accept-edits"]
        super().__init__("devin_cli", "devin", _devin_cmd)


class CursorBackend(SubprocessAgentBackend):
    """Cursor Agent CLI non-interactive adapter.

    Uses the installed ``agent`` executable in print mode, which is Cursor's
    supported non-interactive automation interface. The ``agent`` binary is
    distinct from the IDE launcher (``cursor``) and does not require the IDE.
    Planning, review, and acceptance roles use ``--mode plan`` so they stay
    read-only; implementation uses the default editing mode.
    """

    def __init__(self):
        def _cursor_cmd(task, cwd, role, prompt, **kwargs):
            command = ["agent", "-p", prompt, "--output-format", "text", "--workspace", str(cwd)]
            if role in ("planning", "review", "acceptance"):
                command += ["--mode", "plan"]
            return command
        super().__init__("cursor", "agent", _cursor_cmd)


class OllamaLocalBackend(AgentBackend):
    """
    Real local inference backend for the canonical Tier-3 local model
    (qwen2.5-coder:7b-instruct) via Ollama's HTTP API (#58 Phase 4).

    Truthful attribution: this backend only reports `implementing_provider =
    local_ollama` when actual local inference executed. Unavailability,
    resource constraints, and concurrency contention are returned as
    distinct, non-exhaustion failure classifications (see provider_pool.py's
    `detect_exhaustion` special-casing of local providers).
    """

    agent_id = "local_ollama"

    def __init__(
        self,
        model: str = OLLAMA_DEFAULT_MODEL,
        context_length: int = OLLAMA_DEFAULT_CONTEXT_LENGTH,
        min_available_ram_gib: float = OLLAMA_DEFAULT_MIN_AVAILABLE_RAM_GIB,
        host: str = OLLAMA_DEFAULT_HOST,
        repo_root: Union[str, Path] = ".",
        diagnostics_fn: Optional[Callable[[], "OllamaDiagnostics"]] = None,
        http_generate: Optional[Callable[[Dict[str, Any], float], Dict[str, Any]]] = None,
        memory_reader: Callable[[], Optional[float]] = read_available_memory_gib,
        keep_alive: Union[int, str] = OLLAMA_DEFAULT_KEEP_ALIVE,
    ):
        self.model = model
        self.context_length = context_length
        self.min_available_ram_gib = min_available_ram_gib
        self.host = host
        self.repo_root = Path(repo_root).resolve()
        self._memory_reader = memory_reader
        # Ollama's own implicit default keep_alive is "5m" when the field is
        # omitted entirely -- the prior implementation never sent this key
        # at all, so "5m" here preserves that behavior explicitly rather
        # than changing it. Overnight-safe campaigns construct a separate
        # instance with keep_alive=0 (unload immediately after each
        # inference, #59 Phase 17) rather than changing this default, which
        # would also affect normal interactive/local development use.
        self.keep_alive = keep_alive
        self._diagnostics_fn = diagnostics_fn or (
            lambda: diagnose_ollama(
                model=self.model,
                min_available_ram_gib=self.min_available_ram_gib,
                host=self.host,
                memory_reader=self._memory_reader,
            )
        )
        self._http_generate = http_generate or self._default_http_generate

    def diagnose(self) -> OllamaDiagnostics:
        return self._diagnostics_fn()

    def is_available(self) -> bool:
        return self.diagnose().available

    def probe_readiness(self) -> BackendReadiness:
        """Checks the local runtime, model tag, and memory without generation."""
        diag = self.diagnose()
        status_by_reason = {
            OllamaAvailabilityReason.NOT_INSTALLED: ReadinessStatus.MISSING_EXECUTABLE,
            OllamaAvailabilityReason.SERVICE_UNAVAILABLE: ReadinessStatus.UNREACHABLE,
            OllamaAvailabilityReason.MODEL_NOT_INSTALLED: ReadinessStatus.UNAVAILABLE,
            OllamaAvailabilityReason.RESOURCE_CONSTRAINED: ReadinessStatus.UNAVAILABLE,
            OllamaAvailabilityReason.AVAILABLE: ReadinessStatus.READY,
        }
        return BackendReadiness(
            status=status_by_reason.get(diag.reason, ReadinessStatus.UNKNOWN),
            installed=(diag.reason != OllamaAvailabilityReason.NOT_INSTALLED),
            reachable=diag.reason not in (
                OllamaAvailabilityReason.NOT_INSTALLED,
                OllamaAvailabilityReason.SERVICE_UNAVAILABLE,
            ),
            authentication=AuthenticationStatus.NOT_APPLICABLE,
            reason=None if diag.available else diag.reason,
            evidence="local_ollama_diagnostics",
        )

    def _default_http_generate(self, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed local Ollama endpoint
            return json.loads(resp.read().decode("utf-8"))

    def execute(
        self,
        task: TaskSpec,
        cwd: Union[str, Path],
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = OLLAMA_DEFAULT_TIMEOUT_SECONDS,
        env_vars: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> AgentExecutionResult:
        start_iso = datetime.now(timezone.utc).isoformat()
        task_id = task.task_id if task else "unknown"
        command_repr = f"ollama_local:{self.model}"

        # 1. Memory safety FIRST: never launch inference before confirming headroom.
        diag = self.diagnose()
        if not diag.available:
            return AgentExecutionResult(
                agent_id=self.agent_id, role=role, command=command_repr,
                exit_code=-2, stdout="", stderr=diag.detail,
                duration_seconds=0.0, success=False,
                error_message=diag.reason,
                metadata={
                    "provider": self.agent_id, "model": self.model, "task_id": task_id,
                    "availability_reason": diag.reason, "available_ram_gib": diag.available_ram_gib,
                    "start_time": start_iso,
                },
            )

        prompt = prompt_override or f"Task {task_id}: {task.objective if task else ''}"

        # 2. Context budget: curated prompts only, never silently widened (#58 Phase 8).
        approx_tokens = len(prompt) // 4
        if approx_tokens > self.context_length:
            return AgentExecutionResult(
                agent_id=self.agent_id, role=role, command=command_repr,
                exit_code=-3, stdout="", stderr=(
                    f"Prompt (~{approx_tokens} tokens) exceeds the local context budget "
                    f"({self.context_length} tokens)."
                ),
                duration_seconds=0.0, success=False,
                error_message="LOCAL_CONTEXT_INSUFFICIENT",
                metadata={"provider": self.agent_id, "model": self.model, "task_id": task_id, "start_time": start_iso},
            )

        # 3. Single-flight concurrency: one local inference at a time, machine-wide.
        lock = LocalInferenceLock(self.repo_root, task_id, command=command_repr)
        try:
            lock.acquire()
        except LockError as exc:
            return AgentExecutionResult(
                agent_id=self.agent_id, role=role, command=command_repr,
                exit_code=-4, stdout="", stderr=str(exc),
                duration_seconds=0.0, success=False,
                error_message="LOCAL_PROVIDER_BUSY",
                metadata={"provider": self.agent_id, "model": self.model, "task_id": task_id, "start_time": start_iso},
            )

        ram_before = self._memory_reader()
        t0 = time.time()
        try:
            try:
                result = self._http_generate(
                    {
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"num_ctx": self.context_length},
                        "keep_alive": self.keep_alive,
                    },
                    timeout_seconds,
                )
            except Exception as exc:
                elapsed = round(time.time() - t0, 3)
                ram_after = self._memory_reader()
                return AgentExecutionResult(
                    agent_id=self.agent_id, role=role, command=command_repr,
                    exit_code=-1, stdout="", stderr=str(exc),
                    duration_seconds=elapsed, success=False,
                    error_message="LOCAL_PROVIDER_UNAVAILABLE",
                    metadata={
                        "provider": self.agent_id, "model": self.model, "task_id": task_id,
                        "start_time": start_iso, "finish_time": datetime.now(timezone.utc).isoformat(),
                        "ram_before_gib": ram_before, "ram_after_gib": ram_after,
                    },
                )

            elapsed = round(time.time() - t0, 3)
            ram_after = self._memory_reader()
            # Best-effort: Ollama unloads asynchronously after the response,
            # so this measurement may lag the actual unload (#59 Phase 17).
            # Recorded regardless of keep_alive value so the two are
            # directly comparable in evidence; only meaningful when
            # keep_alive == 0 requested an immediate unload.
            ram_after_unload = self._memory_reader() if self.keep_alive == 0 else None
            output_text = result.get("response", "") if isinstance(result, dict) else ""
            done = bool(result.get("done", True)) if isinstance(result, dict) else False
            success = done and bool(output_text.strip())

            return AgentExecutionResult(
                agent_id=self.agent_id, role=role, command=command_repr,
                exit_code=0 if success else 1,
                stdout=output_text,
                stderr="" if success else "Local model returned an empty or incomplete response.",
                duration_seconds=elapsed, success=success,
                error_message=None if success else "ENGINEERING_FAILURE",
                metadata={
                    "provider": self.agent_id, "model": self.model, "task_id": task_id,
                    "task_class": getattr(task, "task_class", None) if task else None,
                    "start_time": start_iso, "finish_time": datetime.now(timezone.utc).isoformat(),
                    "context_length": self.context_length,
                    "keep_alive": self.keep_alive,
                    "ram_before_gib": ram_before, "ram_after_gib": ram_after,
                    "ram_after_unload_gib": ram_after_unload,
                    "output_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest() if output_text else None,
                },
            )
        finally:
            lock.release()


# Backward-compatible alias: earlier scaffolding referred to this class by this name.
LocalOllamaBackend = OllamaLocalBackend


class FakeAgentBackend(AgentBackend):
    """Deterministic programmable agent backend for automated tests and CI fixtures."""

    def __init__(
        self,
        agent_id: str = "fake_agent",
        default_exit_code: int = 0,
        default_stdout: str = "Fake implementation completed successfully. No issues found.",
        default_stderr: str = "",
        side_effect: Optional[Callable[[TaskSpec, Path, str], None]] = None,
        duration: float = 0.05,
        default_timed_out: bool = False,
        default_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.agent_id = agent_id
        self.default_exit_code = default_exit_code
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr
        self.side_effect = side_effect
        self.duration = duration
        self.default_timed_out = default_timed_out
        self.default_metadata = dict(default_metadata or {})
        self.executed_calls: List[Dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    def probe_readiness(self) -> BackendReadiness:
        return BackendReadiness(
            status=ReadinessStatus.READY,
            installed=True,
            reachable=True,
            authentication=AuthenticationStatus.UNKNOWN,
            evidence="deterministic_fake_backend",
        )

    def execute(
        self,
        task: TaskSpec,
        cwd: Union[str, Path],
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = 300,
        env_vars: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> AgentExecutionResult:
        target_cwd = Path(cwd).resolve()
        prompt = prompt_override or f"Fake execute: {task.objective}"
        self.executed_calls.append({
            "task_id": task.task_id if task else "unknown",
            "role": role,
            "prompt": prompt,
            "cwd": str(target_cwd),
        })

        if self.side_effect:
            try:
                self.side_effect(task, target_cwd, prompt)
            except Exception as exc:
                return AgentExecutionResult(
                    agent_id=self.agent_id,
                    role=role,
                    command=f"fake_agent({self.agent_id})",
                    exit_code=1,
                    stdout="",
                    stderr=f"Side effect error: {exc}",
                    duration_seconds=self.duration,
                    success=False,
                    error_message=str(exc),
                )

        ok = (self.default_exit_code == 0)
        return AgentExecutionResult(
            agent_id=self.agent_id,
            role=role,
            command=f"fake_agent({self.agent_id})",
            exit_code=self.default_exit_code,
            stdout=self.default_stdout,
            stderr=self.default_stderr,
            duration_seconds=self.duration,
            success=ok,
            timed_out=self.default_timed_out,
            error_message=None if ok else f"Exit code {self.default_exit_code}",
            metadata=dict(self.default_metadata),
        )


class AgentBackendRegistry:
    """Registry providing backend instances by agent_id."""

    _instances: Dict[str, AgentBackend] = {
        "claude_code": ClaudeCodeBackend(),
        "codex": CodexBackend(),
        "gemini_cli": GeminiCLIBackend(),
        "agy": AgyBackend(),
        "cursor": CursorBackend(),
        "devin_cli": DevinCLIBackend(),
        "local_ollama": OllamaLocalBackend(),
    }

    _ALIASES: Dict[str, str] = {
        "claude": "claude_code",
        "devin": "devin_cli",
        "gemini": "gemini_cli",
        "ollama": "local_ollama",
        "ollama_local": "local_ollama",
    }

    @classmethod
    def normalize_agent_id(cls, agent_id: str) -> str:
        return cls._ALIASES.get(agent_id, agent_id)

    @classmethod
    def get_backend(cls, agent_id: str, custom_backend: Optional[AgentBackend] = None) -> AgentBackend:
        if custom_backend is not None:
            return custom_backend
        canonical_id = cls.normalize_agent_id(agent_id)
        if canonical_id in cls._instances:
            return cls._instances[canonical_id]
        return SubprocessAgentBackend(agent_id=agent_id, binary_name=agent_id)

    @classmethod
    def register_backend(cls, agent_id: str, backend: AgentBackend) -> None:
        canonical_id = cls.normalize_agent_id(agent_id)
        cls._instances[canonical_id] = backend
