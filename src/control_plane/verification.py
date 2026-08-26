#!/usr/bin/env python3
"""
verification.py

Deterministic verification plan construction and execution.
Distinguishes between claimed, tested, observed, and verified statuses.
"""

from dataclasses import dataclass, field, asdict, fields
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Union
import yaml

from src.control_plane.hygiene_policy import HygienePolicyClassifier, PINNED_SLOPSLINT_VERSION

VERIFICATION_PLAN_SCHEMA_VERSION = "ai.verification_plan/v1"

VALID_STEP_STATUSES = {"claimed", "tested", "observed", "verified", "failed", "skipped"}
VALID_STEP_CATEGORIES = {
    "build",
    "lint",
    "repository_hygiene",
    "unit_test",
    "integration_test",
    "security_check",
    "policy_check",
    "custom",
}
VALID_OVERALL_STATUSES = {"unverified", "passed", "failed", "partial"}


class VerificationError(ValueError):
    """Raised when verification schema or execution constraints are violated."""
    pass


def resolve_python_interpreter(project_root: Optional[Union[str, Path]] = None) -> str:
    """
    Deterministically discovers a compatible Python interpreter:
    1. Active virtualenv ($VIRTUAL_ENV or sys.prefix if running inside a venv)
    2. Repository-local ./venv/bin/python
    3. Repository-local ./.venv/bin/python
    4. Current sys.executable
    5. 'python3' on PATH
    Fails clearly if no valid interpreter is found.
    """
    target_root = Path(project_root).resolve() if project_root else Path.cwd()

    # 1. Active virtualenv
    active_venv = os.environ.get("VIRTUAL_ENV")
    if active_venv:
        candidate = Path(active_venv) / "bin" / "python"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    # 2. Local ./venv
    local_venv = target_root / "venv" / "bin" / "python"
    if local_venv.is_file() and os.access(local_venv, os.X_OK):
        return str(local_venv)

    # 3. Local ./.venv
    local_dot_venv = target_root / ".venv" / "bin" / "python"
    if local_dot_venv.is_file() and os.access(local_dot_venv, os.X_OK):
        return str(local_dot_venv)

    # 4. Current sys.executable if valid
    if sys.executable and os.access(sys.executable, os.X_OK):
        return str(sys.executable)

    # 5. System python3
    py3 = shutil.which("python3")
    if py3 and os.access(py3, os.X_OK):
        return str(py3)

    py = shutil.which("python")
    if py and os.access(py, os.X_OK):
        return str(py)

    raise RuntimeError(
        f"Unable to resolve a valid Python interpreter in active virtualenv, {target_root}/venv, "
        f"{target_root}/.venv, or system PATH."
    )


from src.control_plane.task_spec import DataClassSerializationMixin


@dataclass
class VerificationStep(DataClassSerializationMixin):
    """Represents a discrete verification command or check."""

    step_id: str
    name: str
    command: Union[str, List[str]]
    category: str = "unit_test"
    status: str = "claimed"
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    duration_seconds: Optional[float] = None
    interpreter: Optional[str] = None
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not self.step_id or not isinstance(self.step_id, str):
            raise VerificationError("step_id must be a non-empty string.")
        if self.category not in VALID_STEP_CATEGORIES:
            raise VerificationError(
                f"category '{self.category}' invalid. Allowed: {sorted(VALID_STEP_CATEGORIES)}"
            )
        if self.status not in VALID_STEP_STATUSES:
            raise VerificationError(
                f"status '{self.status}' invalid. Allowed: {sorted(VALID_STEP_STATUSES)}"
            )


