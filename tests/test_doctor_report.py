"""`howlplane doctor` composes the existing diagnostics; the old doctors are untouched."""

import json

import pytest

from howlplane.control_plane import agent_readiness, cli
from tests._product_harness import WORKERS, product  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit


def test_doctor_reports_every_section_and_overall(product):
    code, out, _ = product.run("doctor")
    assert code == 0
    for section in ("System", "Workers", "Repository", "Factory", "Overall"):
        assert section in out
    assert "Codex" in out and "READY" in out


def test_ready_prints_one_word_and_exits_zero(product):
    code, out, _ = product.run("doctor", "--ready")
    assert (code, out.strip()) == (0, "READY")


def test_not_ready_lists_each_blocker_with_a_pasteable_fix(product):
    product.trust = "TRUST_REQUIRED"
    code, out, _ = product.run("doctor", "--ready")
    assert code == 1 and out.splitlines()[0] == "NOT READY"
    assert "1. " in out and "Fix: howlplane setup" in out


def test_no_worker_names_the_agents_fix(product, monkeypatch):
    broken = [dict(w, authenticated=False) for w in WORKERS]
    monkeypatch.setattr(agent_readiness, "evaluate", lambda *a, **k: broken)
    code, out, _ = product.run("doctor", "--ready")
    assert code == 1 and "howlplane doctor --agents" in out


def test_json_is_structured_and_unstyled(product):
    code, out, _ = product.run("--color", "always", "doctor", "--ready", "--json")
    document = json.loads(out)
    assert code == 0 and document["schema"] == "howlplane.doctor/v1" and document["ready"] is True
    assert {e["section"] for e in document["entries"]} >= {"System", "Workers", "Repository", "Factory"}


def test_outside_a_repository_is_not_ready_with_the_git_fix(product, tmp_path, monkeypatch):
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, out, _ = product.run("doctor", "--ready")
    assert code == 1 and "git init" in out


def test_selectors_are_mutually_exclusive(product):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["doctor", "--agents", "--factory"])


def test_agents_selector_delegates_to_agent_doctor(product, monkeypatch):
    seen = []
    monkeypatch.setattr(agent_readiness, "command", lambda args: seen.append(args) or 7)
    code, _, _ = product.run("doctor", "--agents", "--live", "--json")
    assert code == 7 and seen[0].live is True and seen[0].json is True and seen[0].agent is None


def test_factory_selector_delegates_to_factory_doctor(product, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_factory_doctor", lambda args: seen.append(args) or 5)
    code, _, _ = product.run("doctor", "--factory", "--json")
    assert code == 5 and seen[0].json is True and seen[0].state_dir is None


def test_old_agent_and_factory_doctors_still_answer(product):
    assert product.run("agents", "doctor", "--json")[0] == 0
    code, out, _ = product.run("factory", "doctor", "--json")
    assert json.loads(out)["readiness"]["status"] in ("READY", "DEGRADED", "BLOCKED")


def test_system_selector_keeps_the_original_table(product):
    code, out, _ = product.run("doctor", "--system")
    assert "Component" in out and "checks passed" in out
