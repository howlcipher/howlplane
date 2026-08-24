#!/usr/bin/env python3
"""
_shared_store.py

Internal shared durable JSON store helpers for reasoning artifacts.

This module is intentionally internal to the reasoning package; public callers use
TrajectoryStore, ReasoningExperimentStore, and ObservationStore, which specify the
artifact-specific serialization and idempotency rules.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json

T = TypeVar("T")


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
            try:
                objects.append(self._factory(safe_load_json(p)))
            except Exception:
                continue
        return objects
