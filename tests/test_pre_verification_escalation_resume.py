#!/usr/bin/env python3
"""
tests/test_pre_verification_escalation_resume.py

Deterministic tests for issue #12: an ordinary human approval authorizes the
governed workflow to CONTINUE. It is not the human declaring the implementation
correct, and it can never stand in for the deterministic verification gate.

HOWLFRAM-BUG-50 escalated to `awaiting_human` during Stage 5, before Stage 6
ever ran. After `ai approve` and `ai resume` it reported
`Final state: COMPLETE (Exit 0)` while `verification_plan.json` still read
`overall_status: unverified`, every step `claimed`, every `exit_code: null`,
and the run summary had already said `Executed: 0 -- NOT RUN`. HOWLFRAM-BUG-52
is parked in exactly that shape today.

These tests pin the ten invariants the fix has to hold. There is deliberately
no coverage of a "verification override" because no such operation exists:
overruling a failed deterministic gate would be a distinct authority act with
its own evidence and its own terminal semantics.

No provider is invoked. Verification steps are real subprocesses (`true` /
`false`), so the gate is genuinely executed rather than simulated.
"""

import json
from pathlib import Path

import pytest

from src.control_plane.evidence_ledger import EvidenceLedger
from src.control_plane.human_boundary import (
    HumanLifecycleManager,
    VERIFICATION_NO_PLAN,
    VERIFICATION_PASSED,
)
from src.control_plane.orchestrator import TERMINAL_VERIFICATION_STATUSES
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan
from tests._git_test_helpers import init_git_repo
from tests.test_human_approval_lifecycle import _create_awaiting_human_task_run


def _repo(tmp_path: Path) -> Path:
    init_git_repo(tmp_path, files={"README.md": "# Test Repo\n"})
    return tmp_path


def _plan(task_id: str, *, command, name="Deterministic gate", required=True) -> VerificationPlan:
    plan = VerificationPlan(task_id=task_id)
    plan.add_step(
        step_id="step-01",
        name=name,
        command=command,
        category="unit_test",
        required=required,
    )
    return plan


def _park_before_verification(
    repo: Path,
    task_id: str,
    *,
    plan: VerificationPlan | None = None,
) -> Path:
    """Builds the exact durable shape of a pre-Stage-6 escalation.

    Reuses the proven awaiting_human fixture -- which is what keeps
    `diff.patch` consistent with the repository fingerprint that approval
    binds against -- then adds the one thing that distinguishes this case: a
    verification plan that exists but has never been executed. Every step is
    still `claimed` with a null exit code and `overall_status` is `unverified`,
    because routing writes the plan before any stage can escalate.
    """
    run_dir = _create_awaiting_human_task_run(
        repo, task_id, boundaries=["remediation_limit_reached"]
    )
    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    spec.task_class = "bug_fix"
    spec.objective = "Fix a defect that escalated before deterministic verification"
    spec.save_to_file(str(run_dir / "task.yaml"))

    plan = plan or _plan(task_id, command=["true"])
    (run_dir / "verification_plan.json").write_text(plan.to_json(), encoding="utf-8")
    return run_dir


def _recorded_plan(run_dir: Path) -> dict:
    return json.loads((run_dir / "verification_plan.json").read_text(encoding="utf-8"))


def _approve_and_resume(repo: Path, task_id: str, ledger=None):
    HumanLifecycleManager.approve(
        target_repo=repo,
        task_id=task_id,
        reason="Authorizing the governed workflow to continue",
        operator_source="cli",
        ledger=ledger,
    )
    return HumanLifecycleManager.resume(
        target_repo=repo, task_id=task_id, ledger=ledger
    )


# ---------------------------------------------------------------------------
# The parked shape itself, so the fixture cannot drift away from the bug
# ---------------------------------------------------------------------------

