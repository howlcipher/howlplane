#!/usr/bin/env python3
"""Content-bound mission authorization and successor provenance for the Factory.

Root missions (e.g. ``docs/MISSION_001.md``) are human-authored intent. They
must not auto-run merely because a file with the right name exists. When the
owner approves a root mission, the approval is bound to the mission's content
digest, so a later replacement at the same path requires re-approval.

Successor missions (``MISSION_002.md`` onward) are trusted only through
records this Factory wrote itself when a parent mission shipped. Those records
live in the Factory state directory, outside every worktree an agent can
write, so nothing inside a mission file (and nothing an agent can commit) can
make a mission trusted. The chain property is::

    trusted_successor(parent, child) :=
        a Factory provenance record binds (repository, child path, child digest)
        to parent, AND parent is a SHIPPED WorkItem whose mission path and
        digest match the record, AND parent's own mission content is authorized
        (by the owner, or recursively as a trusted successor).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Union

from howlplane.control_plane.durable_store import DurableObjectStore

MISSION_AUTHORIZATION_SCHEMA_VERSION = "howlplane.factory.mission_authorization/v1"
SUCCESSOR_PROVENANCE_SCHEMA_VERSION = "howlplane.factory.successor_provenance/v1"

AUTHORIZATION_ORIGIN_OWNER = "owner_root"
AUTHORIZATION_ORIGIN_SUCCESSOR = "successor_provenance"

# Mission files the Factory recognizes as numbered missions.
MISSION_FILE_PATTERN = re.compile(
    r"^(?:docs|documentation)/(?:DOGFOOD_)?MISSION_(\d{3})\.md$"
)

# Precise, stable trust reasons surfaced to the owner.
TRUST_AUTHORIZED = "mission_authorized"
TRUST_SUCCESSOR = "trusted_successor"
UNTRUSTED_ROOT = "untrusted_root_mission"
CONTENT_CHANGED = "mission_content_changed_after_approval"
SUCCESSOR_DIGEST_MISMATCH = "successor_provenance_digest_mismatch"
SUCCESSOR_PARENT_MISSING = "successor_provenance_parent_missing"
SUCCESSOR_PARENT_NOT_SHIPPED = "successor_provenance_parent_not_shipped"
SUCCESSOR_PARENT_MISMATCH = "successor_provenance_parent_mismatch"
SUCCESSOR_PARENT_NOT_AUTHORIZED = "successor_provenance_parent_not_authorized"
SUCCESSOR_REPOSITORY_MISMATCH = "successor_provenance_repository_mismatch"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def compute_mission_digest_bytes(content: bytes) -> str:
    """Content identity for mission bytes (full SHA-256 hex)."""
    return hashlib.sha256(content).hexdigest()


def compute_mission_digest(path: Union[str, Path]) -> str:
    """Content identity for a mission file, or "" when it is not a regular file."""
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        return ""
    return compute_mission_digest_bytes(p.read_bytes())


def mission_number(mission_path: str) -> Optional[int]:
    """The mission's sequence number, or None if the path is not a numbered mission."""
    match = MISSION_FILE_PATTERN.match(str(PurePosixPath(mission_path)))
    return int(match.group(1)) if match else None


