#!/usr/bin/env python3
"""
test_capability_negotiator.py

Unit tests for HowlFrame capability negotiation, feasibility checking,
and structured framework gap classification.
"""

import pytest

from howlplane.control_plane.synthesis.capability_negotiator import (
    CapabilityNegotiator,
    FeasibilityStatus,
    FrameworkGap,
    HowlFrameCapabilityRegistry,
)
from howlplane.control_plane.synthesis.product_spec import (
    BehaviorSpec,
    EntitySpec,
    FieldSpec,
    PersistenceSpec,
    ProductSpec,
)


def test_feasible_product_negotiation():
    spec = ProductSpec(
        name="notes-app",
        title="Notes App",
        description="A notes app with local json persistence and browser UI.",
        interfaces=["browser_ui", "http_api"],
        entities={
            "Note": EntitySpec(
                name="Note",
                fields={"id": FieldSpec("id"), "title": FieldSpec("title")},
            )
        },
        behaviors=[
            BehaviorSpec(name="create_note", entity="Note", endpoint="/api/notes", method="POST"),
        ],
        persistence=PersistenceSpec(type="local_store", storage_path="data/notes.json"),
        acceptance_criteria=["Starts and runs"],
    )

    negotiator = CapabilityNegotiator()
    res = negotiator.negotiate(spec)

    assert res.status == FeasibilityStatus.FEASIBLE
    assert res.is_feasible is True
    assert "network" in res.granted_capabilities
    assert "database" in res.granted_capabilities
    assert "filesystem" in res.granted_capabilities
    assert res.framework_gaps == []
    assert res.recommended_architecture["root_form"] == "http_server"


@pytest.mark.parametrize(
    "name,title,desc,expected_gap",
    [
        ("counter-app", "Atomic Counter App", "An application requiring atomic shared counter mutation across parallel threads.", "HF_GAP_ATOMIC_MUTATION"),
        ("chat-stream", "Chat Stream", "A real-time raw websocket streaming server for live chat.", "HF_GAP_WEBSOCKET"),
        ("sql-service", "SQL Service", "Connects to an external Postgres database cluster for ACID transactions.", "HF_GAP_DISTRIBUTED_DB"),
    ],
)
def test_infeasible_capability_gaps(name: str, title: str, desc: str, expected_gap: str):
    spec = ProductSpec(
        name=name,
        title=title,
        description=desc,
        interfaces=["http_api"],
        acceptance_criteria=["Check feasibility"],
    )

    negotiator = CapabilityNegotiator()
    res = negotiator.negotiate(spec)

    assert res.status == FeasibilityStatus.INFEASIBLE
    assert res.is_feasible is False
    assert any(g.code == expected_gap and g.blocking for g in res.framework_gaps)
