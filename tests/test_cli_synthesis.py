#!/usr/bin/env python3
"""
test_cli_synthesis.py

Unit tests for CLI commands: ai create, ai run, and ai dogfood.
"""

import json
from pathlib import Path
import pytest

from src.control_plane.cli import build_parser, main


def test_cli_create_notes_app(tmp_path: Path, capsys):
    out_dir = tmp_path / "cli_notes"
    code = main([
        "create",
        "Create a persistent notes application with browser UI and JSON API.",
        "--output-dir", str(out_dir),
        "--port", "8095",
    ])

    captured = capsys.readouterr()
    assert code == 0
    assert "PRODUCT READY" in captured.out
    assert "Notes Application" in captured.out
    assert (out_dir / "build" / "backend.hfbc").exists()


def test_cli_create_json_output(tmp_path: Path, capsys):
    out_dir = tmp_path / "cli_json"
    code = main([
        "create",
        "Create a simple todo app.",
        "--output-dir", str(out_dir),
        "--port", "8096",
        "--json",
    ])

    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data["success"] is True
    assert data["product_name"] == "todo-app"
    assert data["status"] == "VERIFIED_PRODUCT"


def test_cli_dogfood_command(tmp_path: Path, capsys):
    out_dir = tmp_path / "dogfood_cli"
    code = main([
        "dogfood",
        "--benchmarks", "notes,todo",
        "--max-iterations", "2",
        "--output-dir", str(out_dir),
        # Without this the CLI default resolves `.dogfood_runs` against the
        # cwd and the test writes a campaign into the real repository.
        "--campaign-dir", str(tmp_path / "campaigns"),
    ])

    captured = capsys.readouterr()
    assert code == 0
    assert "HowlPlane Marathon Dogfooding Report" in captured.out
    assert "2/2 succeeded" in captured.out