@dataclass
class MissionAuthorizationRecord:
    """A durable, content-bound authorization for one mission file."""

    authorization_id: str
    repository: str
    mission_path: str
    mission_digest: str
    work_item_id: str
    authorized_at: str
    origin: str = AUTHORIZATION_ORIGIN_OWNER
    owner_decision_id: Optional[str] = None
    provenance_id: Optional[str] = None
    schema_version: str = MISSION_AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "repository": self.repository,
            "mission_path": self.mission_path,
            "mission_digest": self.mission_digest,
            "work_item_id": self.work_item_id,
            "authorized_at": self.authorized_at,
            "origin": self.origin,
            "owner_decision_id": self.owner_decision_id,
            "provenance_id": self.provenance_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MissionAuthorizationRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SuccessorProvenanceRecord:
    """Factory-written evidence that a parent mission's shipped change created a successor."""

    provenance_id: str
    repository: str
    parent_work_item_id: str
    parent_mission_path: str
    parent_mission_digest: str
    parent_terminal_state: str
    successor_mission_path: str
    successor_mission_digest: str
    generated_at: str
    generating_task_id: Optional[str] = None
    generating_dispatch_id: Optional[str] = None
    merge_sha: Optional[str] = None
    governed_lifecycle_evidence: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SUCCESSOR_PROVENANCE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provenance_id": self.provenance_id,
            "repository": self.repository,
            "parent_work_item_id": self.parent_work_item_id,
            "parent_mission_path": self.parent_mission_path,
            "parent_mission_digest": self.parent_mission_digest,
            "parent_terminal_state": self.parent_terminal_state,
            "successor_mission_path": self.successor_mission_path,
            "successor_mission_digest": self.successor_mission_digest,
            "generated_at": self.generated_at,
            "generating_task_id": self.generating_task_id,
            "generating_dispatch_id": self.generating_dispatch_id,
            "merge_sha": self.merge_sha,
            "governed_lifecycle_evidence": dict(self.governed_lifecycle_evidence),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SuccessorProvenanceRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class MissionAuthorizationStore(DurableObjectStore):
    """Atomic store for content-bound mission authorizations."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=MissionAuthorizationRecord.from_dict,
            dedup_field=None,
            id_attr="authorization_id",
        )

    def for_path(self, repository: str, mission_path: str) -> List[MissionAuthorizationRecord]:
        return [
            a for a in self.list_all()
            if a.repository == repository and a.mission_path == mission_path
        ]

    def is_authorized(
        self, repository: str, mission_path: str, mission_digest: str
    ) -> Optional[MissionAuthorizationRecord]:
        if not mission_digest:
            return None
        for auth in self.for_path(repository, mission_path):
            if auth.mission_digest == mission_digest:
                return auth
        return None

    def authorize(
        self,
        repository: str,
        mission_path: str,
        mission_digest: str,
        work_item_id: str,
        *,
        origin: str = AUTHORIZATION_ORIGIN_OWNER,
        owner_decision_id: Optional[str] = None,
        provenance_id: Optional[str] = None,
    ) -> MissionAuthorizationRecord:
        if not mission_digest:
            raise ValueError("Refusing to authorize a mission without a content digest")
        existing = self.is_authorized(repository, mission_path, mission_digest)
        if existing is not None:
            return existing
        record = MissionAuthorizationRecord(
            authorization_id=_stable_id("AUTH", repository, mission_path, mission_digest),
            repository=repository,
            mission_path=mission_path,
            mission_digest=mission_digest,
            work_item_id=work_item_id,
            authorized_at=_now(),
            origin=origin,
            owner_decision_id=owner_decision_id,
            provenance_id=provenance_id,
        )
        self.save_object(record)
        return record


class SuccessorProvenanceStore(DurableObjectStore):
    """Atomic store for Factory-recorded successor mission provenance."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=SuccessorProvenanceRecord.from_dict,
            dedup_field=None,
            id_attr="provenance_id",
        )

    def for_successor_path(
        self, repository: str, successor_mission_path: str
    ) -> List[SuccessorProvenanceRecord]:
        return [
            r for r in self.list_all()
            if r.repository == repository and r.successor_mission_path == successor_mission_path
        ]

    def record(
        self,
        *,
        repository: str,
        parent_work_item_id: str,
        parent_mission_path: str,
        parent_mission_digest: str,
        parent_terminal_state: str,
        successor_mission_path: str,
        successor_mission_digest: str,
        generating_task_id: Optional[str] = None,
        generating_dispatch_id: Optional[str] = None,
        merge_sha: Optional[str] = None,
        governed_lifecycle_evidence: Optional[Dict[str, Any]] = None,
    ) -> SuccessorProvenanceRecord:
        provenance_id = _stable_id(
            "PROV", repository, successor_mission_path, successor_mission_digest
        )
        if self.exists(provenance_id):
            return self.load(provenance_id)
        record = SuccessorProvenanceRecord(
            provenance_id=provenance_id,
            repository=repository,
            parent_work_item_id=parent_work_item_id,
            parent_mission_path=parent_mission_path,
            parent_mission_digest=parent_mission_digest,
            parent_terminal_state=parent_terminal_state,
            successor_mission_path=successor_mission_path,
            successor_mission_digest=successor_mission_digest,
            generated_at=_now(),
            generating_task_id=generating_task_id,
            generating_dispatch_id=generating_dispatch_id,
            merge_sha=merge_sha,
            governed_lifecycle_evidence=dict(governed_lifecycle_evidence or {}),
        )
        self.save_object(record)
        return record


@dataclass
class MissionTrust:
    trusted: bool
    reason: str
    provenance_id: Optional[str] = None
    authorization_id: Optional[str] = None


