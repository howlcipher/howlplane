#!/usr/bin/env python3
"""
doctor.py

System environment, dependency, and toolchain health diagnostics for HowlPlane.
Checks:
- Python interpreter & active virtualenv
- Essential Python package dependencies (pytest, yaml, jsonschema)
- Go toolchain availability (go binary and version)
- Git repository status and hooks
- Control plane evidence ledger integrity
"""

from dataclasses import dataclass
import getpass
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


@dataclass
class DiagnosticCheck:
    name: str
    status: str  # "ok", "warning", "error"
    message: str
    details: Optional[Dict[str, str]] = None


def check_python_environment() -> DiagnosticCheck:
    venv = os.environ.get("VIRTUAL_ENV")
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if venv:
        return DiagnosticCheck(
            name="Python Environment",
            status="ok",
            message=f"Running in virtual environment: {venv} (Python {py_ver})",
        )
    return DiagnosticCheck(
        name="Python Environment",
        status="ok",
        message=f"Note: no virtualenv is active (Python {py_ver}); one is optional and does not block anything.",
    )


def check_dependencies() -> DiagnosticCheck:
    required = ["yaml", "jsonschema"]
    missing = []
    for pkg in required:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        return DiagnosticCheck(
            name="Python Dependencies",
            status="error",
            message=f"Missing required dependencies: {', '.join(missing)}",
        )
    return DiagnosticCheck(
        name="Python Dependencies",
        status="ok",
        message="All required dependencies (pytest, yaml, jsonschema) are importable.",
    )


def check_go_toolchain() -> DiagnosticCheck:
    go_path = shutil.which("go")
    if not go_path:
        return DiagnosticCheck(
            name="Go Toolchain",
            status="warning",
            message="Go compiler ('go') not found in PATH.",
        )
    try:
        res = subprocess.run([go_path, "version"], capture_output=True, text=True, check=True)
        return DiagnosticCheck(
            name="Go Toolchain",
            status="ok",
            message=f"Go toolchain detected: {res.stdout.strip()}",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="Go Toolchain",
            status="warning",
            message=f"Go binary found at {go_path} but failed to query version: {exc}",
        )


def check_git_status(repo_root: Path) -> DiagnosticCheck:
    if not shutil.which("git"):
        return DiagnosticCheck(
            name="Git Repository",
            status="error",
            message="Git is not installed (no 'git' on PATH), so no repository can be used.",
            details={"action": "Install Git, then re-run 'howlplane doctor'."},
        )
    # In a linked worktree or submodule .git is a file, not a directory.
    if not (repo_root / ".git").exists():
        return DiagnosticCheck(
            name="Git Repository",
            status="warning",
            message=f"Directory {repo_root} is not a git repository.",
            details={"action": f"git -C {repo_root} init"},
        )
    return DiagnosticCheck(
        name="Git Repository",
        status="ok",
        message=f"Valid git repository confirmed at {repo_root}.",
    )


def _hooks_dir(repo_root: Path) -> Path:
    """Hooks directory; follows a worktree or submodule .git file through git itself."""
    default = repo_root / ".git" / "hooks"
    if (repo_root / ".git").is_dir() or not shutil.which("git"):
        return default
    try:
        res = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
                             capture_output=True, text=True, check=False, timeout=10.0)
        out = res.stdout.strip()
        if res.returncode == 0 and out:
            path = Path(out)
            return path if path.is_absolute() else repo_root / path
    except Exception:
        pass
    return default


def check_git_hooks(repo_root: Path) -> DiagnosticCheck:
    hooks_dir = _hooks_dir(repo_root)
    if not hooks_dir.is_dir():
        return DiagnosticCheck(
            name="Git Hooks",
            status="warning",
            message=f"Git hooks directory {hooks_dir} not found.",
            details={"action": "python3 scripts/install_pre_commit_hook.py && python3 scripts/install_pre_push_hook.py"},
        )
    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"
    missing = []
    if not pre_commit.exists():
        missing.append("pre-commit")
    elif not os.access(pre_commit, os.X_OK):
        missing.append("pre-commit (not executable)")

    if not pre_push.exists():
        missing.append("pre-push")
    elif not os.access(pre_push, os.X_OK):
        missing.append("pre-push (not executable)")

    if missing:
        return DiagnosticCheck(
            name="Git Hooks",
            status="warning",
            message=f"Git hooks missing or not executable: {', '.join(missing)}.",
            details={"action": "python3 scripts/install_pre_commit_hook.py && python3 scripts/install_pre_push_hook.py"},
        )
    return DiagnosticCheck(
        name="Git Hooks",
        status="ok",
        message="Required Git hooks ('pre-commit', 'pre-push') are installed and executable.",
    )


