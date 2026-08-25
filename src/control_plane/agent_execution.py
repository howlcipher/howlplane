#!/usr/bin/env python3
"""
agent_execution.py

Normalized execution abstraction for AI agents across implementation,
remediation, and review roles. Supports CLI backends, local execution,
and deterministic mock/fake backends for testing and CI.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib, json, os, shlex, shutil, subprocess, time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from src.control_plane.locking import LocalInferenceLock, LockError
from src.control_plane.resource_models import (
    AuthenticationStatus,
    BackendReadiness,
    ReadinessStatus,
)
from src.control_plane.task_spec import TaskSpec, DataClassSerializationMixin

AGENT_EXECUTION_SCHEMA_VERSION = "howlplane.agent_execution/v1"

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
        cmd_builder: Optional[Callable[[TaskSpec, Path, str, str], List[str]]] = None,
    ):
        self.agent_id = agent_id
        self.binary_name = binary_name
        self._builder = cmd_builder

    def is_available(self) -> bool:
        return shutil.which(self.binary_name) is not None

    def probe_readiness(self) -> BackendReadiness:
        """Reports executable presence without contacting the hosted provider."""
        installed = self.is_available()
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
        )

    def build_command(self, task: TaskSpec, cwd: Path, role: str, prompt: str) -> List[str]:
        if self._builder:
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
            )

        prompt = prompt_override or f"Execute task {task.task_id}: {task.objective}"
        cmd_args = self.build_command(task, target_cwd, role, prompt)
        cmd_str = " ".join(shlex.quote(c) for c in cmd_args)

        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        start_t = time.time()
        try:
            completed = subprocess.run(
                args=cmd_args,
                cwd=str(target_cwd),
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_seconds,
            )
            elapsed = round(time.time() - start_t, 3)
            return AgentExecutionResult(
                agent_id=self.agent_id,
                role=role,
                command=cmd_str,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=elapsed,
                success=(completed.returncode == 0),
                error_message=None if completed.returncode == 0 else f"Process exited with code {completed.returncode}",
            )
        except subprocess.TimeoutExpired as exc:
            dur = round(time.time() - start_t, 3)
            out = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            err = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            return AgentExecutionResult(
                agent_id=self.agent_id,
                role=role,
                command=cmd_str,
                exit_code=-1,
                stdout=out,
                stderr=err + f"\nTimeout after {timeout_seconds}s.",
                duration_seconds=dur,
                success=False,
                timed_out=True,
                error_message=f"Timeout after {timeout_seconds}s",
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
    def __init__(self):
        super().__init__("claude_code", "claude", lambda t, c, r, p: ["claude", "-p", p])


class CodexBackend(SubprocessAgentBackend):
    def __init__(self):
        def _codex_cmd(t, c, r, p):
            # Codex defaults to a read-only sandbox; implementation and
            # remediation roles must be able to edit files in the workspace.
            # Review roles keep the default read-only sandbox.
            if r and (r.endswith("-reviewer") or r == "review"):
                return ["codex", "exec", p]
            return ["codex", "exec", "--sandbox", "workspace-write", p]
        super().__init__("codex", "codex", _codex_cmd)


class GeminiCLIBackend(SubprocessAgentBackend):
    def __init__(self):
        super().__init__("gemini_cli", "gemini", lambda t, c, r, p: ["gemini", "-p", p])


class AgyBackend(SubprocessAgentBackend):
    def __init__(self):
        super().__init__("agy", "agy", lambda t, c, r, p: ["agy", "-p", p, "--mode", "accept-edits"])


class DevinCLIBackend(SubprocessAgentBackend):
    def __init__(self):
        def _devin_cmd(t, c, r, p):
            return ["devin", "-p", p, "--permission-mode", "accept-edits"]
        super().__init__("devin_cli", "devin", _devin_cmd)


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
    ):
        self.agent_id = agent_id
        self.default_exit_code = default_exit_code
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr
        self.side_effect = side_effect
        self.duration = duration
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
            error_message=None if ok else f"Exit code {self.default_exit_code}",
        )


class AgentBackendRegistry:
    """Registry providing backend instances by agent_id."""

    _instances: Dict[str, AgentBackend] = {
        "claude_code": ClaudeCodeBackend(),
        "codex": CodexBackend(),
        "gemini_cli": GeminiCLIBackend(),
        "agy": AgyBackend(),
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
