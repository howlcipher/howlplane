"""`howlplane doctor --creative`: preflight for the Dream -> Writer -> Create pipeline.

Run 5 lost time to failures that only surfaced at runtime: a repository
virtualenv carrying a stale howl-provider-core (``ImportError: cannot import
name 'classify_failure'``), virtualenvs pinned to an interpreter that had
since been upgraded, and no way to tell which component builds were
installed. This module finds those problems before a workflow starts.

Each component runs as its own CLI, in whatever environment installed it
(DOG-032), so the components are checked by running them, not by importing
them into HowlPlane's interpreter.

Every check is read-only. It never installs, upgrades, writes, or prints a
credential; command configurations are reported by executable name only.
Statuses are PASS, WARN and FAIL, and every non-PASS result carries a
concrete remediation.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

SCHEMA = "howlplane.doctor.creative/v1"
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_PYTHON = (3, 11)

CLIS = {"howldream": "HowlDream", "howlwriter": "HowlWriter", "howlcreate": "HowlCreate"}
CLI_PROBE_TIMEOUT_SECONDS = 30
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "cmd.exe", "powershell", "pwsh"}
_LOCAL_ENV = ("OLLAMA_HOST", "OLLAMA_BASE_URL", "LLAMA_CPP_SERVER", "LMSTUDIO_BASE_URL",
              "LOCALAI_BASE_URL", "VLLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
_LOCAL_HOST = re.compile(r"(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|::1|\.local\b|\.localhost\b)", re.I)
_LOCAL_LAUNCHERS = {"ollama", "llama-cli", "llama-server", "vllm", "local-ai", "lms", "lm-studio"}


@dataclass
class Check:
    id: str
    label: str
    status: str
    detail: str
    fix: str = ""
    data: Optional[Dict[str, Any]] = None


def check_interpreter(python: str, version: tuple, prefix: str, base_prefix: str) -> List[Check]:
    checks = []
    shown = ".".join(str(v) for v in version[:3])
    if tuple(version[:2]) < MIN_PYTHON:
        checks.append(Check("python", "Python interpreter", FAIL, f"{python} is Python {shown}",
                            "Use Python 3.11 or newer for the Howl components."))
    else:
        checks.append(Check("python", "Python interpreter", PASS, f"{python} (Python {shown})"))
    if prefix == base_prefix:
        checks.append(Check("venv", "Virtual environment", WARN, "No virtual environment is active",
                            "Create one: python -m venv .venv && . .venv/bin/activate"))
        return checks
    cfg = Path(prefix) / "pyvenv.cfg"
    values: Dict[str, str] = {}
    if cfg.is_file():
        for line in cfg.read_text(errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    home = values.get("home")
    recorded = values.get("version_info") or values.get("version")
    problems = []
    if home and not Path(home).exists():
        problems.append(f"pyvenv.cfg home {home} no longer exists")
    recorded_minor = ".".join((recorded or "").split(".")[:2])
    if recorded and recorded_minor != ".".join(shown.split(".")[:2]):
        # site-packages is per minor version: a minor mismatch breaks imports.
        problems.append(f"venv was created for Python {recorded}, running {shown}")
    fix = f"Recreate it: python -m venv --clear {prefix} and reinstall the Howl components."
    if problems:
        checks.append(Check("venv", "Virtual environment", FAIL,
                            f"Stale virtualenv {prefix}: " + "; ".join(problems), fix))
    elif recorded and not shown.startswith(".".join(recorded.split(".")[:3])):
        checks.append(Check("venv", "Virtual environment", WARN,
                            f"{prefix} was created for Python {recorded}; running {shown} "
                            "(patch drift, imports still resolve)", fix))
    else:
        checks.append(Check("venv", "Virtual environment", PASS, f"{prefix}"))
    return checks


def _probe(argv: List[str], env: Mapping[str, str]) -> tuple:
    """Runs a component CLI read-only; returns (exit code, combined output)."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, env=dict(env),
                                timeout=CLI_PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, f"{type(error).__name__}: {error}"
    return result.returncode, (result.stdout + result.stderr).strip()


