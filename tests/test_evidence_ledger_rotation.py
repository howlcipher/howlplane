"""The evidence ledger rotates into compressed archives without losing or duplicating history."""

import gzip
import logging
import multiprocessing
import os

import pytest

from howlplane.control_plane import evidence_ledger
from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(evidence_ledger.fcntl is None, reason="rotation needs POSIX flock"),
]

# A few entries cross this, so short tests rotate several times.
SMALL = 2000


def _ledger(tmp_path, rotate_bytes=SMALL) -> EvidenceLedger:
    return EvidenceLedger(str(tmp_path / "ledger.jsonl"), rotate_bytes=rotate_bytes)


def _append(ledger, count, task_id="T1"):
    ids = []
    for _ in range(count):
        entry = EvidenceEntry(task_id=task_id, agent_id="a", action="task_created")
        ledger.append_entry(entry)
        ids.append(entry.entry_id)
    return ids


def _archives(directory):
    return sorted(p.name for p in directory.glob("ledger.jsonl.*.gz"))


def test_crossing_the_threshold_archives_the_active_file_and_keeps_every_entry_in_order(tmp_path):
    ledger = _ledger(tmp_path)
    ids = _append(ledger, 30)

    assert len(_archives(tmp_path)) >= 2
    assert not ledger.ledger_file.exists() or ledger.ledger_file.stat().st_size < SMALL
    assert [e.entry_id for e in ledger.list_all_entries()] == ids
    entries, diagnostics = ledger.read_entries(strict=True)
    assert [e.entry_id for e in entries] == ids and diagnostics == []
    assert [e.entry_id for e in ledger.get_task_entries("T1")] == ids


def test_an_archive_holds_exactly_the_bytes_that_were_rotated(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=0)
    _append(ledger, 3)
    original = ledger.ledger_file.read_bytes()

    archive = ledger.rotate()

    assert archive is not None and gzip.decompress(archive.read_bytes()) == original
    assert not ledger.ledger_file.exists()
    assert not list(tmp_path.glob("ledger.jsonl.*Z")), "the staged copy must be removed"


def test_below_the_threshold_or_with_rotation_disabled_nothing_is_archived(tmp_path):
    _append(_ledger(tmp_path / "large", rotate_bytes=10**6), 30)
    _append(_ledger(tmp_path / "off", rotate_bytes=0), 30)

    assert not list(tmp_path.rglob("*.gz"))


def test_appending_and_reading_leave_nothing_beside_the_ledger(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=10**6)
    _append(ledger, 3)
    ledger.read_entries()
    ledger.segment_files()

    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.jsonl"]


def test_rotating_a_missing_or_empty_ledger_creates_no_archive(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.rotate() is None
    ledger.ledger_file.touch()
    assert ledger.rotate() is None
    assert _archives(tmp_path) == []


def test_a_negative_threshold_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _ledger(tmp_path, rotate_bytes=-1)


def test_a_new_archive_sorts_after_existing_ones_even_if_the_clock_went_back(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=0)
    future = tmp_path / "ledger.jsonl.29991231T235959999999Z.gz"
    future.write_bytes(gzip.compress(b""))
    _append(ledger, 1)

    archive = ledger.rotate()

    assert _archives(tmp_path) == [future.name, archive.name]


def test_a_segment_staged_before_a_crash_is_read_and_then_compressed(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=0)
    first = _append(ledger, 2)
    staged = tmp_path / "ledger.jsonl.20260101T000000000000Z"
    os.replace(ledger.ledger_file, staged)  # the crash came right after the rename
    second = _append(ledger, 1)

    assert [e.entry_id for e in ledger.list_all_entries()] == first + second

    ledger.rotate()

    assert not staged.exists() and (tmp_path / (staged.name + ".gz")).exists()
    assert [e.entry_id for e in ledger.list_all_entries()] == first + second


def test_a_crash_after_compressing_never_duplicates_entries(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=0)
    ids = _append(ledger, 2)
    staged = tmp_path / "ledger.jsonl.20260101T000000000000Z"
    data = ledger.ledger_file.read_bytes()
    os.replace(ledger.ledger_file, staged)
    # The archive landed but the staged copy was not removed yet.
    (tmp_path / (staged.name + ".gz")).write_bytes(gzip.compress(data))
    # A different rotation died while writing its archive.
    torn = tmp_path / "ledger.jsonl.20260102T000000000000Z.gz.partial"
    torn.write_bytes(b"torn")

    assert [e.entry_id for e in ledger.list_all_entries()] == ids

    ids += _append(ledger, 1)
    ledger.rotate()

    assert not staged.exists() and not torn.exists()
    assert [e.entry_id for e in ledger.list_all_entries()] == ids


def test_a_corrupt_line_in_an_archive_is_reported_with_its_segment(tmp_path):
    ledger = _ledger(tmp_path, rotate_bytes=0)
    _append(ledger, 1)
    with open(ledger.ledger_file, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    archive = ledger.rotate()
    _append(ledger, 1)

    entries, diagnostics = ledger.read_entries()

    assert len(entries) == 2
    assert [(d.source, d.line_number, d.kind) for d in diagnostics] == [(archive.name, 2, "malformed")]


def test_every_file_a_rotating_ledger_leaves_is_recognised_as_a_ledger_file(tmp_path):
    ledger = _ledger(tmp_path)
    _append(ledger, 30)
    (tmp_path / "ledger.jsonl.20260102T000000000000Z").write_bytes(b"")
    (tmp_path / "ledger.jsonl.20260103T000000000000Z.gz.partial").write_bytes(b"")

    owned = sorted(p.name for p in tmp_path.iterdir())

    assert any(name.endswith(".gz") for name in owned)
    assert [name for name in owned if not evidence_ledger.is_ledger_file(name)] == []
    for name in ("ledger.json", "src/app.py", "ledger.jsonl.bak", "ledger.jsonl.lock", "notes.jsonl.txt"):
        assert not evidence_ledger.is_ledger_file(name)


def test_a_failed_rotation_is_logged_and_the_append_still_succeeds(tmp_path, monkeypatch, caplog):
    ledger = _ledger(tmp_path)

    def fail(staged):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "_compress", fail)
    with caplog.at_level(logging.ERROR, logger=evidence_ledger.__name__):
        ids = _append(ledger, 30)

    assert "could not rotate" in caplog.text
    assert [e.entry_id for e in ledger.list_all_entries()] == ids


def _append_from_another_process(path, task_id, count):
    ledger = EvidenceLedger(path, rotate_bytes=SMALL)
    for _ in range(count):
        ledger.append_entry(EvidenceEntry(task_id=task_id, agent_id="a", action="task_created"))


def test_concurrent_writers_lose_and_duplicate_nothing_across_rotations(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    context = multiprocessing.get_context("forkserver")
    workers = [
        context.Process(target=_append_from_another_process, args=(path, f"W{i}", 40)) for i in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
    entries, _ = EvidenceLedger(path, rotate_bytes=SMALL).read_entries(strict=True)
    ids = [e.entry_id for e in entries]
    assert len(ids) == 160 and len(set(ids)) == 160
    for i in range(4):
        stamps = [e.timestamp for e in entries if e.task_id == f"W{i}"]
        assert len(stamps) == 40 and stamps == sorted(stamps), "each writer's entries stay in its order"
    assert len(_archives(tmp_path)) >= 2
