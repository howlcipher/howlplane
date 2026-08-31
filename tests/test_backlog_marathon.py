#!/usr/bin/env python3
"""
tests/test_backlog_marathon.py

Deterministic tests for backlog-driven marathon selection and the bounded
marathon loop.

The parsing rules here are not stylistic. Both were derived from the real
HowlFrame backlogs, and both prevent an unattended run from working something
a human said it must not:

  * `improvements.md` holds three tables -- the live ranked backlog plus two
    historical V2/V3 tables with a *different column count*. Parsing every
    pipe row offers work shipped months ago as pending.
  * the status column contains `Pending`, `Pending ⚠️ below floor` and
    `Pending — blocked on #88`. A `startswith("Pending")` test admits an item
    explicitly below the ROI floor, which the backlog says needs human
    confirmation, and one blocked on another item.

No provider is invoked anywhere in this file.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.control_plane.authority_profile import (
    CANONICAL_PROFILES,
    HOWLFRAME_OVERNIGHT_PROFILE,
    OVERNIGHT_SAFE_ALLOWED_ACTIONS,
    OVERNIGHT_SAFE_DENIED_ACTIONS,
    OVERNIGHT_SAFE_PROFILE,
    STRICT_PROFILE,
    get_profile,
)
from src.control_plane.backlog_source import (
    BacklogParseError,
    BacklogSource,
    parse_backlog_file,
)

RANKED = """# Bug Backlog

## Ranked Backlog (best ROI first)

