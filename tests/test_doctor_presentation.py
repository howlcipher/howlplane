"""Doctor tables: one status vocabulary, actionable next step, JSON untouched."""

from types import SimpleNamespace

import pytest

from howlplane.control_plane.factory import factory_cli
from howlplane.control_plane.factory.work_item import WorkItemState
from howlplane.control_plane.presentation.doctor import (
    LIMITED, NEEDS_ACTION, READY, UNAVAILABLE, render_checks_table, render_worker_summary, worker_state)

pytestmark = pytest.mark.unit


def _agent(name, **kw):
    base = {"agent": name.lower(), "name": name, "installed": True, "authenticated": True,
            "unattended_execution": True, "capacity": {"state": "AVAILABLE"}}
    base.update(kw)
    return base


@pytest.mark.parametrize("summary,expected", [
    (_agent("Codex"), (READY, "")),
    (_agent("Claude", capacity={"state": "SESSION_EXHAUSTED"}), (LIMITED, "session limit")),
    (_agent("Devin", authenticated=False), (UNAVAILABLE, "not authenticated")),
    (_agent("AGY", installed=False), (UNAVAILABLE, "not installed")),
    (_agent("Cursor", unattended_execution=False), (NEEDS_ACTION, "interactive only")),
    (_agent("Gem", unattended_execution=None), (READY, "unattended use unverified")),
])
def test_worker_state_vocabulary(summary, expected):
    assert worker_state(summary) == expected


def test_workspace_trust_gates_a_ready_worker():
    assert worker_state(_agent("Codex"), "TRUST_REQUIRED") == (NEEDS_ACTION, "workspace trust required")


def test_summary_counts_and_remediation_command():
    agents = [_agent("Codex"), _agent("Claude", capacity={"state": "SESSION_EXHAUSTED"}),
              _agent("Devin", authenticated=False)]
    text = "\n".join(render_worker_summary(agents))
    assert "1 worker ready" in text and "1 worker temporarily limited" in text and "1 worker unavailable" in text
    assert "howlplane agents doctor --refresh" in text
    assert "Worker" in text and "UNAVAILABLE" in text and text.isascii()


def test_interactive_only_worker_points_at_live_repo_smoke_not_refresh():
    # DOG-005: `--refresh` keeps a real session's permission verdict, so the advice must name the command that replaces it.
    text = "\n".join(render_worker_summary([_agent("Claude", unattended_execution=False)]))
    assert "howlplane agents doctor --live --repo <path-to-your-repo>" in text
    assert "howlplane agents doctor --live --repo" not in "\n".join(render_worker_summary([_agent("Codex"), _agent("Devin", authenticated=False)]))


def test_all_ready_needs_no_action():
    assert "No action required." in "\n".join(render_worker_summary([_agent("Codex")]))


def test_limited_only_needs_no_action():
    text = "\n".join(render_worker_summary([_agent("Claude", capacity={"state": "RATE_LIMITED"})]))
    assert "No action required" in text and "rate limited" in text


def test_checks_table_lists_actions():
    checks = [SimpleNamespace(name="Git Hooks", status="warning", message="missing", details={"action": "install"}),
              SimpleNamespace(name="Git", status="ok", message="fine", details=None)]
    text = "\n".join(render_checks_table(checks))
    assert "WARNING" in text and "OK" in text and "Git Hooks: install" in text


class _Store:
    def __init__(self, items):
        self.items = items

    def list_all(self):
        return self.items


def test_owner_decisions_only_for_real_awaiting_human_tasks(monkeypatch, tmp_path):
    item = SimpleNamespace(work_item_id="WI-042", state=WorkItemState.AWAITING_OWNER, task_ids=["FACTORY-WI-042"])
    other = SimpleNamespace(work_item_id="WI-043", state=WorkItemState.AWAITING_OWNER, task_ids=["FACTORY-WI-043"])
    states = {"FACTORY-WI-042": "awaiting_human", "FACTORY-WI-043": "failed"}

    def load(path):
        return SimpleNamespace(current_state=states[path.split("/")[-2]])
    monkeypatch.setattr(factory_cli.TaskSpec, "load_from_file", staticmethod(load))
    decisions = factory_cli._owner_decisions(_Store([item, other]), tmp_path)
    assert [d["work_item_id"] for d in decisions] == ["WI-042"]
    assert decisions[0]["approve"].startswith("howlplane approve FACTORY-WI-042 --repo ")
    assert decisions[0]["reject"].startswith("howlplane reject FACTORY-WI-042 --repo ")


def test_owner_decisions_survive_missing_task_files(tmp_path):
    item = SimpleNamespace(work_item_id="WI-1", state=WorkItemState.AWAITING_OWNER, task_ids=["FACTORY-WI-1"])
    assert factory_cli._owner_decisions(_Store([item]), tmp_path) == []
