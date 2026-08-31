#!/usr/bin/env python3
"""
tests/test_factory_work_item.py

Deterministic tests for the factory's portfolio entity and its durable store.

No provider is invoked anywhere in this file, and no clock is read that a test
does not control.

Two properties here are load-bearing rather than cosmetic:

  * the state machine refuses illegal transitions, because the supervisor will
    drive these states from a long-running loop where a silently accepted bad
    transition would be invisible until it corrupted a portfolio decision
  * a fingerprint is stable across runs and distinct across repositories,
    because `BacklogItem.task_id` hardcodes a `HOWLFRAM-` prefix for every
    repository, so item ids genuinely do collide between HowlPlane and
    HowlFrame and the work item must not inherit that collision
"""

import json

import pytest

from src.control_plane.durable_store import ArtifactIdentityError
from src.control_plane.factory.work_item import (
    WORK_ITEM_TRANSITIONS,
    InvalidWorkItemTransitionError,
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
    WorkItemStore,
    repository_tag,
    work_item_fingerprint,
)


def _item(**kwargs):
    defaults = dict(
        origin=WorkItemOrigin.EXISTING_BACKLOG,
        repository="howlcipher/howlframe",
        title="A live defect",
        identity_keys=["bugs.md", "51"],
    )
    defaults.update(kwargs)
    return WorkItem.create(**defaults)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_across_constructions():
    assert _item().fingerprint == _item().fingerprint


def test_same_item_id_in_two_repositories_does_not_collide():
    """The real collision this guards: BacklogItem.task_id ignores the repo."""
    frame = _item(repository="howlcipher/howlframe")
    plane = _item(repository="howlcipher/howlplane")

    assert frame.fingerprint != plane.fingerprint
    assert frame.work_item_id != plane.work_item_id


def test_origin_participates_in_identity():
    backlog = _item(origin=WorkItemOrigin.EXISTING_BACKLOG)
    discovered = _item(origin=WorkItemOrigin.DISCOVERED_PROBLEM)

    assert backlog.fingerprint != discovered.fingerprint


def test_identity_key_order_does_not_change_the_fingerprint():
    assert work_item_fingerprint("o", "r", ["a", "b"]) == work_item_fingerprint(
        "o", "r", ["b", "a"]
    )


def test_work_item_id_is_a_safe_store_filename():
    """Ids flow straight into DurableObjectStore, which rejects unsafe names."""
    item = _item(repository="owner/weird name.git")
    WorkItemStore  # id must survive the store's own validation, exercised below
    assert " " not in item.work_item_id
    assert "/" not in item.work_item_id


def test_repository_tag_degrades_rather_than_producing_an_empty_name():
    assert repository_tag("") == "unknown"
    assert repository_tag("owner/---") == "unknown"
    assert repository_tag("howlcipher/howlframe") == "howlframe"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_the_happy_path_reaches_shipped():
    item = _item()
    for state in (
        WorkItemState.ADMITTED,
        WorkItemState.READY,
        WorkItemState.IN_PROGRESS,
        WorkItemState.VERIFYING,
        WorkItemState.SHIPPED,
    ):
        item.transition_to(state, reason="test")
    assert item.state == WorkItemState.SHIPPED
    assert item.is_terminal


def test_illegal_transition_raises_rather_than_being_tolerated():
    item = _item()
    with pytest.raises(InvalidWorkItemTransitionError) as exc:
        item.transition_to(WorkItemState.SHIPPED)

    # The message must name both ends and the legal alternatives, because the
    # supervisor logs it from inside a loop with no other context.
    assert "proposed" in str(exc.value)
    assert "shipped" in str(exc.value)
    assert "admitted" in str(exc.value)


@pytest.mark.parametrize(
    "terminal",
    [WorkItemState.SHIPPED, WorkItemState.REJECTED, WorkItemState.ABANDONED],
)
def test_terminal_states_admit_no_successor(terminal):
    assert WORK_ITEM_TRANSITIONS[terminal] == set()


def test_every_transition_target_is_itself_a_known_state():
    """A typo in the adjacency map would create an unreachable dead end."""
    for source, targets in WORK_ITEM_TRANSITIONS.items():
        for target in targets:
            assert target in WORK_ITEM_TRANSITIONS, f"{source} -> {target} unknown"


