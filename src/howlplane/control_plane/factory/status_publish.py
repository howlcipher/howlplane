#!/usr/bin/env python3
"""Redacted, durable Factory status snapshots for remote operators.

The snapshot is a Git-readable projection of supervisor state. It is not a
second campaign, and it is not the supervisor's private state directory.
Callers on the Factory host publish it; remote roles only read the file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from howlplane.control_plane.atomic_io import atomic_write_text


STATUS_SCHEMA = "howlplane.factory.status/v1"
DEFAULT_RELATIVE_PATH = Path(".dogfood") / "factory-status.json"
MARKER_NAME = "status_publish.path"
OWNER_REQUIRED = "OWNER_REQUIRED"
_MAX_TEXT = 500
_MAX_BLOCKERS = 40
_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_AUTHORITY_VALUES = frozenset({
    "strict",
    "overnight-safe",
    "howlframe-overnight",
    "safe",
    "standard",
    "autonomous",
    "not configured",
})

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"(?i)([?&][^=&\s]*(?:token|key|secret|password|passwd|cookie)[^=&\s]*)=[^&\s]+"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|cookie|session|token)\b\s*[:=]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_GITHUB_TOKEN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")
_GITHUB_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_POSIX_ABS = re.compile(r"(?<![:\w])(?:/(?:[\w.@+-]+))+")
_WINDOWS_ABS = re.compile(r"[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']+")
_SECRET_VALUE_PATTERNS = (
    _ASSIGNMENT,
    _BEARER,
    _GITHUB_TOKEN,
    _GITHUB_PAT,
    _SK_TOKEN,
    _SLACK_TOKEN,
    _AWS_KEY,
    _JWT,
)


def default_publish_path(start: Optional[Union[str, Path]] = None) -> Path:
    """Snapshot path relative to a checkout. Default is the process cwd."""
    root = Path(start) if start is not None else Path.cwd()
    return root / DEFAULT_RELATIVE_PATH


def repository_slug(status: Mapping[str, Any]) -> Optional[str]:
    """Prefer an owner/name slug over a filesystem path."""
    for key in ("project", "target_repository"):
        slug = _slug_from(status.get(key))
        if slug is not None:
            return slug
    return None


def redact_text(value: str, repository: Optional[str] = None) -> str:
    """Strip credentials and absolute host paths from one free-text field."""
    urls: list[str] = []

    def _stash(match: re.Match) -> str:
        urls.append(_redact_url(match.group(0)))
        return f"\x00URL{len(urls) - 1}\x00"

    text = _URL.sub(_stash, value)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _replace_paths(text, repository)
    for index, url in enumerate(urls):
        text = text.replace(f"\x00URL{index}\x00", url)
    text = text.replace("\x00", "")
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "..."
    return text


def build_redacted_status(
    status: Mapping[str, Any],
    *,
    mission_campaign_id: Optional[str] = None,
    published_at: Optional[str] = None,
) -> dict:
    """Project supervisor status onto the public snapshot shape."""
    repository = repository_slug(status)
    dispatch = status.get("current_dispatch_id")
    if isinstance(dispatch, str) and dispatch.strip():
        current_dispatch = redact_text(dispatch.strip(), repository)
    else:
        current_dispatch = "idle"
    blockers = _blockers(status, repository)
    state = _plain(status.get("state")) or "unknown"
    if state == "waiting_for_authority" and not any(
        item.get("class") == OWNER_REQUIRED for item in blockers
    ):
        blockers.append({
            "class": OWNER_REQUIRED,
            "summary": "supervisor is waiting for authority",
        })
    authority = status.get("authority")
    if authority == "not configured":
        blockers.append({
            "class": OWNER_REQUIRED,
            "summary": "authority envelope is not configured",
        })
    blockers = blockers[:_MAX_BLOCKERS]
    owner_required = any(item.get("class") == OWNER_REQUIRED for item in blockers)
    mission = mission_campaign_id if _safe_mission_id(mission_campaign_id) else None
    return {
        "schema": STATUS_SCHEMA,
        "redacted": True,
        "published_at": published_at or datetime.now(timezone.utc).isoformat(),
        "campaign_id": _plain(status.get("campaign_id")),
        "mission_campaign_id": mission,
        "repository": repository,
        "state": state,
        "current_dispatch": current_dispatch,
        "current_work_item_id": _plain(status.get("current_work_item_id")),
        "blockers": blockers,
        "owner_required": owner_required,
        "last_tick_at": _plain(status.get("last_tick_at")),
        "last_successful_tick_at": _plain(status.get("last_successful_tick_at")),
        "last_error": _optional_text(status.get("last_error"), repository),
        "failure_count": _count(status.get("failure_count")),
        "stopped_reason": _optional_text(status.get("stopped_reason"), repository),
        "objective": _optional_text(status.get("objective"), repository),
        "target_mode": _plain(status.get("target_mode")),
        "run_mode": _plain(status.get("run_mode")),
        "authority": authority if authority in _AUTHORITY_VALUES else None,
    }


def read_mission_campaign_id(publish_path: Union[str, Path]) -> Optional[str]:
    """Read the org campaign id from a sibling mission_state file, if present."""
    path = Path(publish_path)
    candidates = []
    if path.parent.name == ".dogfood":
        candidates.append(path.parent / "mission_state.json")
    candidates.append(path.parent / ".dogfood" / "mission_state.json")
    for candidate in candidates:
        mission_id = _mission_id_in(candidate)
        if mission_id is not None:
            return mission_id
    return None


def assert_publishable(
    path: Union[str, Path],
    state_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Resolve the snapshot path and refuse to write it into Factory state."""
    resolved = Path(path).expanduser().resolve()
    if resolved.name in {"factory_supervisor.json", "process.json", "campaign.json"}:
        raise ValueError(f"refusing to publish factory status over {resolved.name}")
    if state_dir is not None and _is_inside(resolved, Path(state_dir)):
        raise ValueError("refusing to publish factory status inside the state directory")
    return resolved


