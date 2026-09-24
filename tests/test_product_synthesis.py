#!/usr/bin/env python3
"""
test_product_synthesis.py

Integration tests for end-to-end prompt-to-product synthesis:
Natural language prompt -> ProductSpec -> Code Synthesis -> Compiler Verification ->
Black-box Acceptance -> Review -> Runnable Product Bundle.
"""

from pathlib import Path
import pytest

from howlplane.control_plane.synthesis import (
    NaturalLanguageSynthesizer,
    ProductSynthesizer,
    ProductBundle,
    SynthesisResult,
)


def test_end_to_end_notes_synthesis(tmp_path: Path):
    prompt = (
        "Create a persistent notes application. "
        "Users should be able to create, view, edit, and delete notes. "
        "Provide a browser interface and a JSON HTTP API. "
        "Notes must survive application restart. Reject invalid note data."
    )

    out_dir = tmp_path / "notes_app"
    engine = ProductSynthesizer()
    res = engine.create_from_prompt(prompt, output_dir=out_dir, port=8091)

    assert res.success is True
    assert res.status == "VERIFIED_PRODUCT"
    assert res.product_bundle is not None
    assert res.acceptance_report is not None
    assert res.acceptance_report.all_passed is True
    assert res.acceptance_report.passed_count >= 5

    # Check generated files in output bundle
    assert (out_dir / "app" / "backend.howl").exists()
    assert (out_dir / "app" / "frontend.howl").exists()
    assert (out_dir / "static" / "index.html").exists()
    assert (out_dir / "static" / "app.js").exists()
    assert (out_dir / "static" / "style.css").exists()
    assert (out_dir / "scripts" / "build.sh").exists()
    assert (out_dir / "scripts" / "run.sh").exists()
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "verification_summary.json").exists()
    assert (out_dir / "product_spec.yaml").exists()
    assert (out_dir / "README.md").exists()

    # Verify compiled bytecode exists
    assert (out_dir / "build" / "backend.hfbc").exists()


def test_end_to_end_todo_synthesis(tmp_path: Path):
    prompt = "Create a todo task manager with browser UI, API, and restart persistence."
    out_dir = tmp_path / "todo_app"
    engine = ProductSynthesizer()
    res = engine.create_from_prompt(prompt, output_dir=out_dir, port=8092)

    assert res.success is True
    assert res.status == "VERIFIED_PRODUCT"
    assert res.acceptance_report.all_passed is True
    assert (out_dir / "build" / "backend.hfbc").exists()


def test_synthesis_blocked_on_unsupported_framework_gap(tmp_path: Path):
    prompt = "Create an application with atomic shared counter increment across parallel threads."
    out_dir = tmp_path / "blocked_app"
    engine = ProductSynthesizer()
    res = engine.create_from_prompt(prompt, output_dir=out_dir)

    assert res.success is False
    assert res.status == "PRODUCT_BLOCKED"
    assert len(res.framework_gaps) >= 1
    assert res.framework_gaps[0].code == "HF_GAP_ATOMIC_MUTATION"