def evaluate_mission_trust(
    repository: str,
    mission_path: str,
    mission_digest: str,
    *,
    mission_store: MissionAuthorizationStore,
    provenance_store: SuccessorProvenanceStore,
    load_work_item: Callable[[str], Optional[Any]],
) -> MissionTrust:
    """Decide whether a discovered mission may run without an owner decision.

    Fails closed: every path that is not positively proven returns
    ``trusted=False`` with a precise reason.
    """
    if not mission_digest:
        return MissionTrust(False, UNTRUSTED_ROOT)

    auth = mission_store.is_authorized(repository, mission_path, mission_digest)
    if auth is not None:
        return MissionTrust(
            True, TRUST_AUTHORIZED,
            provenance_id=auth.provenance_id, authorization_id=auth.authorization_id,
        )

    records = provenance_store.for_successor_path(repository, mission_path)
    if records:
        matching = [r for r in records if r.successor_mission_digest == mission_digest]
        if not matching:
            return MissionTrust(False, SUCCESSOR_DIGEST_MISMATCH)
        record = matching[0]
        if record.repository != repository:
            return MissionTrust(False, SUCCESSOR_REPOSITORY_MISMATCH)
        parent = load_work_item(record.parent_work_item_id)
        if parent is None:
            return MissionTrust(False, SUCCESSOR_PARENT_MISSING)
        if getattr(parent, "repository", None) != repository:
            return MissionTrust(False, SUCCESSOR_REPOSITORY_MISMATCH)
        parent_state = getattr(parent, "state", "")
        if str(getattr(parent_state, "value", parent_state)) != "shipped":
            return MissionTrust(False, SUCCESSOR_PARENT_NOT_SHIPPED)
        if (
            getattr(parent, "mission_path", None) != record.parent_mission_path
            or getattr(parent, "mission_digest", None) != record.parent_mission_digest
        ):
            return MissionTrust(False, SUCCESSOR_PARENT_MISMATCH)
        if mission_store.is_authorized(
            repository, record.parent_mission_path, record.parent_mission_digest
        ) is None:
            return MissionTrust(False, SUCCESSOR_PARENT_NOT_AUTHORIZED)
        return MissionTrust(True, TRUST_SUCCESSOR, provenance_id=record.provenance_id)

    if mission_store.for_path(repository, mission_path):
        return MissionTrust(False, CONTENT_CHANGED)
    return MissionTrust(False, UNTRUSTED_ROOT)


def evidence_fingerprint_for_mission(mission_path: str, mission_digest: str) -> str:
    """Evidence fingerprint that changes whenever mission content changes."""
    return f"mission:{mission_path}:{mission_digest}"


def iter_mission_files(repo_path: Union[str, Path]) -> List[Path]:
    """Every numbered mission file under docs/ or documentation/.

    A filename is not trust. Callers still have to evaluate content identity.
    Symlinks are skipped so a mission cannot point outside the repository.
    """
    root = Path(repo_path)
    found: List[Path] = []
    for directory in ("docs", "documentation"):
        folder = root / directory
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*MISSION_*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if mission_number(relative) is None:
                continue
            found.append(path)
    return found


def inspect_successor_missions(
    repo_root: Union[str, Path],
    merge_sha: str,
    parent_mission_path: str,
    *,
    run_git: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, str]]:
    """Mission files a governed merge *added*, with the digest of their merged content.

    A candidate must be added by ``merge_sha`` itself (not merely present in
    the tree) and must be either a numbered mission later than the parent or
    the ``next_mission`` that the merged ``.dogfood/mission_state.json`` names.
    Digests are read from the merge commit, never from the working tree, so a
    later edit on disk cannot inherit the provenance.
    """
    import json

    def _git_text(args: List[str]) -> Tuple[int, str, str]:
        if run_git is not None:
            result = run_git(args)
            out = result.stdout
            err = result.stderr
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            return result.returncode, out or "", err or ""
        from howlplane.control_plane.git_env import run_git_in_repo

        result = run_git_in_repo(repo_root, args)
        return result.returncode, result.stdout or "", result.stderr or ""

    diff_code, diff_out, diff_err = _git_text(
        ["diff", "--name-status", "--no-renames", f"{merge_sha}^1", merge_sha]
    )
    if diff_code != 0:
        raise RuntimeError(f"git diff for {merge_sha} failed: {diff_err.strip()}")
    added = set()
    for line in diff_out.splitlines():
        status, _, path = line.partition("\t")
        if status.strip() == "A" and path:
            added.add(path.strip())

    parent_number = mission_number(parent_mission_path)
    candidates = set()
    for path in added:
        number = mission_number(path)
        if number is not None and (parent_number is None or number > parent_number):
            candidates.add(path)
    state_code, state_out, _state_err = _git_text(["show", f"{merge_sha}:.dogfood/mission_state.json"])
    if state_code == 0:
        try:
            next_mission = json.loads(state_out).get("next_mission")
        except ValueError:
            next_mission = None
        if isinstance(next_mission, str) and next_mission in added:
            candidates.add(next_mission)

    successors: List[Dict[str, str]] = []
    for path in sorted(candidates):
        if path == parent_mission_path:
            continue
        blob_code, blob_out, _blob_err = _git_text(["show", f"{merge_sha}:{path}"])
        if blob_code != 0:
            continue
        successors.append({
            "mission_path": path,
            "mission_digest": compute_mission_digest_bytes(blob_out.encode("utf-8")),
        })
    return successors
