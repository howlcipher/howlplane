"""`howlplane start|logs|stop`: thin delegation, safe continuation, no silent recovery from failure."""

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane.factory.supervisor_state import SupervisorState
from tests._product_harness import WORKERS, product  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit


def _stop_with(product, reason, last_error=None):
    supervisor = product._supervisor()
    record = supervisor.state_store.load(reconcile_restart=False)
    record.transition_to(SupervisorState.STOPPED, reason=reason)
    record.stopped_reason, record.last_error = reason, last_error
    supervisor.state_store.save(record)
    product._stop_process(product._campaign())  # the background process is gone, as after a crash


def test_start_continues_an_operator_stop_without_a_resume_step(product):
    product.run("start", "--authority", "safe")
    product.run("stop")
    assert product.state_record().state == SupervisorState.STOPPED
    code, out, _ = product.run("start")
    assert code == 0 and "STARTED" in out
    assert product.state_record().state == SupervisorState.IDLE


def test_start_after_a_bounded_completion_continues(product):
    product.run("start", "--authority", "safe")
    _stop_with(product, "bounded_work_item_completed")
    assert product.run("start")[0] == 0


@pytest.mark.parametrize("reason,error", [
    ("bounded_work_item_failed", "dispatch crashed"),
    ("malformed_state", None),
    (None, "verification exploded"),
])
def test_a_real_failure_is_not_silently_resumed(product, reason, error):
    product.run("start", "--authority", "safe")
    _stop_with(product, reason or "failure", error)
    started_before = len(product.started_with)
    code, out, err = product.run("start")
    assert code == 1
    assert "did not restart on its own" in err and "howlplane start --retry" in err
    assert len(product.started_with) == started_before  # nothing was launched
    assert product.state_record().state == SupervisorState.STOPPED  # and nothing was resumed


def test_retry_is_the_deliberate_way_past_a_failure(product):
    product.run("start", "--authority", "safe")
    _stop_with(product, "bounded_work_item_failed", "dispatch crashed")
    code, out, _ = product.run("start", "--retry")
    assert code == 0 and "STARTED" in out


def test_status_after_a_failure_recommends_logs_then_retry(product):
    product.run("start", "--authority", "safe")
    _stop_with(product, "failure", "dispatch crashed")
    _, out, _ = product.run("status")
    assert "FAILED" in out and "howlplane logs --errors" in out and "howlplane start --retry" in out


def test_objective_is_passed_to_the_existing_start_path(product):
    product.run("start", "continue building the grocery search vertical slice", "--authority", "safe")
    assert product.started_with[-1]["objective"] == "continue building the grocery search vertical slice"
    product.run("stop")
    product.run("start", "--objective", "ship it")
    assert product.started_with[-1]["objective"] == "ship it"


def test_two_different_objectives_are_refused(product):
    code, _, err = product.run("start", "one", "--objective", "two")
    assert code == 1 and "START_OBJECTIVE_CONFLICT" in err


def test_start_without_a_usable_worker_explains_and_names_the_fix(product, monkeypatch):
    broken = [dict(w, authenticated=False) for w in WORKERS]
    monkeypatch.setattr(cli.agent_readiness, "evaluate", lambda *a, **k: broken)
    code, out, err = product.run("start", "--authority", "safe")
    assert code == 1
    assert "could not start" in err and "No authenticated autonomous worker" in err
    assert "howlplane doctor --agents" in err and product.started_with == []


def test_unprepared_workspace_sends_the_user_to_setup(product):
    product.trust = "TRUST_REQUIRED"
    code, out, err = product.run("start", "--authority", "safe")
    assert code == 1 and "howlplane setup" in err and product.started_with == []


def test_stop_before_start_changes_nothing(product):
    code, out, _ = product.run("stop")
    assert code == 0 and "NOT RUNNING" in out and "howlplane start" in out
    assert not (product.repo.parent / "state" / "howlplane").exists()


def test_logs_passes_every_filter_through_to_factory_logs(product, monkeypatch):
    product.run("start", "--authority", "safe")
    seen = {}
    monkeypatch.setattr(cli, "cmd_factory_logs", lambda args: seen.update(vars(args)) or 0)
    argv = ["logs", "--follow", "--errors", "--since", "1h", "--provider", "codex", "--work-item", "WI-1",
            "--json", "--raw", "--verbose", "--lines", "5", "--level", "warning"]
    assert product.run(*argv)[0] == 0
    assert seen["follow"] and seen["errors"] and seen["since"] == "1h" and seen["provider"] == "codex"
    assert seen["work_item"] == "WI-1" and seen["json"] and seen["raw"] and seen["verbose"]
    assert seen["lines"] == 5 and seen["level"] == "WARNING"


def test_the_original_factory_commands_still_work(product):
    assert product.run("factory", "start", "--authority", "safe")[0] == 0
    assert "State" in product.run("factory", "status", "--verbose")[1]
    code, out, _ = product.run("factory", "stop")
    assert code == 0 and "howlplane factory resume" in out  # legacy wording is unchanged
    assert product.run("factory", "resume")[0] == 0
    assert product.run("factory", "logs")[0] == 0
