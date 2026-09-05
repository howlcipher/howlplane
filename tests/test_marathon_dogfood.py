#!/usr/bin/env python3
"""
test_marathon_dogfood.py

Unit and integration tests for MarathonDogfoodEngine: repeated benchmark synthesis,
provider exhaustion handling, and ledger recording.
"""

from pathlib import Path
import pytest

from src.control_plane.evidence_ledger import EvidenceLedger
from src.control_plane.synthesis.marathon import (
    MarathonDogfoodEngine,
    STANDARD_BENCHMARKS,
)
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)


def test_marathon_dogfood_runs_selected_benchmarks(tmp_path: Path):
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = EvidenceLedger(ledger_path)
    engine = MarathonDogfoodEngine(
        base_output_dir=tmp_path / "dogfood",
        campaign_dir=tmp_path / "campaigns",
        ledger=ledger,
    )

    report = engine.run_marathon(
        benchmarks=["notes", "todo"],
        max_iterations=2,
    )

    assert report.iterations_attempted == 2
    assert report.iterations_succeeded == 2
    assert report.iterations_failed == 0
    assert len(report.benchmark_results) == 2
    assert report.benchmark_results[0].benchmark_id == "notes"
    assert report.benchmark_results[0].success is True
    assert report.benchmark_results[1].benchmark_id == "todo"
    assert report.benchmark_results[1].success is True

    # Confirm ledger entries were appended
    entries = ledger.list_all_entries()
    assert len(entries) >= 2
    assert any("SYNTH-NOTES-APP" in e.task_id for e in entries)


def test_marathon_stops_when_all_providers_exhausted(tmp_path: Path):
    pool = ProviderPoolManager()
    for agent in ["agy", "codex", "claude_code", "devin_cli", "local_ollama", "gemini_cli"]:
        pool.set_status(agent, ProviderAvailabilityStatus.SESSION_EXHAUSTED)

    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        base_output_dir=tmp_path / "dogfood",
        campaign_dir=tmp_path / "campaigns",
    )
    report = engine.run_marathon(benchmarks=["notes", "todo"])

    assert report.iterations_attempted == 0
    assert report.stopped_reason == "all_providers_exhausted"
