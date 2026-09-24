#!/usr/bin/env python3
"""
product_spec.py

Small, deterministic, machine-checkable specification of a user's requested
software product outcome. Captures domain intent, entities, behaviors,
persistence models, interfaces, validation rules, and acceptance contracts
without prescribing implementation source files or syntax details.
"""

from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, List, Optional, Union
import yaml

from howlplane.control_plane.task_spec import DataClassSerializationMixin

PRODUCT_SPEC_SCHEMA_VERSION = "howlplane.product_spec/v1"


@dataclass
class FieldSpec(DataClassSerializationMixin):
    """Specification of an entity field / property."""

    name: str
    type: str = "string"  # "string", "int", "bool", "float", "datetime", "list", "dict"
    required: bool = True
    bounded: Optional[int] = None  # max length for strings or max value for ints
    min_length: Optional[int] = None
    default: Optional[Any] = None
    description: str = ""


@dataclass
class EntitySpec(DataClassSerializationMixin):
    """Specification of a domain entity."""

    name: str
    description: str = ""
    fields: Dict[str, FieldSpec] = field(default_factory=dict)
    primary_key: str = "id"


@dataclass
class BehaviorSpec(DataClassSerializationMixin):
    """Specification of an observable behavioral capability / endpoint."""

    name: str  # "create", "read", "update", "delete", "list", "health", etc.
    entity: str  # Entity name or "system"
    description: str = ""
    endpoint: str = ""  # e.g. "/api/notes"
    method: str = "GET"  # "GET", "POST", "PUT", "DELETE"
    input_fields: List[str] = field(default_factory=list)
    output_type: str = "dict"
    requires_auth: bool = False


@dataclass
class PersistenceSpec(DataClassSerializationMixin):
    """Specification of application state persistence requirements."""

    type: str = "local_store"  # "local_store", "memory_store", "none"
    storage_path: str = "data/store.json"
    survives_restart: bool = True
    record_key_prefix: str = ""


@dataclass
class ValidationRuleSpec(DataClassSerializationMixin):
    """Specification of explicit input validation constraints."""

    entity: str
    field: str
    rule: str  # "required", "max_length:<n>", "min_length:<n>", "non_empty", "regex:<pattern>"
    error_message: str


