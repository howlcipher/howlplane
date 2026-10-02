"""`howlplane doctor --creative`: preflight for the Dream -> Writer -> Create pipeline.

Run 5 lost time to failures that only surfaced at runtime: a repository
virtualenv carrying a stale howl-provider-core (``ImportError: cannot import
name 'classify_failure'``), virtualenvs pinned to an interpreter that had
since been upgraded, and no way to tell which component builds were
installed. This module finds those problems before a workflow starts.

Every check is read-only. It never installs, upgrades, writes, or prints a
credential; command configurations are reported by executable name only.
Statuses are PASS, WARN and FAIL, and every non-PASS result carries a
concrete remediation.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib
import importlib.metadata as metadata
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

COMPONENTS = ("howl-provider-core", "howldream", "howlwriter", "howlcreate")
CLIS = {"howldream": "howldream", "howlwriter": "howlwriter", "howlcreate": "howlcreate"}
CONSUMERS_OF_CORE = ("howldream", "howlwriter", "howlcreate")
# Symbols the creative pipeline imports from each component at runtime.
REQUIRED_SYMBOLS: Dict[str, Dict[str, tuple]] = {
    "howl-provider-core": {
        "howl_provider_core": ("classify_failure", "CommandConfig", "CommandProvider", "Policy",
                               "ProviderError", "reported_metadata"),
    },
    "howlwriter": {
        "howlwriter.native.engine": ("write_copy", "amend"),
        "howlwriter.native.fidelity": ("compare",),
        "howlwriter.native.intake": ("request_from_dream",),
    },
    "howlcreate": {
        "howlcreate.engine.writer_intake": ("develop_from_writer",),
        "howlcreate.engine.materialize": ("materialize",),
        "howlcreate.engine.sandbox": ("SandboxRoot",),
    },
    "howldream": {
        "howldream.participation": ("cluster_external", "export_external"),
        "howldream.interop": ("export_candidate",),
    },
}
_PIN = re.compile(r"howl-provider-core\s*@\s*git\+\S+?@(?P<sha>[0-9a-f]{7,40})", re.I)
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


def _dist(name: str, lookup: Callable[[str], Any]) -> Any:
    try:
        return lookup(name)
    except metadata.PackageNotFoundError:
        return None


def _direct_url(dist: Any) -> Dict[str, Any]:
    try:
        return json.loads(dist.read_text("direct_url.json") or "{}")
    except (ValueError, TypeError):
        return {}


def _git_head(path: Path) -> Optional[str]:
    if not (path / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def installed_source(dist: Any) -> Dict[str, Any]:
    """Version plus where it came from: a VCS commit or an editable checkout."""
    info = _direct_url(dist)
    source: Dict[str, Any] = {"version": dist.version, "kind": "index", "commit": None, "path": None}
    if info.get("vcs_info"):
        source.update(kind="vcs", commit=info["vcs_info"].get("commit_id"))
    elif info.get("dir_info", {}).get("editable"):
        url = info.get("url", "")
        path = Path(url[7:]) if url.startswith("file://") else None
        source.update(kind="editable", path=str(path) if path else None,
                      commit=_git_head(path) if path else None)
    return source


def core_pin(dist: Any) -> Optional[str]:
    for requirement in dist.requires or []:
        match = _PIN.search(requirement)
        if match:
            return match.group("sha")
    return None


def _missing_symbols(component: str, importer: Callable[[str], Any]) -> List[str]:
    missing = []
    for module_name, symbols in REQUIRED_SYMBOLS[component].items():
        try:
            module = importer(module_name)
        except Exception as error:  # noqa: BLE001 - any import failure is the finding
            missing.append(f"{module_name} ({type(error).__name__}: {str(error)[:160]})")
            continue
        missing.extend(f"{module_name}.{s}" for s in symbols if not hasattr(module, s))
    return missing


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


def check_components(lookup: Callable[[str], Any], importer: Callable[[str], Any],
                     repos_root: Optional[Path]) -> List[Check]:
    checks: List[Check] = []
    sources: Dict[str, Dict[str, Any]] = {}
    for name in COMPONENTS:
        dist = _dist(name, lookup)
        if dist is None:
            checks.append(Check(f"package.{name}", f"{name} installed", FAIL, "Not installed",
                                f"pip install 'git+https://github.com/howlcipher/{name}.git@main'"))
            continue
        source = installed_source(dist)
        sources[name] = source
        where = source["commit"][:12] if source["commit"] else source["kind"]
        missing = _missing_symbols(name, importer)
        if missing:
            checks.append(Check(
                f"package.{name}", f"{name} installed", FAIL,
                f"{name} {source['version']} ({where}) lacks: {', '.join(missing[:4])}",
                f"Reinstall {name} from the merged main branch into this interpreter.",
                data=source))
        else:
            checks.append(Check(f"package.{name}", f"{name} installed", PASS,
                                f"{name} {source['version']} ({where})", data=source))
        if repos_root is not None and source["kind"] != "editable":
            head = _git_head(repos_root / name)
            if head and source["commit"] and head != source["commit"]:
                checks.append(Check(
                    f"repo.{name}", f"{name} checkout vs install", WARN,
                    f"Local checkout {head[:12]} differs from installed {source['commit'][:12]}",
                    f"pip install -e {repos_root / name}  (or update the checkout)"))

    core = sources.get("howl-provider-core")
    for consumer in CONSUMERS_OF_CORE:
        dist = _dist(consumer, lookup)
        if dist is None or core is None:
            continue
        pin = core_pin(dist)
        core_commit = core.get("commit")
        if pin is None:
            continue
        label = f"{consumer} -> howl-provider-core"
        if core_commit and core_commit.startswith(pin):
            checks.append(Check(f"compat.{consumer}.core", label, PASS, f"pin {pin[:12]} installed"))
        elif _missing_symbols("howl-provider-core", importer):
            checks.append(Check(
                f"compat.{consumer}.core", label, FAIL,
                f"{consumer} requires provider-core {pin[:12]}; installed "
                f"{(core_commit or core['kind'])[:12]} is missing required symbols",
                f"pip install 'git+https://github.com/howlcipher/howl-provider-core.git@{pin}'"))
        else:
            checks.append(Check(
                f"compat.{consumer}.core", label, WARN,
                f"{consumer} pins {pin[:12]}; installed {(core_commit or core['kind'])[:12]} "
                "differs but exports every required symbol",
                f"pip install 'git+https://github.com/howlcipher/howl-provider-core.git@{pin}'"))
    checks.append(check_core_pin_agreement(lookup))
    checks.append(check_writer_create_contract(importer))
    return checks


def check_core_pin_agreement(lookup: Callable[[str], Any]) -> Check:
    """Consumers pinning different provider-core commits cannot be co-installed.

    pip treats two direct-URL requirements for the same project as a conflict,
    so a fresh environment fails to resolve even when the pinned trees match.
    """
    label = "Shared provider-core pin"
    pins = {}
    for consumer in CONSUMERS_OF_CORE:
        dist = _dist(consumer, lookup)
        pin = core_pin(dist) if dist is not None else None
        if pin:
            pins[consumer] = pin
    if len(set(pins.values())) <= 1:
        detail = f"all consumers pin {next(iter(pins.values()))[:12]}" if pins else "no pinned consumers installed"
        return Check("compat.core_pin_agreement", label, PASS, detail)
    listing = ", ".join(f"{c}@{p[:12]}" for c, p in sorted(pins.items()))
    return Check("compat.core_pin_agreement", label, FAIL,
                 f"Consumers pin different howl-provider-core commits ({listing}); "
                 "a fresh install cannot resolve them together",
                 "Align every consumer's howl-provider-core pin to one commit and reinstall.")


def check_writer_create_contract(importer: Callable[[str], Any]) -> Check:
    """Create validates Writer packages against a vendored schema; both copies must agree."""
    label = "Writer -> Create contract"
    try:
        writer = importer("howlwriter.schemas")
        create = importer("howlcreate")
    except Exception:  # noqa: BLE001
        return Check("compat.writer.create", label, FAIL, "howlwriter or howlcreate is not importable",
                     "Install both components from merged main.")
    name = "howlwriter.copy_package.v1.schema.json"
    writer_schema = Path(writer.__file__).with_name(name)
    create_schema = Path(create.__file__).parent / "schemas" / name
    if not writer_schema.is_file() or not create_schema.is_file():
        missing = "howlwriter" if not writer_schema.is_file() else "howlcreate"
        return Check("compat.writer.create", label, FAIL, f"{missing} does not ship {name}",
                     f"Upgrade {missing} to a build with the native copy-package contract.")
    if json.loads(writer_schema.read_text()) != json.loads(create_schema.read_text()):
        return Check("compat.writer.create", label, FAIL,
                     "Writer and Create copy-package schemas differ",
                     "Install matching merged builds of howlwriter and howlcreate.")
    return Check("compat.writer.create", label, PASS, "copy_package/v1 schemas match")


def _shebang_target(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            first = handle.readline(512).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].split()
    return parts[0] if parts else None


def check_entrypoints(which: Callable[[str], Optional[str]]) -> List[Check]:
    checks = []
    for name, command in CLIS.items():
        path = which(command)
        if path is None:
            checks.append(Check(f"cli.{name}", f"{command} CLI", WARN, "Not on PATH",
                                f"Activate the environment that has {name}, or run python -m {name}.cli"))
            continue
        target = _shebang_target(path)
        if target and not target.endswith("/env") and not Path(target).exists():
            checks.append(Check(f"cli.{name}", f"{command} CLI", FAIL,
                                f"{path} points at missing interpreter {target}",
                                f"Reinstall {name} into a live environment; the entry point is stale."))
        else:
            checks.append(Check(f"cli.{name}", f"{command} CLI", PASS, path))
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
        return Check("provider.remote", "Remote provider configuration", FAIL,
                     "howl-provider-core is not importable", "Install howl-provider-core."), None
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
    repos_root: Optional[Path] = None,
    lookup: Callable[[str], Any] = metadata.distribution,
    importer: Callable[[str], Any] = importlib.import_module,
    which: Callable[[str], Optional[str]] = shutil.which,
    interpreter: Optional[tuple] = None,
) -> List[Check]:
    env = os.environ if env is None else env
    python, version, prefix, base = interpreter or (
        sys.executable, tuple(sys.version_info[:3]), sys.prefix, sys.base_prefix)
    checks = check_interpreter(python, version, prefix, base)
    checks += check_components(lookup, importer, repos_root)
    checks += check_entrypoints(which)
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
        repos_root=Path(args.repos_root).expanduser() if getattr(args, "repos_root", None) else None,
    ))
    print(json.dumps(report, indent=2) if getattr(args, "json", False) else render(report))
    return 1 if report["status"] == FAIL else 0
