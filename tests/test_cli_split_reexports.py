"""The cli.py split keeps every moved name importable from cli and patchable there (row 78)."""

import pytest

from howlplane.control_plane import cli, governance_cli, status_cli
from howlplane.control_plane.factory import factory_run_cli

pytestmark = pytest.mark.unit

MOVED = {
    governance_cli: ["_handle_work_item_decision", "_handle_proposal_decision", "_handle_decision", "cmd_approve", "cmd_reject",
                     "cmd_resume", "cmd_cancel", "_lock_candidates", "_lock_relevance", "cmd_unlock"],
    factory_run_cli: ["_resolve_factory_campaign", "_select_factory_authority", "_build_factory_supervisor",
                      "cmd_factory_run_once", "cmd_factory_run", "_factory_state_store",
                      "_factory_workspace", "_factory_readiness", "_factory_preflight"],
    status_cli: ["cmd_status", "cmd_doctor", "cmd_agents"],
}


@pytest.mark.parametrize("module,name", [(m, n) for m, names in MOVED.items() for n in names])
def test_cli_reexports_the_same_object(module, name):
    assert getattr(cli, name) is getattr(module, name)


def test_moved_handlers_resolve_helpers_on_cli_at_call_time(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_handle_decision", lambda args, decision: seen.append(decision) or 0)
    assert governance_cli.cmd_approve(object()) == 0
    assert governance_cli.cmd_reject(object()) == 0
    assert seen == ["approved", "rejected"]


def test_handlers_table_still_points_at_the_reexports():
    assert cli.HANDLERS["approve"] is cli.cmd_approve and cli.HANDLERS["status"] is cli.cmd_product_status
