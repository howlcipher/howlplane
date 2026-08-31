#!/usr/bin/env python3
"""
durable_store.py

Atomic, idempotent durable store for schema-versioned JSON objects.

This began life inside the reasoning package, whose own module docstring
called it "intentionally internal". It is now shared: the factory's work-item
and proposal stores need exactly the same guarantees, and copying them would
both duplicate the path-traversal defence and drift from it. The reasoning
package keeps importing it from its original home through a re-export shim, so
no existing caller changes.

The guarantees a subclass inherits:

  * writes are atomic (`atomic_io.atomic_write_json`), so a crash mid-save
    leaves the previous object intact rather than a truncated one
  * object ids cannot escape the store directory (`_SAFE_OBJECT_ID`)
  * an optional `dedup_field` makes `save` idempotent for a re-run while
    refusing a silent overwrite that would change the object's identity
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
        id_attr: Optional[str] = None,
    ):
        """
        `id_attr` names the attribute holding an object's own identifier. When
        set, `save_object` can derive the id from the object itself, which is
        what lets a subclass be a declaration rather than a reimplementation.
        """
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._factory = factory
        self._dedup_field = dedup_field
        self._id_attr = id_attr

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

    def save_object(self, obj: Any) -> Path:
        """Persist an object that carries its own id in `id_attr`.

        Deliberately binds `DurableObjectStore.save` rather than `self.save`.
        Subclasses predating this method expose a single-argument
        `save(object)` convenience that shadows the two-argument base, so
        dispatching through `self` would call the wrapper that called us.
        """
        if not self._id_attr:
            raise ArtifactIdentityError(
                "save_object requires the store to declare id_attr."
            )
        return DurableObjectStore.save(self, getattr(obj, self._id_attr), obj.to_dict())

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

    def find_by_field(self, name: str, value: Any) -> Optional[T]:
        """First stored object whose `name` attribute equals `value`.

        Linear by design. These stores hold work items and observations, not
        events, and an index over them would be a second source of truth for
        something a directory listing already answers correctly.
        """
        for obj in self.list_all():
            if getattr(obj, name, None) == value:
                return obj
        return None
