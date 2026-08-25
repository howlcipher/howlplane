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
        status="warning",
        message=f"Not running inside an active VIRTUAL_ENV (Python {py_ver})",
    )


def check_dependencies() -> DiagnosticCheck:
    required = ["pytest", "yaml", "jsonschema"]
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
    if not (repo_root / ".git").is_dir():
        return DiagnosticCheck(
            name="Git Repository",
            status="warning",
            message=f"Directory {repo_root} is not a git repository.",
        )
    return DiagnosticCheck(
        name="Git Repository",
        status="ok",
        message=f"Valid git repository confirmed at {repo_root}.",
    )


def check_git_hooks(repo_root: Path) -> DiagnosticCheck:
    hooks_dir = repo_root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        return DiagnosticCheck(
            name="Git Hooks",
            status="warning",
            message="Git hooks directory .git/hooks not found.",
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
            details={"action": "Run hook installer scripts to install."},
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
            details={"action": "Run 'bash scripts/install_slopslint.sh' to install pinned v0.1.0."},
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
            )
        return DiagnosticCheck(
            name="SlopsLint Binary",
            status="warning",
            message=f"SlopsLint returned non-zero exit code: {res.returncode}.",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="SlopsLint Binary",
            status="warning",
            message=f"Error checking slopslint: {exc}.",
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
        valid_lines = 0
        with open(ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    json.loads(line)
                    valid_lines += 1
        return DiagnosticCheck(
            name="Evidence Ledger",
            status="ok",
            message=f"Evidence ledger healthy ({valid_lines} valid JSON records).",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="Evidence Ledger",
            status="error",
            message=f"Evidence ledger file is corrupted: {exc}",
        )


def check_operating_mode(cfg: Optional[Dict] = None) -> DiagnosticCheck:
    try:
        from src.infrastructure.config_loader import default_loader

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
        from src.control_plane.resource_cli import resource_diagnostic_rows
        from src.control_plane.synthesis.provider_pool import ProviderPoolManager

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

def run_diagnostics(
    repo_root: Optional[Path] = None,
    provider_pool: Optional[Any] = None,
) -> List[DiagnosticCheck]:
    root = repo_root or Path(__file__).resolve().parent.parent.parent
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
