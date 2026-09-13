"""Persistent process backends for a resolved Factory campaign."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Optional

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.factory.campaign import CampaignError, FactoryCampaign
from src.control_plane.locking import LockError, SupervisorLock, get_process_create_time, is_process_alive


@dataclass
class FactoryProcessRecord:
    pid: int
    process_create_time: float
    hostname: str
    backend: str
    command: list[str]
    started_at: str
    log_path: str
    unit_name: Optional[str] = None
    status: str = "running"


def _record_path(campaign: FactoryCampaign) -> Path:
    return campaign.state_dir / "campaign" / "process.json"


def _log_path(campaign: FactoryCampaign) -> Path:
    return campaign.state_dir / "logs" / "factory.log"


def load_process(campaign: FactoryCampaign) -> Optional[FactoryProcessRecord]:
    path = _record_path(campaign)
    if not path.is_file():
        return None
    try:
        return FactoryProcessRecord(**safe_load_json(path))
    except Exception as exc:
        raise CampaignError(f"Factory process record is unreadable: {exc}") from exc


def _save_process(campaign: FactoryCampaign, record: FactoryProcessRecord) -> None:
    _record_path(campaign).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(_record_path(campaign), asdict(record))


def _systemd_available() -> bool:
    binary = shutil.which("systemctl")
    if not binary or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        result = subprocess.run([binary, "--user", "show-environment"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=3, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _active(campaign: FactoryCampaign, record: FactoryProcessRecord) -> bool:
    if record.backend == "systemd" and record.unit_name:
        try:
            result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", record.unit_name],
                                    timeout=3, check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    alive, _ = is_process_alive(record.pid, record.hostname, record.process_create_time)
    return alive


def process_status(campaign: FactoryCampaign) -> str:
    record = load_process(campaign)
    if record is None:
        return "absent"
    if _active(campaign, record):
        return "running"
    if record.status == "running":
        record.status = "stale"
        _save_process(campaign, record)
    return record.status


def _command(campaign: FactoryCampaign, authority_profile: Optional[str], objective: Optional[str]) -> list[str]:
    command = [sys.executable, "-m", "src.control_plane.cli", "factory", "run",
               "--state-dir", str(campaign.state_dir), "--target-repo", str(campaign.target_dir),
               "--target", "repo", "--resume-stopped"]
    if authority_profile:
        command.extend(["--authority-profile", authority_profile])
    if objective:
        command.extend(["--objective", objective])
    return command


def start_process(campaign: FactoryCampaign, authority_profile: Optional[str], objective: Optional[str]) -> tuple[bool, FactoryProcessRecord]:
    """Start exactly one supervisor, preferring a usable user systemd manager."""
    command = _command(campaign, authority_profile, objective)
    launch_lock = SupervisorLock(campaign.state_dir, command="howlplane factory start")
    try:
        launch_lock.acquire()
    except LockError:
        # The supervisor owns this lock while running. A duplicate normal start
        # is successful when its durable record verifies that same process.
        existing = load_process(campaign)
        if existing and _active(campaign, existing):
            return False, existing
        raise
    try:
        existing = load_process(campaign)
        if existing and _active(campaign, existing):
            return False, existing
        log_path = _log_path(campaign)
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = datetime.now(timezone.utc).isoformat()
        if _systemd_available():
            unit = f"howlplane-factory-{campaign.repository.campaign_id}"
            result = subprocess.run(
                ["systemd-run", "--user", "--unit", unit, "--collect", "--same-dir",
                 "--property=Restart=on-failure", "--property=RestartSec=30", *command],
                cwd=str(Path(__file__).resolve().parents[3]), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20,
            )
            if result.returncode:
                raise CampaignError(f"Could not start Factory user service: {result.stderr.strip()}")
            pid = 0
            try:
                shown = subprocess.run(["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False)
                pid = int(shown.stdout.strip() or "0")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            record = FactoryProcessRecord(pid, get_process_create_time(pid) if pid else 0.0, socket.gethostname(),
                                          "systemd", command, now, str(log_path), unit)
        else:
            environment = dict(os.environ)
            controller = str(Path(__file__).resolve().parents[3])
            environment["PYTHONPATH"] = controller + os.pathsep + environment.get("PYTHONPATH", "")
            with log_path.open("a", encoding="utf-8") as stream:
                process = subprocess.Popen(command, cwd=controller, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            record = FactoryProcessRecord(process.pid, get_process_create_time(process.pid), socket.gethostname(),
                                          "process", command, now, str(log_path))
        _save_process(campaign, record)
        return True, record
    finally:
        launch_lock.release()


def stop_process(campaign: FactoryCampaign, timeout_seconds: float = 30.0) -> str:
    """Request supervisor reconciliation before terminating its backend."""
    record = load_process(campaign)
    if record is None or not _active(campaign, record):
        if record is not None:
            record.status = "stopped"
            _save_process(campaign, record)
        return "Factory is already stopped."
    if record.backend == "systemd" and record.unit_name:
        subprocess.run(["systemctl", "--user", "stop", record.unit_name], timeout=timeout_seconds, check=False)
    else:
        try:
            os.killpg(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and _active(campaign, record):
            time.sleep(0.1)
        if _active(campaign, record):
            raise CampaignError("Factory did not stop after its graceful reconciliation window")
    record.status = "stopped"
    _save_process(campaign, record)
    return "Factory stopped. Durable campaign state and evidence were preserved."


_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
]


def recent_logs(campaign: FactoryCampaign, follow: bool = False, lines: int = 80) -> int:
    record = load_process(campaign)
    if record and record.backend == "systemd" and record.unit_name:
        command = ["journalctl", "--user", "--unit", record.unit_name, "--no-pager", "-n", str(lines)]
        if follow:
            command.append("--follow")
        return subprocess.call(command)
    path = _log_path(campaign)
    if not path.exists():
        print("No Factory logs have been recorded for this campaign.")
        return 0
    def emit() -> None:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        for line in content:
            for pattern in _SECRET_PATTERNS:
                line = pattern.sub("[REDACTED]", line)
            print(line)
    emit()
    if follow:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(0, os.SEEK_END)
            while True:
                line = stream.readline()
                if line:
                    print(line.rstrip())
                else:
                    time.sleep(0.25)
    return 0