def check_component_clis(which: Callable[[str], Optional[str]],
                         probe: Callable[[List[str]], tuple]) -> List[Check]:
    """The pipeline runs each component's own CLI; prove each one starts in its own environment.

    A stale environment (run 5: a provider-core lacking ``classify_failure``)
    fails here as an ImportError from ``--version``, before any stage runs.
    """
    checks = []
    for command, display in CLIS.items():
        path = which(command)
        if path is None:
            checks.append(Check(f"cli.{command}", f"{display} CLI", FAIL, "Not on PATH",
                                f"Install {display} so `{command}` is on PATH; the pipeline runs each "
                                "component's own CLI."))
            continue
        code, output = probe([path, "--version"])
        if code != 0:
            last = output.splitlines()[-1] if output else f"exit {code}"
            checks.append(Check(f"cli.{command}", f"{display} CLI", FAIL,
                                f"`{command} --version` failed: {last[:200]}",
                                f"Repair {display}'s own environment (for example reinstall it); "
                                "HowlPlane runs it as installed."))
            continue
        version = output.splitlines()[0] if output else "version not reported"
        checks.append(Check(f"cli.{command}", f"{display} CLI", PASS, f"{path} ({version[:80]})"))
    return checks


def check_policy(env: Mapping[str, str]) -> Check:
    value = env.get("HOWL_FORBID_LOCAL_INFERENCE", "")
    if value.lower() in {"1", "true", "yes"}:
        return Check("policy.local", "HOWL_FORBID_LOCAL_INFERENCE", PASS, "Local inference is forbidden")
    return Check("policy.local", "HOWL_FORBID_LOCAL_INFERENCE", WARN,
                 f"Not set (value: {value or 'unset'}); local inference is only denied by default",
                 "export HOWL_FORBID_LOCAL_INFERENCE=1")


def check_local_providers(env: Mapping[str, str], config_argv: Optional[List[str]]) -> Check:
    forbid = env.get("HOWL_FORBID_LOCAL_INFERENCE", "").lower() in {"1", "true", "yes"}
    found = [name for name in _LOCAL_ENV if env.get(name) and (
        name.startswith(("OLLAMA", "LLAMA", "LMSTUDIO", "LOCALAI", "VLLM")) or _LOCAL_HOST.search(env[name]))]
    if config_argv and Path(config_argv[0]).name.lower() in _LOCAL_LAUNCHERS:
        found.append(f"command config launcher {Path(config_argv[0]).name}")
    if not found:
        return Check("policy.local_config", "Local model configuration", PASS, "None detected")
    status = WARN if forbid else FAIL
    return Check("policy.local_config", "Local model configuration", status,
                 "Local inference configuration present: " + ", ".join(found)
                 + (" (blocked by HOWL_FORBID_LOCAL_INFERENCE)" if forbid else ""),
                 "Unset these variables or remove the local launcher; keep HOWL_FORBID_LOCAL_INFERENCE=1.")


def check_command_config(path: Optional[Path]) -> tuple:
    if path is None:
        return Check("provider.remote", "Remote provider configuration", WARN,
                     "No --command-config supplied; Dream/Writer/Create model stages cannot run",
                     "Pass --command-config with a reviewed remote CLI profile."), None
    try:
        from howl_provider_core import CommandConfig, ProviderError
    except ImportError:
        # Components carry provider-core in their own environments (DOG-032); HowlWriter
        # validates the profile again before its call. Check the shape here.
        return _check_command_config_shape(path)
    try:
        config = CommandConfig.read(path)
    except (ProviderError, OSError, TypeError, ValueError) as error:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} is invalid: {str(error)[:200]}",
                     "Fix the profile: explicit argv, remote: true, no shell or local launcher."), None
    executable = Path(config.argv[0]).name
    if shutil.which(config.argv[0]) is None:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} parses, but executable {executable} is not available",
                     f"Install or put {executable} on PATH."), list(config.argv)
    return Check("provider.remote", "Remote provider configuration", PASS,
                 f"{path} parses; remote command {executable}, adapter {config.adapter or config.output_format}"), \
        list(config.argv)


