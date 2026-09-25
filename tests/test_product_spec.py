#!/usr/bin/env python3
"""
test_product_spec.py

Unit tests for ProductSpec schema validation, serialization, and invariants.
"""

from pathlib import Path
import pytest
import yaml

from howlplane.control_plane.synthesis.product_spec import (
    BehaviorSpec,
    EntitySpec,
    FieldSpec,
    PersistenceSpec,
    ProductSpec,
    ValidationRuleSpec,
)


def test_product_spec_creation_and_validation():
    spec = ProductSpec(
        name="notes-app",
        title="Notes Application",
        description="A lightweight persistent notes application.",
        interfaces=["browser_ui", "http_api"],
        entities={
            "Note": EntitySpec(
                name="Note",
                description="A text note.",
                primary_key="id",
                fields={
                    "id": FieldSpec(name="id", type="string", required=True),
                    "title": FieldSpec(name="title", type="string", required=True, bounded=200),
                    "content": FieldSpec(name="content", type="string", required=True),
                },
            )
        },
        behaviors=[
            BehaviorSpec(name="health", entity="system", description="Health check", endpoint="/health", method="GET"),
            BehaviorSpec(name="create_note", entity="Note", description="Create note", endpoint="/api/notes", method="POST"),
            BehaviorSpec(name="list_notes", entity="Note", description="List notes", endpoint="/api/notes", method="GET"),
        ],
        persistence=PersistenceSpec(type="local_store", storage_path="data/notes.json", survives_restart=True),
        validation_rules=[
            ValidationRuleSpec(entity="Note", field="title", rule="required", error_message="Title is required"),
        ],
        acceptance_criteria=[
            "Application compiles cleanly to HowlFrame bytecode",
            "Health probe returns 200 OK",
            "CRUD operations work over REST API",
            "Notes persist across restart",
        ],
    )

    errors = spec.validate()
    assert errors == []
    assert spec.default_port == 8088
    assert "network" in spec.capabilities_required


def test_product_spec_validation_catches_invalid_fields():
    # Invalid slug name
    spec = ProductSpec(
        name="Invalid Name With Spaces!",
        title="",
        description="",
        interfaces=["unsupported_interface"],
        acceptance_criteria=[],
    )
    errors = spec.validate()
    assert any("Product name must be a non-empty alphanumeric slug" in e for e in errors)
    assert any("Product title cannot be empty" in e for e in errors)
    assert any("Product description cannot be empty" in e for e in errors)
    assert any("Unknown interface type 'unsupported_interface'" in e for e in errors)
    assert any("Product specification must define at least one acceptance criterion" in e for e in errors)


def test_product_spec_entity_validation():
    # Missing primary key field
    spec = ProductSpec(
        name="test-app",
        title="Test App",
        description="A test app.",
        interfaces=["http_api"],
        entities={
            "User": EntitySpec(
                name="User",
                primary_key="uuid",
                fields={
                    "name": FieldSpec(name="name", type="string"),
                },
            )
        },
        acceptance_criteria=["Starts ok"],
    )
    errors = spec.validate()
    assert any("primary key 'uuid' is not declared in fields" in e for e in errors)


def test_product_spec_serialization_roundtrip(tmp_path: Path):
    spec = ProductSpec(
        name="todo-app",
        title="Todo App",
        description="Todo application.",
        interfaces=["browser_ui", "http_api"],
        entities={
            "Task": EntitySpec(
                name="Task",
                primary_key="id",
                fields={
                    "id": FieldSpec(name="id", type="string"),
                    "title": FieldSpec(name="title", type="string"),
                },
            )
        },
        behaviors=[
            BehaviorSpec(name="list_tasks", entity="Task", endpoint="/api/tasks", method="GET"),
        ],
        persistence=PersistenceSpec(type="local_store", storage_path="data/todos.json"),
        acceptance_criteria=["CRUD works"],
    )

    # YAML roundtrip
    yaml_str = spec.to_yaml()
    loaded_spec = ProductSpec.from_yaml(yaml_str)
    assert loaded_spec.name == spec.name
    assert loaded_spec.title == spec.title
    assert "Task" in loaded_spec.entities
    assert loaded_spec.entities["Task"].fields["title"].type == "string"

    # File save and load
    file_path = tmp_path / "product_spec.yaml"
    spec.save_to_file(file_path)
    from_disk = ProductSpec.from_file(file_path)
    assert from_disk.name == spec.name
    assert from_disk.validate() == []
