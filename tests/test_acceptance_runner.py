#!/usr/bin/env python3
"""
test_acceptance_runner.py

Unit tests for ProductAcceptanceRunner: compilation verification, static assets,
health probes, CRUD operations, input validation rejection, and restart persistence.
"""

from pathlib import Path
import pytest

from howlplane.control_plane.synthesis.acceptance_runner import (
    AcceptanceCheckResult,
    ProductAcceptanceReport,
    ProductAcceptanceRunner,
)
from howlplane.control_plane.synthesis.product_spec import (
    BehaviorSpec,
    EntitySpec,
    FieldSpec,
    PersistenceSpec,
    ProductSpec,
)


def test_acceptance_report_rendering():
    report = ProductAcceptanceReport(
        product_name="notes-app",
        all_passed=True,
        passed_count=6,
        total_count=6,
        duration_seconds=1.45,
        checks=[
            AcceptanceCheckResult(name="build_compilation", description="Compile bytecode", status="passed", duration_seconds=0.05, evidence="Exit 0"),
            AcceptanceCheckResult(name="server_health_probe", description="Probe /health", status="passed", duration_seconds=0.02, evidence="HTTP 200"),
            AcceptanceCheckResult(name="create_entity_crud", description="POST note", status="passed", duration_seconds=0.03, evidence="Created 1"),
            AcceptanceCheckResult(name="list_entities_crud", description="GET notes", status="passed", duration_seconds=0.01, evidence="List len 1"),
            AcceptanceCheckResult(name="input_validation_rejection", description="Reject bad payload", status="passed", duration_seconds=0.02, evidence="HTTP 400"),
            AcceptanceCheckResult(name="restart_persistence", description="Survives reboot", status="passed", duration_seconds=0.15, evidence="Retained"),
        ],
    )

    md = report.render_markdown()
    assert "# Acceptance Verification: notes-app" in md
    assert "✓ PASSED" in md
    assert "6/6 checks passed" in md
    assert "restart_persistence" in md


def test_acceptance_runner_missing_build_script(tmp_path: Path):
    runner = ProductAcceptanceRunner()
    spec = ProductSpec(
        name="test-app",
        title="Test App",
        description="A test app",
        acceptance_criteria=["Builds ok"],
    )

    report = runner.run_acceptance_suite(tmp_path, spec)
    assert report.all_passed is False
    assert report.checks[0].name == "build_script_exists"
    assert report.checks[0].status == "failed"