def _check_command_config_shape(path: Path) -> tuple:
    fix = "Fix the profile: explicit argv list, remote: true, no shell or local launcher."
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} is unreadable or not JSON: {type(error).__name__}", fix), None
    argv = value.get("argv") if isinstance(value, dict) else None
    # Arguments may be empty strings (`--tools ""` disables a CLI's tools); the executable may not.
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) and argv[0]):
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} needs an argv list of strings starting with an executable", fix), None
    executable = Path(argv[0]).name
    if value.get("remote") is not True:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} does not declare remote: true", fix), None
    if executable.lower() in _SHELLS or executable.lower() in _LOCAL_LAUNCHERS:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} launches {executable}, a shell or local model launcher", fix), list(argv)
    if shutil.which(argv[0]) is None:
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     f"{path} parses, but executable {executable} is not available",
                     f"Install or put {executable} on PATH."), list(argv)
    return Check("provider.remote", "Remote provider configuration", PASS,
                 f"{path} parses; remote command {executable} (HowlWriter re-validates it before its call)"), \
        list(argv)


def check_workspace(path: Path) -> Check:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    if os.access(target, os.W_OK | os.X_OK):
        return Check("workspace", "Workspace permissions", PASS, f"{path} is writable")
    return Check("workspace", "Workspace permissions", FAIL, f"{path} is not writable",
                 "Choose a writable --workspace directory.")


def run_checks(
    *,
    env: Optional[Mapping[str, str]] = None,
    command_config: Optional[Path] = None,
    workspace: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    probe: Optional[Callable[[List[str]], tuple]] = None,
    interpreter: Optional[tuple] = None,
) -> List[Check]:
    env = os.environ if env is None else env
    python, version, prefix, base = interpreter or (
        sys.executable, tuple(sys.version_info[:3]), sys.prefix, sys.base_prefix)
    probe_env = {**env, "HOWL_FORBID_LOCAL_INFERENCE": "1"}
    checks = check_interpreter(python, version, prefix, base)
    checks += check_component_clis(which, probe or (lambda argv: _probe(argv, probe_env)))
    config_check, argv = check_command_config(command_config)
    checks.append(config_check)
    checks.append(check_policy(env))
    checks.append(check_local_providers(env, argv))
    checks.append(check_workspace(workspace or Path.cwd()))
    return checks


def summarize(checks: Iterable[Check]) -> Dict[str, Any]:
    rows = [asdict(c) for c in checks]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, WARN, FAIL)}
    overall = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    return {"schema": SCHEMA, "status": overall, "counts": counts, "checks": rows}


def render(report: Dict[str, Any]) -> str:
    lines = [f"HowlPlane creative preflight: {report['status']}",
             f"{report['counts'][PASS]} PASS, {report['counts'][WARN]} WARN, {report['counts'][FAIL]} FAIL", ""]
    for row in report["checks"]:
        lines.append(f"{row['status']:<4}  {row['label']}: {row['detail']}")
        if row["status"] != PASS and row["fix"]:
            lines.append(f"      Suggested action: {row['fix']}")
    return "\n".join(lines)


def command(args: argparse.Namespace) -> int:
    report = summarize(run_checks(
        command_config=Path(args.command_config).expanduser() if getattr(args, "command_config", None) else None,
        workspace=Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None,
    ))
    print(json.dumps(report, indent=2) if getattr(args, "json", False) else render(report))
    return 1 if report["status"] == FAIL else 0
