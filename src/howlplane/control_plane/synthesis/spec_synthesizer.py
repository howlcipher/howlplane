#!/usr/bin/env python3
"""
spec_synthesizer.py

Synthesizes structured, deterministic ProductSpec instances from natural-language
intent descriptions. Extracts entities, behaviors, validation constraints,
persistence requirements, and observable acceptance criteria.
"""

from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from howlplane.control_plane.synthesis.product_spec import (
    BehaviorSpec,
    EntitySpec,
    FieldSpec,
    PersistenceSpec,
    ProductSpec,
    ValidationRuleSpec,
)


class NaturalLanguageSynthesizer:
    """
    Parses natural language product intent into a machine-checkable ProductSpec.
    """

    def synthesize(self, prompt: str, name_override: Optional[str] = None) -> ProductSpec:
        """
        Converts a natural language prompt into a validated ProductSpec.
        """
        clean_prompt = prompt.strip()
        lowered = clean_prompt.lower()

        # 1. Infer Product Name and Title
        name, title = self._infer_name_and_title(clean_prompt, lowered, name_override)

        # 2. Infer Interfaces
        interfaces = self._infer_interfaces(lowered)

        # 3. Infer Entities and Fields
        entities = self._infer_entities(lowered, name)

        # 4. Infer Behaviors
        behaviors = self._infer_behaviors(lowered, entities)

        # 5. Infer Persistence
        persistence = self._infer_persistence(lowered, name)

        # 6. Infer Validation Rules
        validation_rules = self._infer_validation_rules(lowered, entities)

        # 7. Generate Machine-Checkable Acceptance Criteria
        acceptance_criteria = self._generate_acceptance_criteria(
            entities=entities,
            behaviors=behaviors,
            interfaces=interfaces,
            persistence=persistence,
            validation_rules=validation_rules,
        )

        # 8. Assemble Required Capabilities
        caps = ["database", "filesystem"] if persistence.type == "local_store" else ["database"]
        if "http_api" in interfaces or "browser_ui" in interfaces:
            caps.append("network")

        inferred_defaults = {
            "inferred_from_prompt": clean_prompt,
            "default_port": 8088,
            "primary_entity": list(entities.keys())[0] if entities else "item",
        }

        spec = ProductSpec(
            name=name,
            title=title,
            description=clean_prompt,
            interfaces=interfaces,
            entities=entities,
            behaviors=behaviors,
            persistence=persistence,
            validation_rules=validation_rules,
            acceptance_criteria=acceptance_criteria,
            capabilities_required=sorted(list(set(caps))),
            default_port=8088,
            metadata={"synthesis": inferred_defaults},
        )

        validation_errors = spec.validate()
        if validation_errors:
            # Auto-repair minor validation inconsistencies
            for err in validation_errors:
                if "primary key" in err:
                    for ent in spec.entities.values():
                        if ent.primary_key not in ent.fields:
                            ent.fields[ent.primary_key] = FieldSpec(name=ent.primary_key, type="string", required=True)
            # Re-validate
            remaining_errors = spec.validate()
            if remaining_errors:
                raise ValueError(f"Synthesized ProductSpec validation failed: {'; '.join(remaining_errors)}")

        return spec

    def _infer_name_and_title(self, prompt: str, lowered: str, override: Optional[str]) -> Tuple[str, str]:
        if override:
            slug = re.sub(r"[^a-z0-9_-]", "-", override.lower()).strip("-")
            title = override.replace("-", " ").replace("_", " ").title()
            return slug or "app", title

        if "note" in lowered:
            return "notes-app", "Notes Application"
        elif "todo" in lowered or "task" in lowered:
            return "todo-app", "Todo Application"
        elif "inventory" in lowered or "catalog" in lowered:
            return "inventory-app", "Inventory Application"
        elif "status" in lowered or "health" in lowered or "monitor" in lowered:
            return "status-api", "Status and Health API"
        elif "kv" in lowered or "key-value" in lowered or "store" in lowered:
            return "kv-store", "Key-Value Store"
        elif "transform" in lowered or "convert" in lowered:
            return "json-transformer", "JSON Transformer"

        # Fallback heuristic from first few words
        words = re.findall(r"[a-z0-9]+", lowered)
        slug_words = [w for w in words if w not in ("create", "a", "an", "the", "with", "and", "app", "application")][:3]
        slug = "-".join(slug_words) if slug_words else "app"
        return f"{slug}-app", slug.replace("-", " ").title() + " Application"

    def _infer_interfaces(self, lowered: str) -> List[str]:
        ifaces: List[str] = []
        has_browser = any(k in lowered for k in ["browser", "ui", "web", "frontend", "html", "gui"])
        has_api = any(k in lowered for k in ["api", "http", "json", "rest", "endpoint", "server", "crud"])
        has_cli = any(k in lowered for k in ["cli", "command line", "terminal", "console"])

        if has_browser or (not has_api and not has_cli):
            ifaces.append("browser_ui")
        if has_api or has_browser or not has_cli:
            ifaces.append("http_api")
        if has_cli and not has_browser:
            ifaces.append("cli")

        if not ifaces:
            ifaces = ["browser_ui", "http_api"]
        return ifaces

    def _infer_entities(self, lowered: str, app_name: str) -> Dict[str, EntitySpec]:
        entities: Dict[str, EntitySpec] = {}
        entity_templates = {
            "note": ("Note", "A persistent text note with title and content.", {
                "id": FieldSpec(name="id", type="string", required=True, description="Unique note identifier"),
                "title": FieldSpec(name="title", type="string", required=True, bounded=200, min_length=1, description="Note title"),
                "content": FieldSpec(name="content", type="string", required=True, bounded=4000, description="Note body text"),
                "created_at": FieldSpec(name="created_at", type="string", required=False, description="ISO timestamp"),
                "updated_at": FieldSpec(name="updated_at", type="string", required=False, description="ISO timestamp"),
            }),
            "todo": ("Task", "A todo task with title and completion status.", {
                "id": FieldSpec(name="id", type="string", required=True),
                "title": FieldSpec(name="title", type="string", required=True, bounded=200, min_length=1),
                "completed": FieldSpec(name="completed", type="bool", required=False, default=False),
                "created_at": FieldSpec(name="created_at", type="string", required=False),
            }),
            "inventory": ("Item", "An inventory item with quantity and price.", {
                "id": FieldSpec(name="id", type="string", required=True),
                "name": FieldSpec(name="name", type="string", required=True, bounded=200),
                "quantity": FieldSpec(name="quantity", type="int", required=True, default=0),
                "category": FieldSpec(name="category", type="string", required=False, default="general"),
            }),
            "status": ("ServiceStatus", "Service health and metric observation.", {
                "id": FieldSpec(name="id", type="string", required=True),
                "service": FieldSpec(name="service", type="string", required=True),
                "status": FieldSpec(name="status", type="string", required=True),
                "timestamp": FieldSpec(name="timestamp", type="string", required=False),
            }),
            "generic": ("Record", "Generic structured domain record.", {
                "id": FieldSpec(name="id", type="string", required=True),
                "record_name": FieldSpec(name="record_name", type="string", required=True, bounded=200),
                "payload": FieldSpec(name="payload", type="string", required=True),
                "timestamp": FieldSpec(name="timestamp", type="string", required=False),
            }),
        }

        key = "generic"
        if any(k in lowered for k in ("note", "memo")) or app_name == "notes-app":
            key = "note"
        elif any(k in lowered for k in ("todo", "task")) or app_name == "todo-app":
            key = "todo"
        elif "inventory" in lowered or app_name == "inventory-app":
            key = "inventory"
        elif "status" in lowered or app_name == "status-api":
            key = "status"

        ent_name, desc, fields = entity_templates[key]
        entities[ent_name] = EntitySpec(
            name=ent_name,
            description=desc,
            primary_key="id",
            fields=fields,
        )
        return entities

    def _infer_behaviors(self, lowered: str, entities: Dict[str, EntitySpec]) -> List[BehaviorSpec]:
        behaviors: List[BehaviorSpec] = []
        ent_name = list(entities.keys())[0] if entities else "Item"
        slug_ent = ent_name.lower() + "s"

        # Always provide health check
        behaviors.append(BehaviorSpec(
            name="health",
            entity="system",
            description="System health probe",
            endpoint="/health",
            method="GET",
            output_type="dict",
        ))

        # CRUD behaviors
        behaviors.append(BehaviorSpec(
            name=f"list_{slug_ent}",
            entity=ent_name,
            description=f"Retrieve all {ent_name} records",
            endpoint=f"/api/{slug_ent}",
            method="GET",
            output_type="list",
        ))
        crud_table = [
            ("create", f"Create a new {ent_name} record", "POST", [f for f in entities[ent_name].fields.keys() if f != "id"]),
            ("get", f"Retrieve a single {ent_name} record by ID", "GET", ["id"]),
            ("update", f"Update an existing {ent_name} record", "PUT", list(entities[ent_name].fields.keys())),
            ("delete", f"Delete a {ent_name} record by ID", "DELETE", ["id"]),
        ]
        for op, desc, method, in_fields in crud_table:
            behaviors.append(BehaviorSpec(
                name=f"{op}_{ent_name.lower()}",
                entity=ent_name,
                description=desc,
                endpoint=f"/api/{slug_ent}",
                method=method,
                input_fields=in_fields,
                output_type="dict",
            ))

        return behaviors

    def _infer_persistence(self, lowered: str, app_name: str) -> PersistenceSpec:
        if "memory" in lowered and "restart" not in lowered:
            return PersistenceSpec(type="memory_store", storage_path="memory://data", survives_restart=False)

        # Default is local file store with restart persistence
        ent_slug = app_name.replace("-app", "").replace("-api", "")
        return PersistenceSpec(
            type="local_store",
            storage_path=f"file://data/{ent_slug}.json",
            survives_restart=True,
            record_key_prefix=f"{ent_slug}:",
        )

    def _infer_validation_rules(self, lowered: str, entities: Dict[str, EntitySpec]) -> List[ValidationRuleSpec]:
        rules: List[ValidationRuleSpec] = []
        for ent_name, ent in entities.items():
            for f_name, f_spec in ent.fields.items():
                if f_spec.required and f_name != "id":
                    rules.append(ValidationRuleSpec(
                        entity=ent_name,
                        field=f_name,
                        rule="required",
                        error_message=f"Field '{f_name}' is required.",
                    ))
                if f_spec.bounded:
                    rules.append(ValidationRuleSpec(
                        entity=ent_name,
                        field=f_name,
                        rule=f"max_length:{f_spec.bounded}",
                        error_message=f"Field '{f_name}' exceeds maximum length of {f_spec.bounded}.",
                    ))
        return rules

    def _generate_acceptance_criteria(
        self,
        entities: Dict[str, EntitySpec],
        behaviors: List[BehaviorSpec],
        interfaces: List[str],
        persistence: PersistenceSpec,
        validation_rules: List[ValidationRuleSpec],
    ) -> List[str]:
        criteria = [
            "Application builds and compiles to valid HowlFrame bytecode and static assets with exit 0",
            "HTTP server starts successfully and responds to /health probe with 200 OK",
        ]
        if "browser_ui" in interfaces:
            criteria.append("Root browser interface '/' serves valid HTML and references compiled static app.js")

        ent_name = list(entities.keys())[0] if entities else "Record"
        slug_ent = ent_name.lower() + "s"

        criteria.append(f"Creating a new {ent_name} via POST /api/{slug_ent} persists record and returns 200/201")
        criteria.append(f"Retrieving {ent_name} list via GET /api/{slug_ent} includes created item")
        criteria.append(f"Retrieving single {ent_name} by ID returns matching record attributes")
        criteria.append(f"Updating {ent_name} via PUT/POST /api/{slug_ent} updates record in-place")
        criteria.append(f"Deleting {ent_name} via DELETE /api/{slug_ent} removes record from store")

        if validation_rules:
            criteria.append(f"Submitting invalid {ent_name} payload (missing required field or empty) is rejected with 400 Bad Request")

        if persistence.survives_restart:
            criteria.append(f"Application state persists across process restart using {persistence.storage_path}")

        return criteria