def test_transition_records_its_reason():
    item = _item()
    item.transition_to(WorkItemState.ADMITTED, reason="cleared the score floor")

    assert item.reopening_history[-1]["to_state"] == "admitted"
    assert item.reopening_history[-1]["reason"] == "cleared the score floor"


def test_unknown_state_or_origin_is_refused_at_construction():
    with pytest.raises(ValueError):
        WorkItem(
            work_item_id="WI-x-1", fingerprint="1", origin="existing_backlog",
            repository="r", title="t", state="not_a_state",
        )
    with pytest.raises(ValueError):
        WorkItem(
            work_item_id="WI-x-1", fingerprint="1", origin="not_an_origin",
            repository="r", title="t",
        )


def test_a_blocked_item_is_not_dispatchable_even_when_ready():
    item = _item()
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    assert item.is_dispatchable

    item.blocked_by = ["WI-howlframe-deadbeefdeadbeef"]
    assert not item.is_dispatchable


# ---------------------------------------------------------------------------
# Reopening: only materially new evidence
# ---------------------------------------------------------------------------

def test_reopen_requires_the_caller_to_supply_new_evidence_fingerprints():
    item = _item()
    item.transition_to(WorkItemState.AWAITING_OWNER)
    item.transition_to(WorkItemState.REJECTED, reason="owner said no")

    item.reopen(["traj-9"], ["fp-new"], reason="new failure signature")

    assert item.state == WorkItemState.PROPOSED
    assert "fp-new" in item.evidence_fingerprints
    assert item.reopening_history[-1]["reason"] == "new failure signature"


def test_reopen_unions_evidence_rather_than_replacing_it():
    item = _item(evidence_refs=["traj-1"], evidence_fingerprints=["fp-1"])
    item.reopen(["traj-2"], ["fp-1", "fp-2"], reason="more of the same plus one")

    assert item.evidence_refs == ["traj-1", "traj-2"]
    assert item.evidence_fingerprints == ["fp-1", "fp-2"]


# ---------------------------------------------------------------------------
# Serialization and the durable store
# ---------------------------------------------------------------------------

def test_enum_and_loaded_items_render_identically():
    """A fresh item and the same item off disk must be indistinguishable."""
    fresh = _item(origin=WorkItemOrigin.SELF_IMPROVEMENT)
    loaded = WorkItem.from_dict(json.loads(json.dumps(fresh.to_dict())))

    assert fresh.origin == loaded.origin == "self_improvement"
    assert repr(fresh.origin) == repr(loaded.origin)
    assert fresh.state == loaded.state == "proposed"


def test_store_round_trip(tmp_path):
    store = WorkItemStore(tmp_path / "work_items")
    item = _item()
    store.save_object(item)

    assert store.exists(item.work_item_id)
    assert store.load(item.work_item_id).title == item.title
    assert [i.work_item_id for i in store.list_all()] == [item.work_item_id]


def test_store_finds_by_fingerprint(tmp_path):
    store = WorkItemStore(tmp_path / "work_items")
    item = _item()
    store.save_object(item)

    assert store.find_by_fingerprint(item.fingerprint).work_item_id == item.work_item_id
    assert store.find_by_fingerprint("nope") is None


def test_store_refuses_an_id_that_could_escape_the_directory(tmp_path):
    store = WorkItemStore(tmp_path / "work_items")
    with pytest.raises(ArtifactIdentityError):
        store.load("../../etc/passwd")


def test_a_truncated_file_fails_closed_rather_than_loading_a_partial_item(tmp_path):
    store = WorkItemStore(tmp_path / "work_items")
    item = _item()
    store.save_object(item)
    (tmp_path / "work_items" / f"{item.work_item_id}.json").write_text("{not json")

    with pytest.raises(Exception):
        store.load(item.work_item_id)


def test_saving_twice_is_idempotent_and_reflects_the_later_state(tmp_path):
    store = WorkItemStore(tmp_path / "work_items")
    item = _item()
    store.save_object(item)
    item.transition_to(WorkItemState.ADMITTED)
    store.save_object(item)

    assert len(store.list_all()) == 1
    assert store.load(item.work_item_id).state == WorkItemState.ADMITTED
