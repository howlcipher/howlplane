"""Bare `howlplane` and `howlplane status`: one situation, the same answer everywhere."""

import json
import re

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.factory.supervisor_state import SupervisorState
from howlplane.control_plane.factory.work_item import WorkItemState
from tests._factory_test_helpers import ready_work_item
from tests._product_harness import product  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_unprepared_repository_points_at_setup(product):
    product.trust = "TRUST_REQUIRED"
    code, out, _ = product.run()
    assert code == 0 and "NOT READY" in out and "howlplane setup" in out
    assert "howlplane factory" not in out


def test_outside_a_repository_says_so_and_changes_nothing(product, tmp_path, monkeypatch):
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, out, _ = product.run()
    assert code == 0 and "not a Git repository" in out
    assert not (tmp_path / "state" / "howlplane").exists()  # read-only: nothing was created


def test_prepared_repository_is_ready_to_start(product):
    code, out, _ = product.run()
    assert code == 0 and "READY" in out and "2 available" in out and "howlplane start" in out
    assert not (product.repo.parent / "state" / "howlplane").exists()  # bare invocation is read-only


def test_running_shows_work_worker_and_nothing_required(product):
    product.run("start", "--authority", "safe")
    item = ready_work_item(product._supervisor().work_item_store, title="Add Kroger search adapter", key="k")
    supervisor = product._supervisor()
    record = supervisor.state_store.load(reconcile_restart=False)
    record.transition_to(SupervisorState.DISPATCHING, reason="test")
    record.current_work_item_id, record.current_provider = item.work_item_id, "codex"
    supervisor.state_store.save(record)
    code, out, _ = product.run()
    assert code == 0 and "RUNNING" in out
    assert f"{item.work_item_id} - Add Kroger search adapter" in out and "Codex" in out
    assert "No action required." in out


def test_owner_required_names_the_item_and_the_short_command(product):
    product.run("start", "--authority", "safe")
    item = product.seed_owner_decision()
    code, out, _ = product.run()
    assert code == 0 and "NEEDS" not in out and "OWNER REQUIRED" in out
    assert f"howlplane approve {item.work_item_id}" in out and "--state-dir" not in out
    code, out, _ = product.run("status")
    assert "Add Kroger search adapter" in out  # the id comes with its human-readable title


def test_status_json_is_the_factory_operator_contract_without_styling(product):
    product.run("start", "--authority", "safe")
    product.seed_owner_decision()
    code, out, _ = product.run("--color", "always", "status", "--json")
    document = json.loads(out)  # valid JSON even when color is forced: styling never reaches JSON
    assert ANSI.search(out) is None
    assert document["operator"]["schema"] == "howlplane.operator.status/v1"
    assert document["operator"]["owner_required"] is True
    factory_code, factory_out, _ = product.run("factory", "status", "--json")
    assert json.loads(factory_out)["operator"] == document["operator"]


def test_plain_and_styled_text_carry_the_same_words(product):
    product.run("start", "--authority", "safe")
    _, plain, _ = product.run("--color", "never", "status")
    _, styled, _ = product.run("--color", "always", "status")
    assert ANSI.search(plain) is None and ANSI.sub("", styled) == plain


def test_status_before_start_has_machine_readable_not_started(product):
    code, out, _ = product.run("status", "--json")
    document = json.loads(out)
    assert code == 0 and document["state"] == "not_started"
    assert document["operator"]["reason_code"] == "NOT_STARTED"
    assert document["operator"]["next_action"]["command"] == "howlplane start"


def test_status_verbose_keeps_the_project_diagnostics(product):
    code, out, _ = product.run("status", "--verbose")
    assert code == 0 and "PROJECT STATUS" in out and "Repository Path:" in out


def test_deprecated_ai_entry_point_keeps_printing_help_when_bare(capsys):
    assert cli.main([], program_name="ai") == 1
    assert "usage:" in capsys.readouterr().out
