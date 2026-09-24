#!/usr/bin/env python3
"""
test_spec_synthesizer.py

Unit tests for NaturalLanguageSynthesizer: converting natural language intent
into structured, deterministic ProductSpec instances with observable acceptance criteria.
"""

import pytest

from howlplane.control_plane.synthesis.spec_synthesizer import NaturalLanguageSynthesizer


def test_synthesize_notes_prompt():
    prompt = (
        "Create a persistent notes application with a browser UI, "
        "JSON API, CRUD, validation, and restart persistence."
    )
    synthesizer = NaturalLanguageSynthesizer()
    spec = synthesizer.synthesize(prompt)

    assert spec.name == "notes-app"
    assert spec.title == "Notes Application"
    assert "browser_ui" in spec.interfaces
    assert "http_api" in spec.interfaces
    assert "Note" in spec.entities
    assert "title" in spec.entities["Note"].fields
    assert "content" in spec.entities["Note"].fields
    assert spec.persistence.type == "local_store"
    assert spec.persistence.survives_restart is True
    assert spec.validate() == []
    assert any("restart" in c.lower() for c in spec.acceptance_criteria)
    assert any("health" in c.lower() for c in spec.acceptance_criteria)


def test_synthesize_todo_prompt():
    prompt = "Create a todo task manager with browser UI, API, status filtering, and persistence."
    synthesizer = NaturalLanguageSynthesizer()
    spec = synthesizer.synthesize(prompt)

    assert spec.name == "todo-app"
    assert "Task" in spec.entities
    assert "completed" in spec.entities["Task"].fields
    assert spec.validate() == []


def test_synthesize_status_api_prompt():
    prompt = "Create a service status and health check API with monitoring dashboard."
    synthesizer = NaturalLanguageSynthesizer()
    spec = synthesizer.synthesize(prompt)

    assert spec.name == "status-api"
    assert "ServiceStatus" in spec.entities
    assert spec.validate() == []


def test_synthesize_inventory_prompt():
    prompt = "Create an inventory tracking application with item quantities and local store."
    synthesizer = NaturalLanguageSynthesizer()
    spec = synthesizer.synthesize(prompt)

    assert spec.name == "inventory-app"
    assert "Item" in spec.entities
    assert "quantity" in spec.entities["Item"].fields
    assert spec.validate() == []
