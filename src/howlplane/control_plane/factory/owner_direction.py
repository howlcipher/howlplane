#!/usr/bin/env python3
"""Read untrusted owner-direction files a remote role can commit.

Files under ``factory/owner_direction/*.json`` are admission requests. They
are never treated as trusted provenance: a file cannot mark itself trusted,
so the supervisor parks the item for the Owner instead of dispatching it.
Ranked backlog rows with status exactly ``Pending`` remain the path the
existing Factory executes on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

OWNER_DIRECTION_SCHEMA = "howlplane.factory.owner_direction/v1"
OWNER_DIRECTION_DIR = Path("factory") / "owner_direction"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_FILES = 50
_MAX_BYTES = 65536
_MAX_TITLE = 200
_MAX_GOAL = 4000
_MAX_CONSTRAINTS = 20
_MAX_CONSTRAINT = 200


def discover_owner_directions(
    repo_path: Union[str, Path],
    repository: str,
) -> List[Dict[str, Any]]:
    """Return admission evidence for well-formed direction files.

    Malformed files are skipped. Nothing in the file is allowed to set
    ``trusted_provenance``.
    """
    directory = Path(repo_path) / OWNER_DIRECTION_DIR
    if not directory.is_dir() or directory.is_symlink():
        return []
    evidence: List[Dict[str, Any]] = []
    paths = sorted(
        path for path in directory.glob("*.json")
        if path.is_file() and not path.name.endswith(".example.json")
    )
    for path in paths[:_MAX_FILES]:
        item = _one_direction(path, directory, repository)
        if item is not None:
            evidence.append(item)
    return evidence


def collect_for_supervisor_record(record: Any) -> List[Dict[str, Any]]:
    """Scan the supervisor's target or workspace for direction files."""
    evidence: List[Dict[str, Any]] = []
    for path, name in _direction_roots(record):
        try:
            evidence.extend(discover_owner_directions(path, name))
        except Exception:
            continue
    return evidence


def _one_direction(
    path: Path,
    directory: Path,
    repository: str,
) -> Optional[Dict[str, Any]]:
    data = _read_direction_file(path, directory)
    if data is None:
        return None
    parsed = _parse_direction(data, path, repository)
    if parsed is None:
        return None
    direction_id, title, goal, constraints = parsed
    return _evidence(path.name, repository, direction_id, title, goal, constraints)


def _read_direction_file(path: Path, directory: Path) -> Optional[dict]:
    if path.is_symlink():
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(directory.resolve())
        if path.stat().st_size > _MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != OWNER_DIRECTION_SCHEMA:
        return None
    return data


def _parse_direction(
    data: dict,
    path: Path,
    repository: str,
) -> Optional[tuple]:
    direction_id = data.get("id") or path.stem
    title = data.get("title")
    goal = data.get("goal")
    if not isinstance(direction_id, str) or not _ID.fullmatch(direction_id):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(goal, str) or not goal.strip():
        return None
    declared = data.get("repository")
    if declared is not None and not _repository_matches(declared, repository):
        return None
    constraints = _constraints(data.get("constraints", []))
    if constraints is None:
        return None
    return (
        direction_id,
        title.strip()[:_MAX_TITLE],
        goal.strip()[:_MAX_GOAL],
        constraints,
    )


def _evidence(
    filename: str,
    repository: str,
    direction_id: str,
    title: str,
    goal: str,
    constraints: List[str],
) -> Dict[str, Any]:
    description = goal
    if constraints:
        description = goal + "\n\nConstraints:\n" + "\n".join(
            f"- {item}" for item in constraints
        )
    digest = _digest(direction_id, title, goal, constraints)
    return {
        "origin": "owner_direction",
        "repository": repository,
        "title": title,
        "description": description[:_MAX_GOAL],
        "identity_keys": ["factory/owner_direction", direction_id],
        "evidence_refs": [f"factory/owner_direction/{filename}"],
        "evidence_fingerprints": [f"owner_direction:{direction_id}:{digest}"],
        "trusted_provenance": False,
        "source_file_rank": 0,
        "source_rank": 0,
        "kind": "improvement",
    }


def _direction_roots(record: Any) -> List[tuple]:
    roots = _workspace_roots(getattr(record, "workspace_file", None))
    if roots:
        return roots
    return _target_root(getattr(record, "target_repository", None))


def _workspace_roots(workspace_file: Any) -> List[tuple]:
    if not workspace_file:
        return []
    try:
        from howlplane.control_plane.factory.target import Workspace
        workspace = Workspace.from_file(workspace_file)
    except Exception:
        return []
    roots = []
    for repo in workspace.repositories:
        name = (repo.repository or "").strip() or repo.path.name
        roots.append((repo.path, name))
    return roots


def _target_root(target: Any) -> List[tuple]:
    if not target:
        return []
    path = Path(str(target))
    if not path.is_dir():
        return []
    try:
        from howlplane.control_plane.git_integration import detect_repo_slug
        slug = detect_repo_slug(path)
    except Exception:
        slug = None
    return [(path, slug or path.name)]


def _repository_matches(declared: Any, repository: str) -> bool:
    if not isinstance(declared, str):
        return False
    text = declared.strip()
    tail = repository.rsplit("/", 1)[-1]
    return text == repository or text == tail


def _constraints(value: Any) -> Optional[List[str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_CONSTRAINTS:
        return None
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        cleaned.append(item.strip()[:_MAX_CONSTRAINT])
    return cleaned


def _digest(direction_id: str, title: str, goal: str, constraints: List[str]) -> str:
    payload = {
        "constraints": constraints,
        "goal": goal,
        "id": direction_id,
        "title": title,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