def test_fixture_reproduces_the_unverified_shape(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(repo, "TASK-SHAPE")

    recorded = _recorded_plan(run_dir)
    assert recorded["overall_status"] == "unverified"
    assert recorded["overall_status"] not in TERMINAL_VERIFICATION_STATUSES
    assert [s["status"] for s in recorded["steps"]] == ["claimed"]
    assert [s["exit_code"] for s in recorded["steps"]] == [None]


# ---------------------------------------------------------------------------
# Invariant 1 + 2: approve -> resume -> verification executes; PASS may complete
# ---------------------------------------------------------------------------

def test_approval_runs_the_gate_and_a_pass_may_complete(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(repo, "TASK-PASS")

    res = _approve_and_resume(repo, "TASK-PASS")

    recorded = _recorded_plan(run_dir)
    assert recorded["overall_status"] == "passed"
    # The gate actually ran: a real exit code, not a claim.
    assert recorded["steps"][0]["status"] == "verified"
    assert recorded["steps"][0]["exit_code"] == 0

    assert res.final_state == "complete"
    assert res.exit_code == 0
    assert TaskSpec.load_from_file(str(run_dir / "task.yaml")).current_state == "complete"


# ---------------------------------------------------------------------------
# Invariant 3: FAIL cannot become ordinary COMPLETE because a human approved
# ---------------------------------------------------------------------------

def test_failed_verification_cannot_complete_on_approval(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(
        repo, "TASK-FAIL", plan=_plan("TASK-FAIL", command=["false"])
    )

    res = _approve_and_resume(repo, "TASK-FAIL")

    recorded = _recorded_plan(run_dir)
    assert recorded["overall_status"] == "failed"
    assert recorded["steps"][0]["exit_code"] != 0

    assert res.final_state == "failed"
    assert res.exit_code == 1
    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert spec.current_state == "failed"
    assert spec.current_state != "complete"


# ---------------------------------------------------------------------------
# Invariant 4: partial / not-run / unverified cannot masquerade as verified
# ---------------------------------------------------------------------------

def test_partial_verification_does_not_read_as_verified(tmp_path):
    """A skipped optional step leaves the plan short of `passed`."""
    repo = _repo(tmp_path)
    plan = VerificationPlan(task_id="TASK-PARTIAL")
    plan.add_step(
        step_id="step-01", name="Gate", command=["true"],
        category="unit_test", required=True,
    )
    plan.add_step(
        step_id="step-02", name="Unrunnable gate",
        command=["definitely-not-a-real-binary-xyz"],
        category="unit_test", required=False,
    )
    run_dir = _park_before_verification(repo, "TASK-PARTIAL", plan=plan)

    res = _approve_and_resume(repo, "TASK-PARTIAL")

    recorded = _recorded_plan(run_dir)
    assert recorded["overall_status"] != "passed"
    assert res.final_state != "complete"
    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert spec.current_state in ("failed", "blocked")


def test_summary_never_asserts_verification_it_did_not_observe(tmp_path):
    """The old summary hardcoded '(Human Approved & Verified)'."""
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(
        repo, "TASK-SUMMARY", plan=_plan("TASK-SUMMARY", command=["false"])
    )

    _approve_and_resume(repo, "TASK-SUMMARY")

    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Human Approved & Verified" not in summary
    assert "`failed`" in summary
    assert "COMPLETE" not in summary.split("Final State:")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# Invariant 5: post-verification approvals keep their previous semantics
# ---------------------------------------------------------------------------

def test_already_passed_plan_is_not_re_executed(tmp_path):
    """A task that escalated at Stage 7 completes exactly as it always did."""
    repo = _repo(tmp_path)
    plan = _plan("TASK-POST", command=["false"])
    plan.overall_status = "passed"
    run_dir = _park_before_verification(repo, "TASK-POST", plan=plan)

    res = _approve_and_resume(repo, "TASK-POST")

    # The recorded pass stands. Had the gate been re-run, this `false` command
    # would have flipped it to `failed`.
    assert _recorded_plan(run_dir)["overall_status"] == "passed"
    assert res.final_state == "complete"


def test_a_recorded_failure_cannot_be_resumed_into_a_pass(tmp_path):
    """Resume is idempotent in the direction that matters."""
    repo = _repo(tmp_path)
    plan = _plan("TASK-STICKY", command=["true"])
    plan.overall_status = "failed"
    run_dir = _park_before_verification(repo, "TASK-STICKY", plan=plan)

    res = _approve_and_resume(repo, "TASK-STICKY")

    assert _recorded_plan(run_dir)["overall_status"] == "failed"
    assert res.final_state == "failed"


# ---------------------------------------------------------------------------
# Invariant 6 + 9: approval evidence is durable and agrees with the outcome
# ---------------------------------------------------------------------------

def test_approval_evidence_survives_a_failed_verification(tmp_path):
    repo = _repo(tmp_path)
    ledger = EvidenceLedger(str(tmp_path / "ledger.jsonl"))
    run_dir = _park_before_verification(
        repo, "TASK-EVIDENCE", plan=_plan("TASK-EVIDENCE", command=["false"])
    )

    _approve_and_resume(repo, "TASK-EVIDENCE", ledger=ledger)

    assert (run_dir / "human_decision.json").is_file()
    actions = [e.action for e in ledger.get_task_entries("TASK-EVIDENCE")]
    assert "human_approval" in actions
    assert "verification_executed_on_resume" in actions
    assert "resume_blocked_on_verification" in actions
    # The task was never recorded complete.
    assert "task_completed" not in actions


def test_final_state_agrees_with_verification_evidence(tmp_path):
    """The disagreement HOWLFRAM-BUG-50 shipped is now impossible."""
    for task_id, command, expected in (
        ("TASK-AGREE-PASS", ["true"], "complete"),
        ("TASK-AGREE-FAIL", ["false"], "failed"),
    ):
        repo = _repo(tmp_path / task_id)
        run_dir = _park_before_verification(
            repo, task_id, plan=_plan(task_id, command=command)
        )
        res = _approve_and_resume(repo, task_id)
        recorded = _recorded_plan(run_dir)["overall_status"]

        assert res.final_state == expected
        if res.final_state == "complete":
            assert recorded == VERIFICATION_PASSED
        else:
            assert recorded != VERIFICATION_PASSED


# ---------------------------------------------------------------------------
# Invariant 7: resume is safe to repeat
# ---------------------------------------------------------------------------

def test_resume_is_idempotent_after_completion(tmp_path):
    repo = _repo(tmp_path)
    _park_before_verification(repo, "TASK-IDEMPOTENT")

    first = _approve_and_resume(repo, "TASK-IDEMPOTENT")
    second = HumanLifecycleManager.resume(target_repo=repo, task_id="TASK-IDEMPOTENT")

    assert first.final_state == "complete"
    assert second.final_state == "complete"


# ---------------------------------------------------------------------------
# Invariant 8: existing serialized tasks do not silently become verified
# ---------------------------------------------------------------------------

def test_a_task_serialized_before_this_fix_fails_closed(tmp_path):
    """A pre-#12 task.yaml carries no marker saying where it escalated.

    The trigger is the verification evidence itself, so such a task reads
    `unverified` and has its gate executed rather than being assumed to have
    passed one.
    """
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(
        repo, "TASK-LEGACY", plan=_plan("TASK-LEGACY", command=["false"])
    )
    # Strip everything a pre-fix task would not have had.
    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    payload = spec.to_dict()
    payload.pop("effective_implementer_resource_id", None)
    payload.pop("dispatch_resource_id", None)
    TaskSpec.from_dict(payload).save_to_file(str(run_dir / "task.yaml"))

    res = _approve_and_resume(repo, "TASK-LEGACY")

    assert res.final_state != "complete"
    assert _recorded_plan(run_dir)["overall_status"] == "failed"


def test_a_run_without_any_plan_completes_without_claiming_verification(tmp_path):
    """Task runs assembled outside the orchestrator have no plan at all.

    Every governed run writes `verification_plan.json` during routing, before
    any stage can escalate, so this shape only arises for runs whose completion
    is governed by the bounded-execution receipt path. They may still complete;
    what they may not do is describe themselves as verified.
    """
    repo = _repo(tmp_path)
    run_dir = _park_before_verification(repo, "TASK-NOPLAN")
    (run_dir / "verification_plan.json").unlink()

    res = _approve_and_resume(repo, "TASK-NOPLAN")

    assert res.final_state == "complete"
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert VERIFICATION_NO_PLAN in summary
    assert "Human Approved & Verified" not in summary


# ---------------------------------------------------------------------------
# Invariant 10: human authority is not weakened
# ---------------------------------------------------------------------------

def test_resume_without_approval_still_refuses(tmp_path):
    """Running the gate is not a way around needing a human at all."""
    from src.control_plane.human_boundary import ApprovalRequiredError

    repo = _repo(tmp_path)
    run_dir = _park_before_verification(repo, "TASK-NOAPPROVAL")

    with pytest.raises(ApprovalRequiredError):
        HumanLifecycleManager.resume(target_repo=repo, task_id="TASK-NOAPPROVAL")

    # Nothing ran, so nothing was recorded.
    assert _recorded_plan(run_dir)["overall_status"] == "unverified"
    assert TaskSpec.load_from_file(
        str(run_dir / "task.yaml")
    ).current_state == "awaiting_human"


def test_no_generic_verification_override_exists(tmp_path):
    """Guards the boundary this fix deliberately did not cross.

    If someone later adds a way to force a failed gate to complete, it must be
    a distinct, explicitly named authority operation -- not a flag on ordinary
    approval, and not a keyword on resume.
    """
    import inspect

    resume_sig = inspect.signature(HumanLifecycleManager.resume)
    approve_sig = inspect.signature(HumanLifecycleManager.approve)
    forbidden = ("override", "force", "skip_verification", "ignore_verification")
    for sig in (resume_sig, approve_sig):
        for name in sig.parameters:
            assert not any(word in name.lower() for word in forbidden), name