@dataclass
class VerificationPlan(DataClassSerializationMixin):
    """Represents the complete verification suite for a task or project."""

    task_id: str
    steps: List[VerificationStep] = field(default_factory=list)
    overall_status: str = "unverified"
    schema: str = VERIFICATION_PLAN_SCHEMA_VERSION

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise VerificationError("task_id must be a non-empty string.")
        if self.overall_status not in VALID_OVERALL_STATUSES:
            raise VerificationError(
                f"overall_status '{self.overall_status}' invalid. Allowed: {sorted(VALID_OVERALL_STATUSES)}"
            )
        for s in self.steps:
            s.validate()

    def add_step(
        self,
        step_id: str,
        name: str,
        command: Union[str, List[str]],
        category: str = "unit_test",
        required: bool = True,
        interpreter: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        step = VerificationStep(
            step_id=step_id,
            name=name,
            command=command,
            category=category,
            status="claimed",
            required=required,
            interpreter=interpreter,
            metadata=metadata or {},
        )
        self.steps.append(step)

    def execute_step(
        self,
        step: VerificationStep,
        cwd: Optional[str] = None,
        progress_tracker: Optional[Any] = None,
    ) -> None:
        """
        Executes a single step deterministically using subprocess without shell=True.
        Enforces provider integrity for repository hygiene gates.
        Transitions: claimed -> tested -> observed -> (verified | failed).
        """
        step.status = "tested"
        cmd_args: List[str]
        if isinstance(step.command, str):
            cmd_args = shlex.split(step.command)
        else:
            cmd_args = list(step.command)

        start_time = time.time()
        env = os.environ.copy()
        target_root = Path(cwd).resolve() if cwd else Path.cwd()

        # Hygiene provider integrity check
        if step.category == "repository_hygiene" or (cmd_args and "slopslint" in cmd_args[0]):
            ok, msg, prov_meta = HygienePolicyClassifier.verify_provider_integrity(
                executable_name="slopslint",
                pinned_version=PINNED_SLOPSLINT_VERSION,
            )
            step.metadata["hygiene_provider"] = prov_meta
            if not ok:
                step.duration_seconds = round(time.time() - start_time, 3)
                step.exit_code = 127 if prov_meta.get("status") == "missing" else 1
                step.stderr = f"Hygiene provider integrity failure: {msg}"
                step.status = "failed"
                return
            # Use resolved absolute binary path to avoid PATH hijacking
            if cmd_args and cmd_args[0] == "slopslint" and prov_meta.get("path"):
                cmd_args[0] = prov_meta["path"]

        # Resolve Python interpreter deterministically for python-based commands
        try:
            resolved_py = resolve_python_interpreter(target_root)
            step.interpreter = resolved_py
            py_bin_dir = str(Path(resolved_py).parent)
            env["PATH"] = f"{py_bin_dir}:{env.get('PATH', '')}"
            if "venv" in py_bin_dir or ".venv" in py_bin_dir:
                env["VIRTUAL_ENV"] = str(Path(resolved_py).parent.parent)
        except Exception:
            step.interpreter = sys.executable

        # If command starts with python/pytest/flake8/bandit and we resolved a specific venv python
        if cmd_args and cmd_args[0] in ("python", "python3") and step.interpreter:
            cmd_args[0] = step.interpreter
        elif cmd_args and cmd_args[0] in ("pytest", "flake8", "bandit", "mypy", "black", "pdoc") and step.interpreter:
            candidate_bin = Path(step.interpreter).parent / cmd_args[0]
            if candidate_bin.is_file() and os.access(candidate_bin, os.X_OK):
                cmd_args[0] = str(candidate_bin)

        cmd_display = (
            " ".join(shlex.quote(c) for c in cmd_args)
            if isinstance(cmd_args, list)
            else str(cmd_args)
        )

        from src.control_plane.progress import track_operation
        try:
            with track_operation(
                progress_tracker,
                phase="VERIFYING",
                resource_id=step.category,
                details=cmd_display,
                role="verification",
            ):
                res = subprocess.run(
                    cmd_args,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=300,
                )

            step.duration_seconds = round(time.time() - start_time, 3)
            step.exit_code = res.returncode
            step.stdout = res.stdout
            step.stderr = res.stderr
            step.status = "observed"

            if res.returncode == 0:
                step.status = "verified"
            else:
                step.status = "failed"

        except Exception as e:
            step.duration_seconds = round(time.time() - start_time, 3)
            step.exit_code = -1
            step.stderr = str(e)
            step.status = "failed"

    def execute_all(
        self,
        cwd: Optional[str] = None,
        stop_on_failure: bool = False,
        progress_tracker: Optional[Any] = None,
    ) -> str:
        """
        Executes all steps in order and computes the overall status.
        """
        any_failed = False
        all_passed = True

        for step in self.steps:
            self.execute_step(step, cwd=cwd, progress_tracker=progress_tracker)
            if step.status == "failed":
                any_failed = True
                if step.required:
                    all_passed = False
                if stop_on_failure:
                    break
            elif step.status != "verified":
                all_passed = False

        if any_failed:
            self.overall_status = "failed"
        elif all_passed:
            self.overall_status = "passed"
        else:
            self.overall_status = "partial"

        return self.overall_status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "overall_status": self.overall_status,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationPlan":
        steps_data = data.get("steps", [])
        steps = [VerificationStep.from_dict(item) for item in steps_data]
        return cls(
            task_id=data.get("task_id", ""),
            steps=steps,
            overall_status=data.get("overall_status", "unverified"),
        )
