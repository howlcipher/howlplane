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
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


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