def publish_status(
    status: Mapping[str, Any],
    path: Union[str, Path],
    *,
    mission_campaign_id: Optional[str] = None,
    published_at: Optional[str] = None,
    state_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Write one redacted snapshot. Does not start or signal a supervisor."""
    destination = assert_publishable(path, state_dir)
    if mission_campaign_id is None:
        mission_campaign_id = read_mission_campaign_id(destination)
    snapshot = build_redacted_status(
        status,
        mission_campaign_id=mission_campaign_id,
        published_at=published_at,
    )
    payload = json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n"
    atomic_write_text(destination, payload)
    return destination


def arm_periodic_publish(
    state_dir: Union[str, Path],
    publish_path: Union[str, Path],
) -> Path:
    """Remember an absolute snapshot path for a future supervisor process.

    The marker lives in the host-local state directory. The running process
    only honors it after it is executing code that reads the marker.
    """
    destination = assert_publishable(publish_path, state_dir)
    marker = Path(state_dir).expanduser().resolve() / MARKER_NAME
    atomic_write_text(marker, str(destination) + "\n")
    return marker


def read_armed_publish_path(state_dir: Optional[Union[str, Path]]) -> Optional[Path]:
    """Return the armed absolute snapshot path, or None when unset or relative."""
    if state_dir is None:
        return None
    marker = Path(state_dir) / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        line = marker.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return None
    if not line:
        return None
    raw = line[0].strip()
    if not raw or raw.startswith("#"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        return assert_publishable(path, state_dir)
    except ValueError:
        return None


def publish_cli_status(status: Mapping[str, Any], args: Any) -> Optional[Path]:
    """Honor `factory status --publish` without starting a campaign."""
    publish = bool(getattr(args, "publish", False) or getattr(args, "arm_periodic", False))
    if not publish:
        return None
    raw = getattr(args, "publish_path", None)
    path = Path(raw) if raw else default_publish_path()
    state_dir = getattr(args, "state_dir", None)
    written = publish_status(status, path, state_dir=state_dir)
    if getattr(args, "arm_periodic", False):
        if not state_dir:
            raise ValueError("--arm-periodic needs the factory state directory")
        arm_periodic_publish(state_dir, written)
    return written


def _blockers(status: Mapping[str, Any], repository: Optional[str]) -> list:
    blockers = []
    parked = status.get("parked_items") or []
    if isinstance(parked, Sequence) and not isinstance(parked, (str, bytes)):
        for item in parked:
            if not isinstance(item, Mapping):
                continue
            state = _plain(item.get("state")) or "blocked"
            blockers.append({
                "class": _blocker_class(state),
                "work_item_id": _plain(item.get("work_item_id")),
                "state": state,
                "summary": _optional_text(item.get("blocker"), repository),
            })
    proposals = status.get("proposals_awaiting_authority") or []
    if isinstance(proposals, Sequence) and not isinstance(proposals, (str, bytes)):
        for proposal in proposals:
            if isinstance(proposal, Mapping):
                proposal_id = _plain(proposal.get("proposal_id"))
            else:
                proposal_id = _plain(proposal)
            blockers.append({
                "class": OWNER_REQUIRED,
                "proposal_id": proposal_id,
                "summary": "proposal awaiting authority",
            })
    return blockers


def _blocker_class(state: str) -> str:
    if state == "awaiting_owner":
        return OWNER_REQUIRED
    if state == "deferred":
        return "DEFERRED"
    return "BLOCKED"


def _redact_url(url: str) -> str:
    cleaned = _USERINFO.sub(r"\1[redacted]@", url)
    cleaned = _QUERY_SECRET.sub(r"\1=[redacted]", cleaned)
    for pattern in _SECRET_VALUE_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned


def _replace_paths(text: str, repository: Optional[str]) -> str:
    def _posix(match: re.Match) -> str:
        return _path_token(match.group(0), repository)

    def _windows(match: re.Match) -> str:
        return _path_token(match.group(0).replace("\\", "/"), repository)

    text = _POSIX_ABS.sub(_posix, text)
    return _WINDOWS_ABS.sub(_windows, text)


def _path_token(path: str, repository: Optional[str]) -> str:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if repository and name and (repository == name or repository.endswith("/" + name)):
        return repository
    return "[path]"


def _slug_from(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("/") or text.startswith("\\") or ":\\" in text:
        return None
    if "://" in text:
        text = text.split("://", 1)[1]
        if "@" in text.split("/", 1)[0]:
            text = text.split("@", 1)[1]
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return None
    owner, name = parts[-2], parts[-1]
    if _MISSION_ID.fullmatch(owner) and _MISSION_ID.fullmatch(name):
        return f"{owner}/{name}"
    return None


def _safe_mission_id(value: Optional[str]) -> bool:
    return isinstance(value, str) and _MISSION_ID.fullmatch(value) is not None


def _mission_id_in(path: Path) -> Optional[str]:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mission_id = data.get("campaign_id")
    if _safe_mission_id(mission_id):
        return mission_id
    return None


def _plain(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip()
    return text or None


def _optional_text(value: Any, repository: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = redact_text(str(value), repository)
    return text or None


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(Path(parent).expanduser().resolve())
    except ValueError:
        return False
    return True
