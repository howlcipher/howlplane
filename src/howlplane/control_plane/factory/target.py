#!/usr/bin/env python3
"""
target.py

Generalized Factory target abstraction.

A target describes what repository or workspace the factory is improving. The
controller checkout that runs the factory is never the mutation target for
``--target self``; a separate candidate worktree is required.
"""

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union

import yaml

from howlplane.control_plane.atomic_io import atomic_write_json, safe_load_json
from howlplane.control_plane.factory.campaign import (
    FactoryCampaign,
    RepositoryIdentity,
    discover_repository,
    factory_data_home,
    prepare_campaign,
    resolve_campaign,
)
from howlplane.control_plane.git_integration import detect_repo_slug


class FactoryTargetMode(str, Enum):
    """Supported Factory target modes."""

    REPO = "repo"
    SELF = "self"
    ECOSYSTEM = "ecosystem"


@dataclass
class WorkspaceRepository:
    """One repository entry inside a workspace definition."""

    path: Path
    repository: str = ""
    roles: List[str] = field(default_factory=list)
    priority_hint: float = 1.0
    verification_commands: List[str] = field(default_factory=list)
    authority_constraints: List[str] = field(default_factory=list)


@dataclass
class Workspace:
    """Generic multi-repository workspace configuration."""

    repositories: List[WorkspaceRepository] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    priority_hints: Dict[str, float] = field(default_factory=dict)
    verification_commands: List[str] = field(default_factory=list)
    authority_constraints: List[str] = field(default_factory=list)
    objective: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workspace":
        repos: List[WorkspaceRepository] = []
        for entry in data.get("repositories") or []:
            if isinstance(entry, str):
                repos.append(WorkspaceRepository(path=Path(entry).expanduser().resolve()))
            else:
                repos.append(
                    WorkspaceRepository(
                        path=Path(entry.get("path", ".")).expanduser().resolve(),
                        repository=entry.get("repository", ""),
                        roles=list(entry.get("roles") or []),
                        priority_hint=float(entry.get("priority_hint", 1.0)),
                        verification_commands=list(entry.get("verification_commands") or []),
                        authority_constraints=list(entry.get("authority_constraints") or []),
                    )
                )
        return cls(
            repositories=repos,
            dependencies=list(data.get("dependencies") or []),
            priority_hints=dict(data.get("priority_hints") or {}),
            verification_commands=list(data.get("verification_commands") or []),
            authority_constraints=list(data.get("authority_constraints") or []),
            objective=data.get("objective"),
        )

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "Workspace":
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"workspace file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)


class WorkspaceResolutionError(ValueError):
    """Raised when a workspace repository cannot be identified unambiguously."""


@dataclass(frozen=True)
class RepositoryExecutionTarget:
    """Repository-scoped execution state owned by one master campaign."""

    repository: WorkspaceRepository
    identity: RepositoryIdentity
    slug: str
    key: str
    state_dir: Path
    target_dir: Path
    campaign: FactoryCampaign

    @property
    def metadata_path(self) -> Path:
        return self.state_dir / "execution.json"


def _canonical_slug(value: str) -> str:
    slug = value.strip().lower().rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    for marker in ("github.com/", "gitlab.com/", "bitbucket.org/"):
        if marker in slug:
            slug = slug.split(marker, 1)[1]
    return slug


def _repository_key(slug: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "repository"
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


class WorkspaceRepositoryResolver:
    """Canonical WorkItem.repository to repository execution-target resolver."""

    def __init__(self, workspace: Workspace, master_campaign_id: str, master_state_dir: Path):
        self.workspace = workspace
        self.master_campaign_id = master_campaign_id
        self.master_state_dir = Path(master_state_dir).resolve()
        self._targets: Dict[str, RepositoryExecutionTarget] = {}
        for repository in workspace.repositories:
            identity = discover_repository(repository.path)
            actual_slug = _canonical_slug(detect_repo_slug(identity.root) or "")
            declared_slug = _canonical_slug(repository.repository)
            if not declared_slug:
                raise WorkspaceResolutionError(
                    f"Workspace repository {repository.path} must declare a canonical repository identity"
                )
            if not actual_slug or actual_slug != declared_slug:
                raise WorkspaceResolutionError(
                    f"Workspace repository identity mismatch for {repository.path}: "
                    f"declared {declared_slug!r}, Git reports {actual_slug or 'unknown'!r}"
                )
            if declared_slug in self._targets:
                raise WorkspaceResolutionError(f"Ambiguous workspace repository identity: {declared_slug}")
            key = _repository_key(declared_slug)
            state_dir = self.master_state_dir / "repositories" / key
            target_dir = (
                factory_data_home()
                / "worktrees"
                / self.master_campaign_id
                / "repositories"
                / key
                / "target"
            ).resolve()
            campaign = resolve_campaign(
                identity.root,
                state_dir=state_dir,
                target_repo=target_dir,
            )
            self._targets[declared_slug] = RepositoryExecutionTarget(
                repository=repository,
                identity=identity,
                slug=declared_slug,
                key=key,
                state_dir=state_dir,
                target_dir=target_dir,
                campaign=campaign,
            )

    @property
    def repositories(self) -> List[RepositoryExecutionTarget]:
        return list(self._targets.values())

    def resolve(self, repository: str) -> RepositoryExecutionTarget:
        slug = _canonical_slug(repository)
        target = self._targets.get(slug)
        if target is None:
            raise WorkspaceResolutionError(f"Unknown workspace repository: {repository!r}")
        return target

    def prepare_all(self) -> List[RepositoryExecutionTarget]:
        prepared = []
        for target in self.repositories:
            prepare_campaign(target.campaign)
            metadata = {
                "schema": "howlplane.factory.repository_execution/v1",
                "master_campaign_id": self.master_campaign_id,
                "repository": target.slug,
                "repository_root": str(target.identity.root),
                "remote": target.identity.remote,
                "default_branch": target.identity.default_branch,
                "source_revision": target.identity.commit,
                "source_common_git_dir": str(target.identity.common_git_dir),
                "target_repo": str(target.target_dir),
            }
            if target.metadata_path.is_file():
                existing = safe_load_json(target.metadata_path)
                immutable = (
                    "schema",
                    "master_campaign_id",
                    "repository",
                    "repository_root",
                    "remote",
                    "source_common_git_dir",
                    "target_repo",
                )
                if any(existing.get(key) != metadata[key] for key in immutable):
                    raise WorkspaceResolutionError(
                        f"Cross-project repository execution state reuse refused for {target.slug}"
                    )
            atomic_write_json(target.metadata_path, metadata)
            prepared.append(target)
        return prepared


@dataclass
class FactoryTarget:
    """Resolved Factory target."""

    mode: FactoryTargetMode
    target_repo: Path
    workspace: Optional[Workspace] = None
    controller_checkout: Optional[Path] = None

    def ensure_isolated_self_target(self) -> None:
        """Fail closed if --target self would mutate the controller checkout."""
        if self.mode != FactoryTargetMode.SELF:
            return
        controller = self.controller_checkout
        if controller is None:
            raise ValueError(
                "--target self requires a controller checkout to be set so the "
                "factory never mutates the checkout it is executing from."
            )
        controller_resolved = controller.resolve()
        target_resolved = self.target_repo.resolve()
        if controller_resolved == target_resolved:
            raise ValueError(
                "--target self refuses to run against the controller checkout itself. "
                "Point --target-repo at a separate candidate worktree."
            )