@dataclass
class ProductSpec(DataClassSerializationMixin):
    """
    Structured, machine-checkable product specification representing
    an application by desired outcome and observable behavior.
    """

    name: str
    title: str
    description: str
    interfaces: List[str] = field(default_factory=lambda: ["browser_ui", "http_api"])
    entities: Dict[str, EntitySpec] = field(default_factory=dict)
    behaviors: List[BehaviorSpec] = field(default_factory=list)
    persistence: PersistenceSpec = field(default_factory=PersistenceSpec)
    validation_rules: List[ValidationRuleSpec] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    capabilities_required: List[str] = field(default_factory=lambda: ["network", "database", "filesystem"])
    default_port: int = 8088
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: str = PRODUCT_SPEC_SCHEMA_VERSION

    def validate(self) -> List[str]:
        """
        Deterministically checks product specification consistency and invariants.
        Returns a list of validation error strings (empty if fully valid).
        """
        errors: List[str] = []
        if not self.name or not re.match(r"^[a-z0-9_-]+$", self.name):
            errors.append("Product name must be a non-empty alphanumeric slug (lowercase, digits, underscores, dashes).")

        if not self.title.strip():
            errors.append("Product title cannot be empty.")

        if not self.description.strip():
            errors.append("Product description cannot be empty.")

        if not self.interfaces:
            errors.append("Product must declare at least one interface (e.g. 'browser_ui', 'http_api', 'cli').")

        for iface in self.interfaces:
            if iface not in ("browser_ui", "http_api", "cli", "wasm"):
                errors.append(f"Unknown interface type '{iface}'.")

        for ent_name, ent in self.entities.items():
            if ent.name != ent_name:
                errors.append(f"Entity key '{ent_name}' does not match entity name '{ent.name}'.")
            if not ent.fields:
                errors.append(f"Entity '{ent_name}' must declare at least one field.")
            if ent.primary_key not in ent.fields:
                errors.append(f"Entity '{ent_name}' primary key '{ent.primary_key}' is not declared in fields.")

        for b in self.behaviors:
            if b.entity != "system" and b.entity not in self.entities:
                errors.append(f"Behavior '{b.name}' references unknown entity '{b.entity}'.")

        for v in self.validation_rules:
            if v.entity not in self.entities:
                errors.append(f"Validation rule references unknown entity '{v.entity}'.")
            elif v.field not in self.entities[v.entity].fields:
                errors.append(f"Validation rule references unknown field '{v.field}' on entity '{v.entity}'.")

        if not self.acceptance_criteria:
            errors.append("Product specification must define at least one acceptance criterion.")

        return errors

    def to_yaml(self) -> str:
        """Serializes ProductSpec to clean YAML string."""
        return yaml.dump(self.to_dict(), sort_keys=False)

    @classmethod
    def from_yaml(cls, yaml_content: str) -> "ProductSpec":
        """Deserializes ProductSpec from YAML string."""
        data = yaml.safe_load(yaml_content)
        return cls.from_dict(data)

    @classmethod
    def from_file(cls, path: Union[str, Any]) -> "ProductSpec":
        """Loads ProductSpec from a YAML or JSON file."""
        from pathlib import Path
        p = Path(path)
        content = p.read_text(encoding="utf-8")
        if p.suffix in (".json",):
            return cls.from_dict(json.loads(content))
        return cls.from_yaml(content)

    def save_to_file(self, path: Union[str, Any]) -> None:
        """Saves ProductSpec to a YAML or JSON file atomically."""
        from pathlib import Path
        from howlplane.control_plane.atomic_io import atomic_write_text
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".json":
            atomic_write_text(p, json.dumps(self.to_dict(), indent=2))
        else:
            atomic_write_text(p, self.to_yaml())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProductSpec":
        entities_dict: Dict[str, EntitySpec] = {}
        for k, v in data.get("entities", {}).items():
            if isinstance(v, dict):
                fields_dict: Dict[str, FieldSpec] = {}
                for fk, fv in v.get("fields", {}).items():
                    if isinstance(fv, dict):
                        fields_dict[fk] = FieldSpec.from_dict(fv)
                    elif isinstance(fv, str):
                        fields_dict[fk] = FieldSpec(name=fk, type=fv)
                    elif isinstance(fv, FieldSpec):
                        fields_dict[fk] = fv
                entities_dict[k] = EntitySpec(
                    name=v.get("name", k),
                    description=v.get("description", ""),
                    fields=fields_dict,
                    primary_key=v.get("primary_key", "id"),
                )
            elif isinstance(v, EntitySpec):
                entities_dict[k] = v

        behaviors_list: List[BehaviorSpec] = []
        for b in data.get("behaviors", []):
            if isinstance(b, dict):
                behaviors_list.append(BehaviorSpec.from_dict(b))
            elif isinstance(b, BehaviorSpec):
                behaviors_list.append(b)

        p_raw = data.get("persistence", {})
        persistence = PersistenceSpec.from_dict(p_raw) if isinstance(p_raw, dict) else (
            p_raw if isinstance(p_raw, PersistenceSpec) else PersistenceSpec()
        )

        validation_list: List[ValidationRuleSpec] = []
        for v in data.get("validation_rules", []):
            if isinstance(v, dict):
                validation_list.append(ValidationRuleSpec.from_dict(v))
            elif isinstance(v, ValidationRuleSpec):
                validation_list.append(v)

        return cls(
            name=data.get("name", "app"),
            title=data.get("title", data.get("name", "App")),
            description=data.get("description", ""),
            interfaces=data.get("interfaces", ["browser_ui", "http_api"]),
            entities=entities_dict,
            behaviors=behaviors_list,
            persistence=persistence,
            validation_rules=validation_list,
            acceptance_criteria=data.get("acceptance_criteria", []),
            capabilities_required=data.get("capabilities_required", ["network", "database", "filesystem"]),
            default_port=int(data.get("default_port", 8088)),
            metadata=data.get("metadata", {}),
            schema=data.get("schema", PRODUCT_SPEC_SCHEMA_VERSION),
        )
