"""Real backlog context must survive discovery and durable admission."""
from types import SimpleNamespace

import pytest

from src.control_plane.cli import _build_factory_supervisor
from tests.test_backlog_marathon import RANKED


def build(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'bugs.md').write_text(RANKED)
    return _build_factory_supervisor(SimpleNamespace(
        state_dir=str(tmp_path / 'state'), target_repo=str(repo),
        authority_profile=None,
    ))


def test_backlog_detail_survives_factory_admission(tmp_path):
    supervisor = build(tmp_path)
    supervisor._ingest_discovered()
    item = next(w for w in supervisor.work_item_store.list_all() if w.source_rank == 51)
    assert '**Deterministic acceptance:** the thing is not broken.' in item.description
    assert 'another thing is broken' not in item.description
    item.description = 'Previously admitted context must not be silently replaced.'
    supervisor.work_item_store.save_object(item)
    supervisor._ingest_discovered()
    assert supervisor.work_item_store.load(item.work_item_id).description == item.description


@pytest.mark.parametrize('state', ['ready', 'awaiting_owner', 'in_progress', 'verifying', 'shipped'])
def test_legacy_context_backfill_does_not_reopen_or_mutate_active_work(tmp_path, state):
    supervisor = build(tmp_path)
    supervisor._ingest_discovered()
    item = next(w for w in supervisor.work_item_store.list_all() if w.source_rank == 51)
    item.description = ''
    item.state = state
    supervisor.work_item_store.save_object(item)
    before = item.to_dict()
    decisions = len(supervisor.state_record.admission_decisions)
    supervisor._ingest_discovered()
    after = supervisor.work_item_store.load(item.work_item_id)
    assert after.state == state
    assert after.reopening_history == item.reopening_history
    assert len(supervisor.state_record.admission_decisions) == decisions
    if state in ('ready', 'awaiting_owner'):
        assert '**Deterministic acceptance:** the thing is not broken.' in after.description
    else:
        assert after.to_dict() == before
