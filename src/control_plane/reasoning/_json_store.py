#!/usr/bin/env python3
"""
_shared_store.py

Internal shared durable JSON store helpers for reasoning artifacts.

This module is intentionally internal to the reasoning package; public callers use
TrajectoryStore, ReasoningExperimentStore, and ObservationStore, which specify the
artifact-specific serialization and idempotency rules.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json

T = TypeVar("T")

_SAFE_OBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArtifactIdentityError(ValueError):
    """Raised when an artifact identifier could escape its durable store."""


class DurableObjectStore:
    """Atomic, idempotent durable store for schema-versioned JSON objects."""

    _filename_suffix: str = ".json"

    def __init__(
        self,
        base_dir: Union[str, Path],
        factory: Callable[[Dict[str, Any]], T],
        dedup_field: Optional[str] = None,
    ):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._factory = factory
        self._dedup_field = dedup_field

    def _path(self, obj_id: str) -> Path:
        if not isinstance(obj_id, str) or not _SAFE_OBJECT_ID.fullmatch(obj_id):
            raise ArtifactIdentityError(
                "Artifact IDs must be 1-128 safe filename characters."
            )
        return self.base_dir / f"{obj_id}{self._filename_suffix}"

    def save(self, obj_id: str, data: Dict[str, Any]) -> Path:
        target = self._path(obj_id)
        if target.is_file() and self._dedup_field:
            try:
                existing = safe_load_json(target)
            except Exception:
                pass
            else:
                if existing.get(self._dedup_field) == data.get(self._dedup_field):
                    return target
                raise FileExistsError(
                    f"Artifact '{obj_id}' already exists with different {self._dedup_field}."
                )
        atomic_write_json(target, data)
        return target

    def load(self, obj_id: str) -> T:
        target = self._path(obj_id)
        return self._factory(safe_load_json(target))

    def exists(self, obj_id: str) -> bool:
        return self._path(obj_id).is_file()

    def list_all(self) -> List[T]:
        objects: List[T] = []
        for p in sorted(self.base_dir.glob(f"*{self._filename_suffix}")):
            objects.append(self._factory(safe_load_json(p)))
        return objects
