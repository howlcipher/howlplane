#!/usr/bin/env python3
"""
strategy_registry.py

Stable, versioned reasoning strategy identifiers with digest-stable definitions.

Strategy definitions are code-defined and digest-verified. Repository content can
reference a strategy_id but cannot redefine the policy, authority, metrics, or
implementation of a strategy. This prevents prompt-injection and repository-
content tampering from silently altering reasoning experiments.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.task_spec import DataClassSerializationMixin
from src.control_plane.reasoning.artifact_safety import (
    ArtifactIntegrityError,
    safe_artifact_value,
)

STRATEGY_SCHEMA_VERSION = "howlplane.strategy_definition/v1"
_STRATEGY_ID_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+/v[1-9][0-9]*$"
)


class StrategyIdentityError(ValueError):
    """Raised when a strategy identity or digest conflict is detected."""
    pass


@dataclass
class StrategyDefinition(DataClassSerializationMixin):
    """
    Immutable definition of a reasoning strategy.

    The digest is computed from (strategy_id, version, strategy_type,
    immutable_config) and is stable across processes. It is stored on the
    object so callers do not have to trust a separate computation.
    """

    strategy_id: str
    version: str
    strategy_type: str
    description: str
    immutable_config: Dict[str, Any] = field(default_factory=dict)
    prompt_template_path: Optional[str] = None
    schema: str = STRATEGY_SCHEMA_VERSION

    def __post_init__(self):
        if not self.strategy_id:
            raise StrategyIdentityError("strategy_id must be a non-empty string")
        if not self.version:
            raise StrategyIdentityError("version must be a non-empty string")
        if not _STRATEGY_ID_RE.fullmatch(self.strategy_id):
            raise StrategyIdentityError(
                f"strategy_id '{self.strategy_id}' must use namespaced versioned form, "
                "e.g. context.foo/v1"
            )
        identity_version = self.strategy_id.rsplit("/", 1)[1]
        if identity_version != self.version:
            raise StrategyIdentityError(
                f"strategy_id suffix '{identity_version}' must match version '{self.version}'"
            )
        self.immutable_config = safe_artifact_value(self.immutable_config)
        self.description = safe_artifact_value(self.description)

    @property
    def digest(self) -> str:
        """Deterministic SHA-256 digest over the strategy's identity fields."""
        payload = {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "strategy_type": self.strategy_type,
            "immutable_config": self.immutable_config,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["digest"] = self.digest
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyDefinition":
        d = dict(data)
        stored_digest = d.pop("digest", None)
        d.pop("schema", None)
        strategy = cls(schema=STRATEGY_SCHEMA_VERSION, **d)
        if stored_digest is not None and stored_digest != strategy.digest:
            raise ArtifactIntegrityError(
                f"Strategy '{strategy.strategy_id}' digest mismatch."
            )
        return strategy


# Canonical, code-defined reasoning strategies. No runtime code constructs a new
# StrategyDefinition from repository content. The canonical strategy data lives in
# a bundled YAML artifact so the Python source remains free of repetitive data
# blocks. JSON is a valid YAML subset, so the canonical compact serialization is
# retained. At import time the module verifies the contents against a pinned
# SHA-256 digest; tampering raises StrategyIdentityError.
_BUILT_IN_STRATEGIES_DIGEST = "ba8d2e418e3c1c5ac91ba753843a490328ac2512b12e75c59fe16050ff2e89e9"


def _load_builtin_strategies() -> List[StrategyDefinition]:
    data_path = Path(__file__).parent / "builtin_strategies.yaml"
    raw = data_path.read_text(encoding="utf-8")
    observed_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if observed_digest != _BUILT_IN_STRATEGIES_DIGEST:
        raise StrategyIdentityError(
            f"Builtin strategy digest mismatch: expected {_BUILT_IN_STRATEGIES_DIGEST}, "
            f"observed {observed_digest}. Repository content may not alter canonical strategies."
        )
    items = json.loads(raw)
    return [
        StrategyDefinition(
            strategy_id=item["strategy_id"],
            version="v1",
            strategy_type=item["strategy_type"],
            description=item["description"],
            immutable_config=item["immutable_config"],
        )
        for item in items
    ]


BUILTIN_STRATEGIES: List[StrategyDefinition] = _load_builtin_strategies()


class StrategyRegistry:
    """
    Code-defined registry of immutable reasoning strategies.

    Registering a strategy with the same (strategy_id, version) but a different
    digest is a hard error. This guarantees that a strategy ID/version always
    refers to exactly one definition.
    """

    def __init__(self, strategies: Optional[List[StrategyDefinition]] = None):
        self._strategies: Dict[Tuple[str, str], StrategyDefinition] = {}
        for s in (strategies if strategies is not None else BUILTIN_STRATEGIES):
            self.register(s)

    def register(self, strategy: StrategyDefinition) -> None:
        key = (strategy.strategy_id, strategy.version)
        existing = self._strategies.get(key)
        if existing is not None and existing.digest != strategy.digest:
            raise StrategyIdentityError(
                f"Strategy {strategy.strategy_id}@{strategy.version} already registered with "
                f"digest {existing.digest}; cannot redefine with digest {strategy.digest}."
            )
        self._strategies[key] = strategy

    def get(self, strategy_id: str, version: Optional[str] = None) -> Optional[StrategyDefinition]:
        if version is None and "/" in strategy_id:
            # strategy_id is expected to contain the version already, e.g. foo/v1
            version = strategy_id.split("/")[-1]
        key = (strategy_id, version or "v1")
        return self._strategies.get(key)

    def list_by_type(self, strategy_type: str) -> List[StrategyDefinition]:
        return [s for s in self._strategies.values() if s.strategy_type == strategy_type]

    def list_all(self) -> List[StrategyDefinition]:
        return list(self._strategies.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": STRATEGY_SCHEMA_VERSION,
            "strategies": [s.to_dict() for s in self._strategies.values()],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyRegistry":
        items = data.get("strategies", [])
        strategies = [StrategyDefinition.from_dict(item) for item in items]
        return cls(strategies=strategies)