| # | Bug | Status | Score (V×D÷E) | Claude model | Gemini model | OpenAI model | ROI rationale |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 51 | [A live defect](#51-a-live-defect) | Pending | 2.5 (5×1÷2) | Haiku 4.5 | — | gpt | worth doing |
| 52 | [Already shipped](#52-already-shipped) | Done (2026-08-29) | 3.0 (6×1÷2) | Sonnet 5 | — | gpt | shipped |
| 74 | [Below the floor](#74-below-the-floor) | Pending ⚠️ below floor | 0.4 (2×1÷5) | Haiku | — | gpt | needs confirmation |
| 100 | [Blocked on another item](#100-blocked) | Pending — blocked on #88 | 1.5 (6×1÷4) | Sonnet 5 | — | gpt | blocked |
| 53 | [A second live defect](#53-second) | Pending | 1.5 (6×0.5÷2) | Sonnet 5 | — | gpt | also worth doing |

## Details

### 51. A live defect
* **Symptom:** the thing is broken.
* **Deterministic acceptance:** the thing is not broken.

### 53. A second live defect
* **Symptom:** another thing is broken.

## V2: Historical Table

| # | Improvement | Status | Score | AI Rationale |
| --- | --- | --- | --- | --- |
| 58 | **Shipped long ago** | Pending | 2.33 | this table is history, not open work |
"""


@pytest.fixture
def backlog_repo(tmp_path: Path) -> Path:
    (tmp_path / "bugs.md").write_text(RANKED, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing: only the live table, only exactly-Pending
# ---------------------------------------------------------------------------

def test_only_the_first_ranked_table_is_read(backlog_repo):
    """The historical V2 table must not contribute open work."""
    items = parse_backlog_file(backlog_repo / "bugs.md")
    assert [i.item_id for i in items] == ["51", "52", "74", "100", "53"]
    assert "58" not in {i.item_id for i in items}, (
        "a historical table below the live one was parsed as open work"
    )


def test_pending_is_matched_exactly_not_as_a_prefix(backlog_repo):
    """`Pending ⚠️ below floor` and `Pending — blocked on #88` are not eligible."""
    selection = BacklogSource(backlog_repo).select()

    assert [i.item_id for i in selection.eligible] == ["51", "53"]
    excluded = {e["item_id"]: e["reason"] for e in selection.excluded}
    assert excluded["74"].startswith("STATUS_NOT_ELIGIBLE")
    assert excluded["100"].startswith("STATUS_NOT_ELIGIBLE")
    # A naive prefix match would have admitted both.
    for item_id in ("74", "100"):
        assert item_id not in {i.item_id for i in selection.eligible}


def test_done_rows_are_never_offered(backlog_repo):
    selection = BacklogSource(backlog_repo).select()
    assert "52" not in {i.item_id for i in selection.eligible}
    # Done rows are not even recorded as near-misses; only Pending* rows are.
    assert "52" not in {e["item_id"] for e in selection.excluded}


def test_roi_floor_excludes_low_scoring_work(backlog_repo):
    selection = BacklogSource(backlog_repo, roi_floor=2.0).select()
    assert [i.item_id for i in selection.eligible] == ["51"]
    assert any(e["reason"].startswith("BELOW_ROI_FLOOR") for e in selection.excluded)


def test_a_file_without_a_ranked_backlog_is_an_error(tmp_path):
    (tmp_path / "bugs.md").write_text("# Notes\n\nNo table here.\n", encoding="utf-8")
    with pytest.raises(BacklogParseError):
        parse_backlog_file(tmp_path / "bugs.md")


def test_missing_backlog_files_are_skipped_not_fatal(tmp_path):
    selection = BacklogSource(tmp_path).select()
    assert selection.eligible == []
    assert selection.files_read == []


def test_task_ids_are_stable_and_distinguish_bugs_from_improvements(backlog_repo):
    item = BacklogSource(backlog_repo).select().eligible[0]
    assert item.task_id == "HOWLFRAM-BUG-51"
    assert item.kind == "bug"


def test_detail_section_is_the_problem_statement(backlog_repo):
    source = BacklogSource(backlog_repo)
    item = source.select().eligible[0]
    detail = source.item_detail(item)
    assert "**Symptom:**" in detail
    assert "Deterministic acceptance" in detail
    # Stops at the next item rather than swallowing the whole file.
    assert "A second live defect" not in detail


def test_next_item_skips_already_attempted_work(backlog_repo):
    selection = BacklogSource(backlog_repo).select()
    assert selection.next_item().task_id == "HOWLFRAM-BUG-51"
    assert selection.next_item(skip=["HOWLFRAM-BUG-51"]).task_id == "HOWLFRAM-BUG-53"
    assert selection.next_item(skip=["HOWLFRAM-BUG-51", "HOWLFRAM-BUG-53"]) is None


# ---------------------------------------------------------------------------
# The authority profile: a separate grant, and no autonomous merge
# ---------------------------------------------------------------------------

def test_howlframe_profile_cannot_merge(tmp_path):
    profile = get_profile("howlframe-overnight")
    assert profile.max_merges == 0
    assert "merge_pull_request" not in profile.allowed_action_classes
    assert profile.authorized_repositories == ["howlcipher/howlframe"]
    assert profile.external_spend_usd_limit == 0.0


def test_howlframe_profile_keeps_every_hard_denial():
    assert set(HOWLFRAME_OVERNIGHT_PROFILE.denied_action_classes) == set(
        OVERNIGHT_SAFE_DENIED_ACTIONS
    )
    for forbidden in (
        "force_push", "history_rewrite", "bypass_required_checks",
        "branch_protection_weakening", "hygiene_policy_weakening",
        "slop_debt_acceptance", "authority_profile_modification",
    ):
        assert forbidden in HOWLFRAME_OVERNIGHT_PROFILE.denied_action_classes


def test_existing_profiles_are_byte_identical():
    """Adding a profile must not widen an already-granted one."""
    assert OVERNIGHT_SAFE_PROFILE.authorized_repositories == ["howlcipher/howlplane"]
    assert OVERNIGHT_SAFE_PROFILE.max_merges == 10
    assert "merge_pull_request" in OVERNIGHT_SAFE_PROFILE.allowed_action_classes
    assert STRICT_PROFILE.max_merges == 0
    assert STRICT_PROFILE.allowed_action_classes == []
    assert STRICT_PROFILE.authorized_repositories == []
    assert set(CANONICAL_PROFILES) == {
        "strict", "overnight-safe", "howlframe-overnight",
    }


def test_howlframe_profile_grants_backlog_selection_and_parking():
    """The actions the loop actually performs are the ones it was granted."""
    for action in (
        "select_next_evidence_backed_task", "park_and_continue",
        "create_task_branch", "commit_task_changes", "push_task_branch",
        "create_pull_request", "inspect_ci", "run_build_test_lint_scan",
    ):
        assert action in HOWLFRAME_OVERNIGHT_PROFILE.allowed_action_classes
        assert action in OVERNIGHT_SAFE_ALLOWED_ACTIONS


# ---------------------------------------------------------------------------
# The loop: bounded, resumable, and honest about why it stopped
# ---------------------------------------------------------------------------

class _StubPool:
    def __init__(self, available: bool = True):
        self._available = available
        self.reset_calls = 0

    def has_available_providers(self) -> bool:
        return self._available

    def reset_transient_exhaustion(self) -> None:
        self.reset_calls += 1

    def select_candidates(self, **kwargs) -> List[str]:
        return ["fake_provider"] if self._available else []


def _engine(repo: Path, campaign_dir: Path, pool=None, outcomes=None):
    """Marathon engine with the governed-task execution seam stubbed.

    The governed lifecycle itself is exercised exhaustively by the
    orchestrator's own suites; these tests are about the loop around it --
    what it selects, when it stops, and what it records.
    """
    from src.control_plane.synthesis.marathon import MarathonDogfoodEngine

    engine = MarathonDogfoodEngine(
        provider_pool=pool or _StubPool(),
        campaign_dir=campaign_dir,
        target_repo=repo,
        repo_slug="howlcipher/howlframe",
    )
    engine._bind_authority_envelope = lambda *a, **k: None
    engine._git_executor_factory = lambda envelope, merges_so_far: object()

    calls: List[str] = []

    def _fake_execute(task_id: str, **kwargs) -> Tuple[bool, Optional[Dict[str, Any]]]:
        calls.append(task_id)
        return (outcomes or {}).get(task_id, (True, {"task_id": task_id, "provider": "fake_provider"}))

    engine._execute_governed_engineering_improvement = _fake_execute
    engine._calls = calls
    return engine


def test_loop_is_bounded_by_max_tasks(backlog_repo, tmp_path):
    engine = _engine(backlog_repo, tmp_path / "campaigns")
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=1)

    assert report["tasks_attempted"] == ["HOWLFRAM-BUG-51"]
    assert report["stop_reason"] == "max_tasks_reached"
    assert engine._calls == ["HOWLFRAM-BUG-51"]


def test_loop_stops_when_the_backlog_empties(backlog_repo, tmp_path):
    engine = _engine(backlog_repo, tmp_path / "campaigns")
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=10)

    assert report["tasks_attempted"] == ["HOWLFRAM-BUG-51", "HOWLFRAM-BUG-53"]
    assert report["stop_reason"] == "backlog_exhausted"


def test_loop_parks_when_no_provider_is_usable(backlog_repo, tmp_path):
    """Scenario D at the marathon level: stop truthfully, do not loop."""
    engine = _engine(backlog_repo, tmp_path / "campaigns", pool=_StubPool(available=False))
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=5)

    assert report["tasks_attempted"] == []
    assert report["stop_reason"] == "resources_unavailable"
    assert engine._calls == []


def test_runtime_ceiling_stops_before_starting_a_task(backlog_repo, tmp_path):
    engine = _engine(backlog_repo, tmp_path / "campaigns")
    report = engine.run_backlog_marathon(
        "howlframe-overnight", max_tasks=5, max_runtime_hours=0.0,
    )

    assert report["tasks_attempted"] == []
    assert report["stop_reason"] == "max_runtime_reached"
    assert engine._calls == [], "no task may begin after the deadline"


def test_a_parked_task_does_not_stop_the_loop(backlog_repo, tmp_path):
    """park_and_continue: an undelegated boundary parks one task, not the run."""
    engine = _engine(
        backlog_repo, tmp_path / "campaigns",
        outcomes={"HOWLFRAM-BUG-51": (False, {
            "task_id": "HOWLFRAM-BUG-51", "integration_mode": "parked",
            "provider": "fake_provider",
            "parked_record": {"task_id": "HOWLFRAM-BUG-51", "boundary_type": "merge_pull_request"},
        })},
    )
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=5)

    assert report["tasks_parked"] == ["HOWLFRAM-BUG-51"]
    assert report["tasks_completed"] == ["HOWLFRAM-BUG-53"]
    assert report["stop_reason"] == "backlog_exhausted"


def test_a_failed_task_does_not_stop_the_loop(backlog_repo, tmp_path):
    engine = _engine(
        backlog_repo, tmp_path / "campaigns",
        outcomes={"HOWLFRAM-BUG-51": (False, {"task_id": "HOWLFRAM-BUG-51", "provider": "fake_provider"})},
    )
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=5)

    assert report["tasks_failed"] == ["HOWLFRAM-BUG-51"]
    assert report["tasks_completed"] == ["HOWLFRAM-BUG-53"]


def test_resume_does_not_rework_finished_items(backlog_repo, tmp_path):
    """An interrupted marathon continues rather than starting over."""
    campaigns = tmp_path / "campaigns"
    first = _engine(backlog_repo, campaigns)
    report_one = first.run_backlog_marathon("howlframe-overnight", max_tasks=1)
    assert report_one["tasks_completed"] == ["HOWLFRAM-BUG-51"]

    second = _engine(backlog_repo, campaigns)
    report_two = second.run_backlog_marathon(
        "howlframe-overnight", max_tasks=5,
        resume_campaign_id=report_one["campaign_id"],
    )

    assert second._calls == ["HOWLFRAM-BUG-53"], "a finished item was reworked"
    assert set(report_two["tasks_completed"]) == {"HOWLFRAM-BUG-51", "HOWLFRAM-BUG-53"}


def test_resume_reconsiders_providers_whose_cooldown_elapsed(backlog_repo, tmp_path):
    campaigns = tmp_path / "campaigns"
    pool = _StubPool()
    first = _engine(backlog_repo, campaigns, pool=pool)
    report = first.run_backlog_marathon("howlframe-overnight", max_tasks=1)

    second = _engine(backlog_repo, campaigns, pool=pool)
    second.run_backlog_marathon(
        "howlframe-overnight", max_tasks=1, resume_campaign_id=report["campaign_id"],
    )
    assert pool.reset_calls == 1, (
        "a resumed run must re-probe providers rather than inherit a stale "
        "exhausted view of the pool"
    )


def test_campaign_state_is_durable_after_every_task(backlog_repo, tmp_path):
    import json

    campaigns = tmp_path / "campaigns"
    engine = _engine(backlog_repo, campaigns)
    report = engine.run_backlog_marathon("howlframe-overnight", max_tasks=5)

    state_file = Path(report["state_dir"]) / "campaign_state.json"
    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "backlog_exhausted"
    assert [t["task_id"] for t in state["completed_tasks"]] == [
        "HOWLFRAM-BUG-51", "HOWLFRAM-BUG-53",
    ]
