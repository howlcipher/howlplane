"""A run directory of named stages that resumes from the first incomplete one.

Shared by ``howlplane creative`` and ``howlplane write``. A run persists its
inputs and per-stage records in one state file. A stage counts as done only
while every output it recorded still exists with the recorded SHA-256, so an
edited or deleted output is redone on resume. A run directory holds one
request: reopening it with different inputs is refused.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from howlplane.control_plane.atomic_io import atomic_write_json


class StageFailed(RuntimeError):
    """A stage stopped. ``status`` FAILED is an error; any other names what the run awaits."""

    def __init__(self, stage: str, message: str, detail: Optional[Dict[str, Any]] = None,
                 status: str = "FAILED"):
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.detail = detail or {}
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class StagedRun:
    """Subclasses set the class attributes and define ``stage_<name>`` for each stage."""

    KIND = "staged"
    STATE_FILE = "run.json"
    STATE_SCHEMA = ""
    RUN_ID_PREFIX = "run"
    STAGES: Tuple[str, ...] = ()

    def __init__(self, run_dir: Path, inputs: Optional[Dict[str, Any]] = None,
                 env: Optional[Dict[str, str]] = None):
        self.env = self._child_env(dict(os.environ if env is None else env))
        self.run_dir = run_dir.expanduser().resolve()
        self.state_path = self.run_dir / self.STATE_FILE
        if self.state_path.exists():
            self.state = load(self.state_path)
            if inputs and inputs != self.state["inputs"]:
                raise ValueError("run directory already holds a run with different inputs; use resume")
        else:
            if inputs is None:
                raise ValueError(f"no {self.KIND} run found in {self.run_dir}")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.state = {
                "schema": self.STATE_SCHEMA,
                "run_id": f"{self.RUN_ID_PREFIX}-"
                + hashlib.sha256((str(self.run_dir) + now()).encode()).hexdigest()[:12],
                "created_at": now(),
                "inputs": inputs,
                "stages": {},
                "status": "PENDING",
            }
            self._save()

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def path(self, *parts: str) -> Path:
        target = self.run_dir.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _stage_done(self, stage: str) -> bool:
        record = self.state["stages"].get(stage)
        if not record or record.get("status") != "COMPLETED":
            return False
        return all(Path(o["path"]).exists() and sha256(Path(o["path"])) == o["sha256"]
                   for o in record.get("outputs", []))

    def _complete(self, stage: str, outputs: List[Path], **extra: Any) -> None:
        record = self.state["stages"].setdefault(stage, {})
        record.update(status="COMPLETED", finished_at=now(),
                      outputs=[{"path": str(p), "sha256": sha256(p)} for p in outputs], **extra)
        self._save()

    # -- hooks -------------------------------------------------------------
    def _child_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """The environment every component this run starts inherits."""
        return env

    def _skip(self, stage: str) -> bool:
        """Whether to record ``stage`` as skipped instead of running it."""
        return False

    def _final_status(self) -> str:
        """The status of a run whose every stage completed."""
        return "COMPLETED"

    # -- driver ------------------------------------------------------------
    def _stop(self, stage: str, status: str, error: str, **detail: Any) -> Dict[str, Any]:
        self.state["stages"][stage].update(status="FAILED" if status == "FAILED" else "BLOCKED",
                                           finished_at=now(), error=error, **detail)
        self.state.update(status=status, failed_stage=stage)
        self._save()
        return self.state

    def execute(self) -> Dict[str, Any]:
        self.state["status"] = "RUNNING"
        self._save()
        for stage in self.STAGES:
            if self._stage_done(stage):
                continue
            if self._skip(stage):
                self._complete(stage, [], skipped=True)
                continue
            self.state["stages"][stage] = {"status": "RUNNING", "started_at": now()}
            self._save()
            try:
                getattr(self, f"stage_{stage}")()
            except StageFailed as error:
                return self._stop(stage, error.status, str(error), **error.detail)
            except (OSError, ValueError, KeyError) as error:
                return self._stop(stage, "FAILED", f"{type(error).__name__}: {error}")
        self.state.update(status=self._final_status(), failed_stage=None, completed_at=now())
        self._save()
        return self.state