def check_slopslint() -> DiagnosticCheck:
    slop_bin = shutil.which("slopslint")
    if not slop_bin:
        return DiagnosticCheck(
            name="SlopsLint Binary",
            status="warning",
            message="SlopsLint binary 'slopslint' not found on PATH.",
            details={"action": "bash scripts/install_slopslint.sh"},
        )
    try:
        res = subprocess.run(
            ["slopslint", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        if res.returncode == 0:
            ver_line = res.stdout.strip()
            if "0.1.0" in ver_line:
                return DiagnosticCheck(
                    name="SlopsLint Binary",
                    status="ok",
                    message=f"SlopsLint v0.1.0 verified ({ver_line}).",
                )
            return DiagnosticCheck(
                name="SlopsLint Binary",
                status="warning",
                message=f"SlopsLint version mismatch: got '{ver_line}', expected '0.1.0'.",
                details={"action": "bash scripts/install_slopslint.sh"},
            )
        return DiagnosticCheck(
            name="SlopsLint Binary",
            status="warning",
            message=f"SlopsLint returned non-zero exit code: {res.returncode}.",
            details={"action": "bash scripts/install_slopslint.sh"},
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="SlopsLint Binary",
            status="warning",
            message=f"Error checking slopslint: {exc}.",
            details={"action": "bash scripts/install_slopslint.sh"},
        )


def check_control_plane_ledger(repo_root: Path) -> DiagnosticCheck:
    ledger_file = repo_root / "logs" / "control_plane" / "evidence_ledger.jsonl"
    if not ledger_file.exists():
        return DiagnosticCheck(
            name="Evidence Ledger",
            status="ok",
            message="No evidence ledger log file exists yet (clean state).",
        )
    try:
        from howlplane.control_plane.evidence_ledger import EvidenceLedger

        entries, diagnostics = EvidenceLedger(str(ledger_file)).read_entries()
        if diagnostics:
            first = diagnostics[0]
            return DiagnosticCheck(
                name="Evidence Ledger",
                status="error",
                message=(
                    f"Evidence ledger has {len(diagnostics)} unclean record(s) "
                    f"(first: line {first.line_number}, {first.kind}: {first.reason})."
                ),
            )
        return DiagnosticCheck(
            name="Evidence Ledger",
            status="ok",
            message=f"Evidence ledger healthy ({len(entries)} schema-valid records).",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="Evidence Ledger",
            status="error",
            message=f"Evidence ledger file is corrupted: {exc}",
        )


def check_operating_mode(cfg: Optional[Dict] = None) -> DiagnosticCheck:
    try:
        from howlplane.control_plane.config_loader import default_loader

        config_data = cfg if cfg is not None else default_loader.config
        mode = config_data.get("operating_mode", "local_only")
    except Exception as exc:
        return DiagnosticCheck(
            name="Operating Mode & Egress Guard",
            status="error",
            message=f"Failed to load operating mode configuration: {exc}",
        )

    if mode == "local_only":
        return DiagnosticCheck(
            name="Operating Mode & Egress Guard",
            status="ok",
            message="Operating mode is 'local_only' (100% Local Privacy enforced: network egress blocked).",
        )
    elif mode == "connected":
        return DiagnosticCheck(
            name="Operating Mode & Egress Guard",
            status="ok",
            message="Operating mode is 'connected' (Network egress enabled for external integrations).",
        )
    else:
        return DiagnosticCheck(
            name="Operating Mode & Egress Guard",
            status="error",
            message=f"Invalid operating mode: '{mode}'. Must be 'local_only' or 'connected'.",
        )


def check_ai_resources(provider_pool: Optional[Any] = None) -> List[DiagnosticCheck]:
    """Reports configured resource readiness without consuming generation."""
    try:
        from howlplane.control_plane.resource_cli import resource_diagnostic_rows
        from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

        pool = provider_pool or ProviderPoolManager.from_config(
            read_only=True, probe_on_start=True
        )
        return [DiagnosticCheck(**row) for row in resource_diagnostic_rows(pool)]
    except Exception as exc:
        return [DiagnosticCheck(
            name="AI Resource Configuration",
            status="error",
            message=f"Invalid AI resource configuration: {exc}",
        )]


MIN_FREE_BYTES = 1024 ** 3


def _state_dir() -> Path:
    from howlplane.control_plane.factory.campaign import factory_state_home

    return factory_state_home()


def _nearest_existing(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def check_state_dir() -> DiagnosticCheck:
    """Read-only: can the Factory state directory be written (or created)?"""
    try:
        state = _state_dir()
    except Exception as exc:
        return DiagnosticCheck(name="State Directory", status="warning",
                               message=f"Could not determine the state directory: {exc}")
    anchor = _nearest_existing(state)
    if anchor.is_dir() and os.access(anchor, os.W_OK | os.X_OK):
        return DiagnosticCheck(name="State Directory", status="ok", message=f"{state} is writable.")
    return DiagnosticCheck(
        name="State Directory", status="warning",
        message=f"State directory {state} is not writable (blocked at {anchor}).",
        details={"action": f"chmod u+rwx {anchor}"},
    )


def check_disk_space() -> DiagnosticCheck:
    """Read-only: free space where the Factory keeps state, warning below 1 GB."""
    try:
        anchor = _nearest_existing(_state_dir())
        free = shutil.disk_usage(anchor).free
    except Exception as exc:
        return DiagnosticCheck(name="Disk Space", status="warning", message=f"Could not read free disk space: {exc}")
    gb = free / 1024 ** 3
    if free < MIN_FREE_BYTES:
        return DiagnosticCheck(
            name="Disk Space", status="warning",
            message=f"Only {gb:.2f} GB free at {anchor}; the Factory needs at least 1 GB.",
            details={"action": f"df -h {anchor}"},
        )
    return DiagnosticCheck(name="Disk Space", status="ok", message=f"{gb:.1f} GB free at {anchor}.")


def check_linger() -> Optional[DiagnosticCheck]:
    """Linux/systemd only: without linger the Factory stops when the user logs out.

    Returns None (skipped silently) when systemd or loginctl is unavailable.
    """
    if not sys.platform.startswith("linux") or not shutil.which("loginctl"):
        return None
    if not Path("/run/systemd/system").is_dir():
        return None
    try:
        user = os.environ.get("USER") or getpass.getuser()
        res = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"],
                             capture_output=True, text=True, check=False, timeout=10.0)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    value = res.stdout.strip().partition("=")[2].strip().lower()
    if value == "yes":
        return DiagnosticCheck(name="Login Linger", status="ok",
                               message="User services keep running after logout (Linger=yes).")
    if value == "no":
        return DiagnosticCheck(
            name="Login Linger", status="warning",
            message="Linger is off: the Factory stops when you log out, so it cannot run 24/7.",
            details={"action": f"loginctl enable-linger {user}"},
        )
    return None


def run_diagnostics(
    repo_root: Optional[Path] = None,
    provider_pool: Optional[Any] = None,
) -> List[DiagnosticCheck]:
    root = repo_root or Path(__file__).resolve().parents[3]
    checks = [
        check_python_environment(),
        check_dependencies(),
        check_go_toolchain(),
        check_git_status(root),
        check_git_hooks(root),
        check_slopslint(),
        check_control_plane_ledger(root),
        check_operating_mode(),
    ]
    checks.extend(check_ai_resources(provider_pool))
    checks.append(check_state_dir())
    checks.append(check_disk_space())
    linger = check_linger()
    if linger is not None:
        checks.append(linger)
    return checks


def main() -> int:
    print("=" * 60)
    print("HowlPlane - System & Toolchain Diagnostics")
    print("=" * 60)
    checks = run_diagnostics()
    has_error = False
    for check in checks:
        if check.status == "ok":
            symbol = "✓"
        elif check.status == "warning":
            symbol = "!"
        else:
            symbol = "✗"
            has_error = True
        print(f"[{symbol}] {check.name}: {check.message}")
    print("=" * 60)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
