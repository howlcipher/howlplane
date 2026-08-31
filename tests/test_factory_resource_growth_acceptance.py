#!/usr/bin/env python3
"""Bounded-growth contracts for persistent operation."""

import inspect

import pytest

from src.control_plane.evidence_ledger import EvidenceLedger


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: EvidenceLedger has no bounded iterator, cursor, derived "
        "index, or retention interface and list_all_entries reads the full JSONL"
    ),
)
def test_evidence_ledger_has_bounded_query_surface_for_factory_status():
    bounded_methods = {
        "iter_entries",
        "query_entries",
        "list_recent_entries",
        "rebuild_index",
    }
    assert bounded_methods.intersection(dir(EvidenceLedger))
    source = inspect.getsource(EvidenceLedger.list_all_entries)
    assert ".readlines(" not in source
    assert ".read_text(" not in source
