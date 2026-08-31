#!/usr/bin/env python3
"""
_json_store.py

Re-export shim. The durable JSON object store now lives at
`src/control_plane/durable_store.py` because the factory's work-item store
needs the same atomicity, idempotency, and path-traversal guarantees, and a
second copy would drift from the first.

Reasoning-package callers (`TrajectoryStore`, `ReasoningExperimentStore`,
`ObservationStore`) keep importing from here unchanged.
"""

from src.control_plane.durable_store import (  # noqa: F401
    ArtifactIdentityError,
    DurableObjectStore,
    T,
    _SAFE_OBJECT_ID,
)

__all__ = [
    "ArtifactIdentityError",
    "DurableObjectStore",
    "T",
    "_SAFE_OBJECT_ID",
]
