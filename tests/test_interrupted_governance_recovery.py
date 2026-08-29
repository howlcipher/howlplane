#!/usr/bin/env python3
"""
test_interrupted_governance_recovery.py

Regressions for HOWLFRAM-SLOPFIX-06, the external acceptance canary that failed
during candidate review. The canary proved the timeout taxonomy, bounded
failover and productive-candidate capture all work -- and that none of it helps
if an interrupted run cannot be resumed. `ai resume` deadlocked against its own
task lock, leaked the repository lock on the way out, and `ai unlock` could not
see the lock that `ai status` was reporting.

Everything here uses fake providers. No test consumes provider quota.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.control_plane.cli import cmd_unlock
from src.control_plane.evidence_ledger import EvidenceLedger
from src.control_plane.human_boundary import HumanLifecycleManager
from src.control_plane.locking import (
    LockOwnership,
    RepoLock,
    TaskLock,
    TaskLockedError,
    get_repo_lock_path,
    get_process_create_time,
    get_task_lock_path,
    reset_lock_ownership_registry,
)
from tests._git_test_helpers import init_git_repo


@pytest.fixture(autouse=True)
def _clean_lock_registry():
    """Ownership is process-wide, so each test starts from an empty registry."""
    reset_lock_ownership_registry()
    yield
    reset_lock_ownership_registry()


def _repo(tmp_path: Path) -> Path:
    init_git_repo(tmp_path, files={"README.md": "recovery fixture\n"})
    return tmp_path


def _unlock_args(repo: Path, task_id: str, ledger_file=None) -> argparse.Namespace:
    return argparse.Namespace(
        repo_dir=repo,
        task_id=task_id,
        ledger_file=str(ledger_file) if ledger_file else None,
        json=False,
    )


def _write_lock(
    path: Path, task_id: str, pid: int, hostname: str, lock_type: str,
    create_time: float = 1.0,
):
    """Writes a lock file directly, standing in for a run that has since died."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "pid": pid,
                "hostname": hostname,
                "lock_type": lock_type,
                "operation": lock_type,
                "command": f"ai work {task_id} --execute",
                "started_at": "2026-08-27T21:58:51.321084+00:00",
                "process_create_time": create_time,
                "schema": "howlplane.lock/v1",
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Lock ownership: one lifecycle, one lineage
# ---------------------------------------------------------------------------


def test_same_lifecycle_reentry_does_not_self_deadlock(tmp_path):
    """The exact SLOPFIX-06 failure: resume holds the task lock, then the
    orchestrator needs it too. Holding the lineage token, it gets it."""
    repo = _repo(tmp_path)
    outer = TaskLock(repo, "T-06", operation="resume")
    outer.acquire()

    inner = TaskLock(repo, "T-06", operation="orchestrate")
    assert inner.acquire(outer.ownership) is True
    assert outer.ownership.depth == 2

    inner.release()
    assert get_task_lock_path(repo, "T-06").exists(), "outer holder still working"
    outer.release()
    assert not get_task_lock_path(repo, "T-06").exists()


def test_unrelated_same_process_component_cannot_bypass_authority(tmp_path):
    """Sharing a process is not sharing authority: without the token, blocked."""
    repo = _repo(tmp_path)
    holder = TaskLock(repo, "T-06", operation="resume")
    holder.acquire()

    intruder = TaskLock(repo, "T-06", operation="orchestrate")
    with pytest.raises(TaskLockedError):
        intruder.acquire()

    # A token from a different lineage is no better than none.
    foreign = LockOwnership(
        lineage_id="not-this-lineage",
        lock_path=str(get_task_lock_path(repo, "T-06")),
        task_id="T-06",
        operation="resume",
    )
    with pytest.raises(TaskLockedError):
        TaskLock(repo, "T-06", operation="orchestrate").acquire(foreign)
    holder.release()


def test_a_live_second_process_is_still_blocked(tmp_path):
    """Reentrancy is scoped to one lifecycle, not relaxed for everyone."""
    repo = _repo(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(
            get_task_lock_path(repo, "T-LIVE"),
            "T-LIVE",
            proc.pid,
            os.uname().nodename,
            "task_run",
            create_time=get_process_create_time(proc.pid),
        )
        # Written by another (live) process: no lineage of ours can claim it.
        with pytest.raises(TaskLockedError):
            TaskLock(repo, "T-LIVE", operation="resume").acquire()
    finally:
        proc.kill()
        proc.wait()


def test_every_acquisition_has_a_deterministic_release(tmp_path):
    """Out-of-order release still ends with the lock file gone."""
    repo = _repo(tmp_path)
    outer = TaskLock(repo, "T-ORDER", operation="resume")
    outer.acquire()
    inner = TaskLock(repo, "T-ORDER", operation="orchestrate")
    inner.acquire(outer.ownership)

    outer.release()
    assert get_task_lock_path(repo, "T-ORDER").exists()
    inner.release()
    assert not get_task_lock_path(repo, "T-ORDER").exists()


# ---------------------------------------------------------------------------
# Cleanup: a failed acquisition never strands a lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fault_stage",
    ["after_repo_lock", "after_task_lock", "after_progress_start"],
)
def test_no_lock_survives_a_failure_during_startup(tmp_path, fault_stage):
    """Locks were acquired outside the try/finally, so a failure between the
    repo lock and the task lock stranded `.git/howlplane.lock` and no later run
    could proceed. Every startup point now unwinds cleanly."""
    from src.control_plane.orchestrator import (
        GovernedTaskOrchestrator,
        OrchestrationConfig,
    )
    from src.control_plane.task_spec import TaskSpec

    repo = _repo(tmp_path)
    task = TaskSpec(
        task_id="T-CLEANUP", repository=repo.name, objective="prove cleanup"
    )

    def explode(stage, _run_dir, _spec):
        if stage == fault_stage:
            raise RuntimeError(f"injected failure at {stage}")

    config = OrchestrationConfig(
        acquire_locks=True,
        enable_howlframe_audit=False,
        failure_injection_hook=explode,
        progress_mode="never",
    )
    orch = GovernedTaskOrchestrator(target_repo=repo, config=config)

    with pytest.raises(RuntimeError):
        orch.run(task)

    assert not get_repo_lock_path(repo).exists(), "repository lock leaked"
    assert not get_task_lock_path(repo, "T-CLEANUP").exists(), "task lock leaked"


def test_repo_lock_is_released_when_the_task_lock_cannot_be_taken(tmp_path):
    """The precise SLOPFIX-06 leak: two dead PIDs were left behind this way."""
    from contextlib import ExitStack

    repo = _repo(tmp_path)
    blocker = TaskLock(repo, "T-LEAK", operation="resume")
    blocker.acquire()

    with pytest.raises(TaskLockedError):
        with ExitStack() as stack:
            repo_lock = RepoLock(repo, "T-LEAK")
            repo_lock.acquire()
            stack.callback(repo_lock.release)
            TaskLock(repo, "T-LEAK", operation="orchestrate").acquire()

    assert not get_repo_lock_path(repo).exists()
    blocker.release()


# ---------------------------------------------------------------------------
# `ai unlock` acts on the locks `ai status` reports
# ---------------------------------------------------------------------------


def test_unlock_reclaims_the_stale_repository_lock(tmp_path):
    """SLOPFIX-06's blocking lock was `.git/howlplane.lock`, which `ai unlock`
    could not see: it reported "nothing to reclaim" while recovery stayed
    impossible."""
    repo = _repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    _write_lock(
        get_repo_lock_path(repo),
        "HOWLFRAM-SLOPFIX-06",
        999999,
        os.uname().nodename,
        "repository_mutation",
    )

    assert cmd_unlock(_unlock_args(repo, "HOWLFRAM-SLOPFIX-06", ledger_file)) == 0
    assert not get_repo_lock_path(repo).exists()

    actions = [e.action for e in EvidenceLedger(str(ledger_file)).list_all_entries()]
    assert "unlock_requested" in actions
    assert "stale_lock_reclaimed" in actions


def test_unlock_refuses_a_repository_lock_owned_by_another_task(tmp_path):
    """A reclaim path, not an arbitrary lock remover."""
    repo = _repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    _write_lock(
        get_repo_lock_path(repo),
        "SOME-OTHER-TASK",
        999999,
        os.uname().nodename,
        "repository_mutation",
    )

    assert cmd_unlock(_unlock_args(repo, "HOWLFRAM-SLOPFIX-06", ledger_file)) == 1
    assert get_repo_lock_path(repo).exists(), "another task's lock was removed"

    entries = EvidenceLedger(str(ledger_file)).list_all_entries()
    assert any(e.action == "unlock_refused" for e in entries)


def test_unlock_refuses_a_live_repository_lock(tmp_path):
    """A running process is never displaced, no matter who asks."""
    repo = _repo(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(
            get_repo_lock_path(repo),
            "T-ACTIVE",
            proc.pid,
            os.uname().nodename,
            "repository_mutation",
            create_time=get_process_create_time(proc.pid),
        )
        assert cmd_unlock(_unlock_args(repo, "T-ACTIVE")) == 1
        assert get_repo_lock_path(repo).exists()
    finally:
        proc.kill()
        proc.wait()


def test_unlock_is_a_truthful_no_op_when_nothing_is_held(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cmd_unlock(_unlock_args(repo, "T-NONE")) == 0
    assert "Nothing to reclaim" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Recovery attempts are themselves durable evidence
# ---------------------------------------------------------------------------


def _seed_interrupted_run(repo: Path, task_id: str, state: str = "reviewing") -> Path:
    """Writes a task run stopped mid-lifecycle, as an interruption leaves it."""
    from src.control_plane.task_spec import TaskSpec

    run_dir = repo / ".task_runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = TaskSpec(task_id=task_id, repository=repo.name, objective="interrupted run")
    spec.current_state = state
    spec.save_to_file(str(run_dir / "task.yaml"))
    return run_dir


def test_a_failed_resume_is_recorded_and_leaks_nothing(tmp_path):
    """SLOPFIX-06's two resume attempts mutated progress and lock state and
    wrote nothing to the ledger, leaving no account of why recovery failed."""
    repo = _repo(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    _seed_interrupted_run(repo, "T-AUDIT")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(
            get_task_lock_path(repo, "T-AUDIT"),
            "T-AUDIT",
            proc.pid,
            os.uname().nodename,
            "task_run",
            create_time=get_process_create_time(proc.pid),
        )
        with pytest.raises(TaskLockedError):
            HumanLifecycleManager.resume(
                target_repo=repo,
                task_id="T-AUDIT",
                ledger=EvidenceLedger(str(ledger_file)),
            )
    finally:
        proc.kill()
        proc.wait()

    entries = EvidenceLedger(str(ledger_file)).list_all_entries()
    actions = [e.action for e in entries]
    assert "resume_requested" in actions
    assert "resume_lock_state" in actions
    assert "resume_failed" in actions

    failed = next(e for e in entries if e.action == "resume_failed")
    assert failed.result == "LOCK_UNAVAILABLE"
    # The evidence names which lock stood in the way and how it classified.
    assert failed.metadata["lock_state"]["locks"]["task_run"]["owner_state"] == "ACTIVE"

    assert not get_repo_lock_path(repo).exists(), "failed resume leaked a repo lock"


def test_a_failed_resume_does_not_overwrite_durable_progress(tmp_path):
    """SLOPFIX-06's progress.json was reset to PREPARING/RUNNING by a resume
    that then failed, destroying the record of the interrupted review."""
    from src.control_plane.atomic_io import safe_load_json

    repo = _repo(tmp_path)
    run_dir = _seed_interrupted_run(repo, "T-PROGRESS")
    original = {
        "task_id": "T-PROGRESS",
        "phase": "REVIEWING",
        "state": "RUNNING",
        "pid": 999999,
        "schema": "howlplane.task_progress/v1",
    }
    (run_dir / "progress.json").write_text(json.dumps(original), encoding="utf-8")

    blocker = TaskLock(repo, "T-PROGRESS", operation="resume")
    blocker.acquire()
    try:
        with pytest.raises(TaskLockedError):
            HumanLifecycleManager.resume(target_repo=repo, task_id="T-PROGRESS")
    finally:
        blocker.release()

    assert safe_load_json(run_dir / "progress.json")["phase"] == "REVIEWING"


# ---------------------------------------------------------------------------
# Review durability: resume reconstructs the persisted verdict, never re-derives it
# ---------------------------------------------------------------------------


def _persist_reviewer_state(cycle_dir: Path, role: str, status: str, transcript: str):
    """Writes the artifacts a reviewer leaves behind, including its verdict."""
    from src.control_plane.atomic_io import atomic_write_json

    cycle_dir.mkdir(parents=True, exist_ok=True)
    (cycle_dir / f"{role}.md").write_text(transcript, encoding="utf-8")
    (cycle_dir / f"{role}_findings.yaml").write_text("[]\n", encoding="utf-8")
    (cycle_dir / role).mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        cycle_dir / role / "result.json",
        {
            "role": role,
            "status": status,
            "findings_count": 0,
            "schema": "howlplane.review_result/v1",
        },
    )


@pytest.mark.parametrize(
    "status",
    [
        "clean",
        "findings_detected",
        "output_invalid",
        "malformed_output",
        "reviewer_failure",
    ],
)
def test_resume_preserves_the_exact_reviewer_verdict(tmp_path, status):
    """Status is read back, not re-derived. Re-deriving it from an empty
    findings list is what turned SLOPFIX-06's dead reviewer into a clean one."""
    from src.control_plane.review_runner import ReviewRunner

    cycle_dir = tmp_path / "reviews"
    _persist_reviewer_state(cycle_dir, "correctness-reviewer", status, "transcript\n")

    rebuilt = ReviewRunner._reconstruct_cached_review(
        cycle_dir, "correctness-reviewer", "Correctness"
    )
    assert rebuilt is not None
    assert rebuilt.status == status


def test_an_empty_transcript_never_reconstructs_as_clean(tmp_path):
    """The exact SLOPFIX-06 artifacts: a 0-byte transcript beside `[]`
    findings, with no persisted verdict because none was written then."""
    from src.control_plane.review_runner import ReviewRunner

    cycle_dir = tmp_path / "reviews"
    cycle_dir.mkdir(parents=True)
    (cycle_dir / "correctness-reviewer.md").write_text("", encoding="utf-8")
    (cycle_dir / "correctness-reviewer_findings.yaml").write_text("[]", encoding="utf-8")

    rebuilt = ReviewRunner._reconstruct_cached_review(
        cycle_dir, "correctness-reviewer", "Correctness"
    )
    assert rebuilt is not None
    assert rebuilt.status == "output_invalid"
    assert rebuilt.findings == []


def test_a_reviewer_that_never_ran_is_not_a_reviewer_that_found_nothing(tmp_path):
    from src.control_plane.review_runner import ReviewRunner

    cycle_dir = tmp_path / "reviews"
    cycle_dir.mkdir(parents=True)
    assert (
        ReviewRunner._reconstruct_cached_review(cycle_dir, "correctness-reviewer", "C")
        is None
    )


def test_an_invalid_review_is_re_run_on_resume_not_accepted(tmp_path):
    """The regression the canary needed: reviewer returns invalid output, the
    run is interrupted, and on resume the role is retried rather than silently
    passing."""
    from src.control_plane.review_runner import ReviewRunner
    from src.control_plane.task_spec import TaskSpec

    repo = _repo(tmp_path)
    run_dir = repo / ".task_runs" / "T-REREVIEW"
    cycle_dir = run_dir / "reviews"
    _persist_reviewer_state(cycle_dir, "correctness-reviewer", "output_invalid", "")

    calls = []

    def reviewer(role, _diff, _task):
        calls.append(role)
        return "findings: []\n"

    result = ReviewRunner.execute_review_cycle(
        task=TaskSpec(task_id="T-REREVIEW", repository=repo.name, objective="o"),
        diff_content="diff --git a/x b/x\n",
        reviewer_roles=["correctness-reviewer"],
        cwd=repo,
        cycle_index=1,
        run_dir=run_dir,
        custom_reviewer_fn=reviewer,
    )

    assert calls == ["correctness-reviewer"], "invalid review was not retried"
    assert result.reviewer_results["correctness-reviewer"].status == "clean"


# ---------------------------------------------------------------------------
# `ai status` reports durable dispositions, not file presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("clean", "completed_clean"),
        ("findings_detected", "completed_with_findings"),
        ("output_invalid", "invalid"),
        ("reviewer_failure", "failed"),
        ("malformed_output", "failed"),
    ],
)
def test_status_reports_the_durable_reviewer_disposition(tmp_path, status, expected):
    from src.control_plane.recovery import CrashRecoveryEngine

    reviews = tmp_path / "reviews"
    _persist_reviewer_state(reviews, "correctness-reviewer", status, "transcript\n")
    assert (
        CrashRecoveryEngine._reviewer_disposition(reviews, "correctness-reviewer")
        == expected
    )


def test_status_does_not_call_an_empty_review_completed(tmp_path):
    """`ai status` said "Completed Reviews: correctness-reviewer" for a reviewer
    whose transcript was zero bytes. File presence is not a verdict."""
    from src.control_plane.recovery import CrashRecoveryEngine

    reviews = tmp_path / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "correctness-reviewer.md").write_text("", encoding="utf-8")
    (reviews / "correctness-reviewer_findings.yaml").write_text("[]", encoding="utf-8")

    assert (
        CrashRecoveryEngine._reviewer_disposition(reviews, "correctness-reviewer")
        == "invalid"
    )


# ---------------------------------------------------------------------------
# Candidate lifecycle: routing stays provisional until governance accepts
# ---------------------------------------------------------------------------


def test_a_parked_candidate_is_never_recorded_as_an_accepted_implementation(tmp_path):
    """SLOPFIX-06's route evidence claimed `accepted_implementation_resource`,
    `final_route.accepted=true` and `reviewer_mapping_status=CONFIRMED` while
    the task was still `reviewing / in_progress`. Acceptance belongs to the
    governance boundary, not to implementation finishing."""
    from tests.test_provider_failover import (
        _init_test_repo,
        _refactor_preserving_behavior,
        _run_slopfix05,
    )

    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix05(repo, _refactor_preserving_behavior)
    meta = res.routing_decision.metadata

    # This run went all the way through verification and the authority gate,
    # so acceptance is legitimately true at the end...
    assert res.final_state == "complete"
    assert meta["accepted_implementation_resource"] == "resource_c"
    assert meta["reviewer_mapping_status"] == "CONFIRMED"

    # ...and the lifecycle stages are distinguishable from one another.
    assert meta["initial_implementation_resource"] == "resource_a"
    assert meta["last_attempted_implementation_resource"] == "resource_c"
    assert meta["candidate_resource"] == "resource_c"


def test_route_stays_provisional_while_the_candidate_is_still_under_review(tmp_path):
    """The state SLOPFIX-06 was actually in: candidate captured, review under
    way, nothing accepted. The route evidence on disk at that moment must not
    claim an accepted implementer."""
    from src.control_plane.atomic_io import safe_load_json
    from tests.test_provider_failover import (
        _init_test_repo,
        _make_registry_three_providers,
        _refactor_preserving_behavior,
        _run_failover_task,
        _slopfix05_resolver,
    )

    repo = _init_test_repo(tmp_path / "repo")
    observed = {}

    def observe_route_during_review(role, _diff, task):
        """Reads the durable route exactly while the candidate is under review."""
        if not observed:
            route = repo / ".task_runs" / task.task_id / "effective_route.json"
            observed.update(safe_load_json(route)["metadata"])
        return "findings: []\n"

    _run_failover_task(
        repo,
        _slopfix05_resolver(_refactor_preserving_behavior),
        max_attempts=3,
        registry=_make_registry_three_providers(),
        reviewer_fn=observe_route_during_review,
    )

    assert observed, "review never ran, so the provisional state was not observed"
    assert observed["candidate_resource"] == "resource_c"
    assert observed["accepted_implementation_resource"] is None
    assert observed["final_implementation_resource"] is None
    assert observed["final_route"]["accepted"] is False
    assert observed["reviewer_mapping_status"] == "CANDIDATE_REVIEW"


def test_the_final_attempt_states_why_no_provider_follows_it(tmp_path):
    """Attempt 3 omitted `rollback` and `next_selection` entirely, so "no
    provider remained" and "nobody wrote the field" looked identical."""
    from tests.test_provider_failover import (
        _init_test_repo,
        _refactor_preserving_behavior,
        _run_slopfix05,
    )

    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix05(repo, _refactor_preserving_behavior)
    final = res.implementation_attempts[2]

    assert final["attempt"] == 3
    assert final["max_attempts"] == 3
    assert final["next_selection"] is None
    assert final["next_selection_reason"] == "MAX_ATTEMPTS_REACHED"
    assert final["rollback"]["status"] == "PARKED_FOR_GOVERNANCE"
    assert final["rollback"]["restored"] is False
    assert final["transition"] == "CANDIDATE_GOVERNANCE"
    assert final["schema"] == "howlplane.implementation_attempt/v1"


# ---------------------------------------------------------------------------
# Provider scratch cannot impersonate canonical evidence
# ---------------------------------------------------------------------------


def test_provider_scratch_cannot_manufacture_a_fake_attempt(tmp_path):
    """A provider wrote `implementation/attempts/01-claude/workspace/`, giving a
    three-attempt run four attempt-shaped directories and no attempt record.
    Such a path is relocated into owned scratch and the empty shell removed, so
    the canonical attempt count stays truthful (HOWLFRAM-SLOPFIX-06)."""
    from tests.test_provider_failover import (
        _FakeBackendResolver,
        _edit_feature_to_true,
        _init_test_repo,
        _run_failover_task,
    )

    repo = _init_test_repo(tmp_path / "repo")

    def impersonate_a_canonical_attempt(task, cwd: Path, _prompt) -> None:
        fake = (
            Path(cwd)
            / ".task_runs"
            / task.task_id
            / "implementation"
            / "attempts"
            / "01-fake"
            / "workspace"
        )
        fake.mkdir(parents=True, exist_ok=True)
        (fake.parent / "probe.sh").write_text("#!/bin/sh\necho probe\n", encoding="utf-8")
        _edit_feature_to_true(task, cwd, _prompt)

    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            "resource_a": {
                "success": True,
                "side_effect": impersonate_a_canonical_attempt,
            },
        }),
        max_attempts=3,
    )

    run_dir = Path(res.run_dir)
    attempts_dir = run_dir / "implementation" / "attempts"

    # The fake attempt shell is gone, and only the real attempt remains.
    assert not (attempts_dir / "01-fake").exists()
    assert [d.name for d in sorted(attempts_dir.iterdir()) if d.is_dir()] == [
        "01-resource_a"
    ]
    assert len(res.implementation_attempts) == 1

    # Nothing was deleted: the contents live in owned scratch, attributed.
    manifest = json.loads((run_dir / "scratch_manifest.json").read_text(encoding="utf-8"))
    scratch = Path(manifest["attempts"]["01-resource_a"]["scratch_path"])
    relocated = scratch / "probe.sh"
    assert relocated.is_file()
    assert "echo probe" in relocated.read_text(encoding="utf-8")

    provenance = json.loads((scratch / "_provenance.json").read_text(encoding="utf-8"))
    assert provenance["origin"] == "provider_scratch"
    assert provenance["created_by"] == "resource_a"
    assert any("01-fake" in entry for entry in provenance["files"])


def test_the_real_attempt_directory_is_never_swept(tmp_path):
    """Control-plane evidence is owned, and the sweep must not touch it."""
    from tests.test_provider_failover import (
        _FakeBackendResolver,
        _edit_feature_to_true,
        _init_test_repo,
        _run_failover_task,
    )

    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _FakeBackendResolver(
            {"resource_a": {"success": True, "side_effect": _edit_feature_to_true}}
        ),
        max_attempts=3,
    )

    attempt_dir = Path(res.run_dir) / "implementation" / "attempts" / "01-resource_a"
    assert (attempt_dir / "attempt_record.json").is_file()
    assert (Path(res.run_dir) / "route.json").is_file()
    assert (Path(res.run_dir) / "task.yaml").is_file()


# ---------------------------------------------------------------------------
# The full SLOPFIX-06 lifecycle, end to end, on fake providers only
# ---------------------------------------------------------------------------


def _fake_orchestrator(repo: Path, task_id: str, registry):
    """An orchestrator wired to fake providers, with real locking.

    Resume normally builds its own orchestrator against live provider backends.
    A test must never reach a real provider, so it supplies one -- and keeps
    `acquire_locks=True`, because the nested task-lock acquisition is precisely
    what is under test.
    """
    from src.control_plane.orchestrator import (
        GovernedTaskOrchestrator,
        OrchestrationConfig,
    )
    from src.control_plane.synthesis.provider_pool import (
        ProviderPoolManager,
        ProviderResourceSettings,
    )
    from tests.test_provider_failover import _FakeBackendResolver

    resolver = _FakeBackendResolver(
        {r.resource_id: {"success": True} for r in registry.list_resources()}
    )
    pool = ProviderPoolManager(
        registry=registry,
        backend_resolver=resolver,
        probe_on_start=False,
        policy=None,
        resources={
            r.resource_id: ProviderResourceSettings(enabled=True)
            for r in registry.list_resources()
        },
        operating_mode="connected",
    )
    pool.policy.allow_paid_api = False
    config = OrchestrationConfig(
        provider_pool=pool,
        backend_resolver=resolver,
        custom_reviewer_fn=lambda role, diff, task: "findings: []\n",
        acquire_locks=True,
        enable_howlframe_audit=False,
        progress_mode="never",
        trajectory_store_dir=str(repo / ".task_runs" / task_id / "trajectories"),
    )
    return GovernedTaskOrchestrator(target_repo=repo, config=config)


def test_interrupted_candidate_review_resumes_and_completes(tmp_path):
    """The canary, reproduced and then recovered.

    A transport failure, a rolled-back budget failure, and a productive
    budget-stopped candidate parked for governance. The correctness review then
    returns invalid output and the run is interrupted mid-review, leaving a
    stale lock behind. `ai status` recommends resume; resume must actually work
    -- no self-deadlock, no leaked repository lock, an audited recovery, the
    invalid review still invalid, and acceptance only at the very end.
    """
    from src.control_plane.atomic_io import safe_load_json
    from src.control_plane.recovery import CrashRecoveryEngine
    from tests.test_provider_failover import (
        _init_test_repo,
        _make_registry_three_providers,
        _refactor_preserving_behavior,
        _run_failover_task,
        _slopfix05_resolver,
    )

    repo = _init_test_repo(tmp_path / "repo")
    ledger_file = tmp_path / "ledger.jsonl"
    task_id = "TEST-FAILOVER-01"
    run_dir = repo / ".task_runs" / task_id

    # --- Interrupted run: the correctness review returns nothing usable, and
    #     the process dies before the cycle is acted on ----------------------
    reviews_seen = []

    def correctness_returns_nothing(role, _diff, _task):
        reviews_seen.append(role)
        # Launched, exit 0, produced nothing: output_invalid, never "clean".
        return "" if role == "correctness-reviewer" else "findings: []\n"

    def die_after_the_review_cycle(stage, _run_dir, _spec):
        if stage == "post_review":
            raise RuntimeError("interrupted during candidate review")

    with pytest.raises(RuntimeError):
        _run_failover_task(
            repo,
            _slopfix05_resolver(_refactor_preserving_behavior),
            max_attempts=3,
            registry=_make_registry_three_providers(),
            reviewer_fn=correctness_returns_nothing,
            failure_injection_hook=die_after_the_review_cycle,
        )
    assert "correctness-reviewer" in reviews_seen

    # The candidate exists, is parked, and is not accepted.
    meta = safe_load_json(run_dir / "effective_route.json")["metadata"]
    assert meta["candidate_resource"] == "resource_c"
    assert meta["accepted_implementation_resource"] is None

    # The invalid review is durably invalid, not clean.
    assert (
        CrashRecoveryEngine._reviewer_disposition(
            run_dir / "reviews", "correctness-reviewer"
        )
        == "invalid"
    )

    # A stale lock is left behind, exactly as the canary left one.
    _write_lock(
        get_repo_lock_path(repo), task_id, 999999, os.uname().nodename,
        "repository_mutation",
    )

    # --- ai status recommends resume, and ai unlock can act on that lock -----
    diagnosis = CrashRecoveryEngine.inspect_task(repo, task_id)
    assert "correctness-reviewer" not in diagnosis["completed_reviewers"]
    assert cmd_unlock(_unlock_args(repo, task_id, ledger_file)) == 0
    assert not get_repo_lock_path(repo).exists()

    # --- Resume: no self-deadlock, no leak, audited -------------------------
    # Locks are real here -- that is the point: resume holds the task lock and
    # hands its ownership to the orchestrator, which used to deadlock against
    # it. Backends stay fake so no provider quota is touched.
    resumed_orchestrator = _fake_orchestrator(
        repo, task_id, registry=_make_registry_three_providers()
    )
    result = HumanLifecycleManager.resume(
        target_repo=repo,
        task_id=task_id,
        orchestrator=resumed_orchestrator,
        ledger=EvidenceLedger(str(ledger_file)),
    )

    assert result.final_state == "complete"
    assert not get_repo_lock_path(repo).exists(), "resume leaked the repository lock"
    assert not get_task_lock_path(repo, task_id).exists(), "resume leaked the task lock"

    actions = [e.action for e in EvidenceLedger(str(ledger_file)).list_all_entries()]
    assert "resume_requested" in actions
    assert "resume_started" in actions
    assert "resume_completed" in actions

    # --- Acceptance happened, and only after the governance boundary --------
    final_meta = safe_load_json(run_dir / "effective_route.json")["metadata"]
    assert final_meta["accepted_implementation_resource"] == "resource_c"
    assert final_meta["final_route"]["accepted"] is True
    assert final_meta["reviewer_mapping_status"] == "CONFIRMED"
    assert final_meta["candidate_resource"] == "resource_c"

    # The reviewer that had returned nothing was re-run, not accepted as clean.
    assert (
        CrashRecoveryEngine._reviewer_disposition(
            run_dir / "reviews", "correctness-reviewer"
        )
        in ("completed_clean", "completed_with_findings")
    )


def test_status_recommends_a_command_that_can_actually_work(tmp_path):
    """A recommendation is only useful if it names a working command. A
    blocking repository lock had no branch at all, so `ai status` sent the
    operator to `ai resume`, which could not succeed."""
    from src.control_plane.recovery import CrashRecoveryEngine

    repo = _repo(tmp_path)
    _seed_interrupted_run(repo, "T-RECOMMEND")
    _write_lock(
        get_repo_lock_path(repo),
        "T-RECOMMEND",
        999999,
        "some-other-box",
        "repository_mutation",
    )

    diagnosis = CrashRecoveryEngine.inspect_task(repo, "T-RECOMMEND")
    assert diagnosis["repo_lock_state"] == "AMBIGUOUS"
    assert "ai unlock T-RECOMMEND" in diagnosis["recommendation"]


def test_a_second_process_cannot_take_an_actively_held_task(tmp_path):
    """Reentrancy is scoped to a lifecycle; concurrency control is unchanged."""
    repo = _repo(tmp_path)
    holder = TaskLock(repo, "T-CONCURRENT", operation="orchestrate")
    holder.acquire()
    try:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from src.control_plane.locking import TaskLock, TaskLockedError\n"
            "try:\n"
            "    TaskLock(%r, 'T-CONCURRENT', operation='orchestrate').acquire()\n"
            "    print('ACQUIRED')\n"
            "except TaskLockedError:\n"
            "    print('BLOCKED')\n"
        ) % (str(Path.cwd()), str(repo))
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, cwd=Path.cwd(),
        )
        assert "BLOCKED" in out.stdout, out.stdout + out.stderr
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-07R: retained salvage state must survive an interruption
#
# The fallback is discoverable only through durable attempt evidence, so an
# interruption anywhere in the retention lifecycle must leave it findable,
# unapplied twice, and still honestly marked as not-yet-promoted.
# ---------------------------------------------------------------------------
def _die_at(target_stage: str):
    """Interrupts the run at one named lifecycle point."""
    def _hook(stage, _run_dir, _spec):
        if stage == target_stage:
            raise RuntimeError(f"interrupted at {target_stage}")
    return _hook


def _crash_during_salvage(tmp_path, crash_stage):
    """Interrupts the salvage chain at one stage; returns repo and evidence dirs."""
    from tests.test_provider_failover import _init_test_repo, _run_salvage_chain

    repo = _init_test_repo(tmp_path / "repo")
    with pytest.raises(RuntimeError):
        _run_salvage_chain(repo, failure_injection_hook=_die_at(crash_stage))
    run_dir = repo / ".task_runs" / "TEST-FAILOVER-01"
    return repo, run_dir, run_dir / "implementation" / "attempts"


@pytest.mark.parametrize(
    "crash_stage", ["after_salvage_retention", "during_salvage_promotion"],
)
def test_retained_fallback_survives_an_interruption(tmp_path, crash_stage):
    """Crashing after retention, and again mid-promotion, must not lose the
    fallback, promote it twice, or let its record claim a promotion that never
    finished."""
    import hashlib

    from src.control_plane.orchestrator import GovernedTaskOrchestrator

    _, run_dir, attempts_dir = _crash_during_salvage(tmp_path, crash_stage)

    # The fallback is still discoverable, and still honestly un-promoted.
    record = GovernedTaskOrchestrator._select_retained_salvage(attempts_dir)
    assert record is not None, "the retained fallback must survive the crash"
    retained = record["retained_salvage"]
    assert retained["resource_id"] == "resource_a"
    assert retained["eligibility"] == "ELIGIBLE"
    assert retained["promotion_status"] == "RETAINED"
    assert retained["provider_completion_claim"] is False
    assert "promoted_at" not in retained

    # Its identity is intact, so a later promotion can still prove it belongs
    # to this baseline rather than applying it blind.
    patch_file = run_dir / retained["patch_path"]
    assert patch_file.is_file()
    assert hashlib.sha256(
        patch_file.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest() == retained["patch_sha256"]

    # An unfinished promotion never becomes a candidate.
    assert not (attempts_dir / "01-resource_a" / "candidate.json").exists()


def _resume_run(repo: Path, registry):
    """Resumes an interrupted run the way `ai resume` does."""
    task_id = "TEST-FAILOVER-01"
    return HumanLifecycleManager.resume(
        target_repo=repo,
        task_id=task_id,
        orchestrator=_fake_orchestrator(repo, task_id, registry),
        ledger=EvidenceLedger(str(repo / ".task_runs" / task_id / "ledger.jsonl")),
    )


def _route_metadata(run_dir: Path) -> dict:
    from src.control_plane.atomic_io import safe_load_json

    return safe_load_json(run_dir / "effective_route.json")["metadata"]


def test_resume_after_an_interrupted_promotion_governs_the_fallback(tmp_path):
    """The promotion crash window leaves the artifact applied with nothing
    describing it. Resume must rebuild the candidate the uninterrupted path
    would have written -- not silently adopt the diff as attested work."""
    from tests.test_provider_failover import _make_registry_with_spare_reviewer

    repo, run_dir, attempts_dir = _crash_during_salvage(
        tmp_path, "during_salvage_promotion"
    )
    res = _resume_run(repo, _make_registry_with_spare_reviewer())

    # The producer never claimed completion, and resuming does not invent one.
    assert res.implementation_completion_claim is False
    assert res.candidate_origin == "timed_out_implementation_attempt"

    record = json.loads(
        (attempts_dir / "01-resource_a" / "attempt_record.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["retained_salvage"]["promotion_status"] == "PROMOTED"
    assert (attempts_dir / "01-resource_a" / "candidate.json").is_file()

    # The producer is credited as the candidate; the chain still ended on C.
    meta = _route_metadata(run_dir)
    assert meta["candidate_resource"] == "resource_a"
    assert meta["last_attempted_implementation_resource"] == "resource_c"

    # Governing an artifact that already exists is not a fourth attempt.
    assert sorted(d.name for d in attempts_dir.iterdir()) == [
        "01-resource_a", "02-resource_b", "03-resource_c",
    ]


def test_resume_does_not_credit_a_producer_for_another_resources_work(tmp_path):
    """A retained fallback whose work was rolled back must never be stamped onto
    a later provider's successful diff. Doing so would launder discarded work
    into an accepted implementation and hide a self-review as independent."""
    from tests.test_provider_failover import (
        _budget_kill,
        _edit_feature_to_false,
        _init_test_repo,
        _make_registry_with_spare_reviewer,
        _refactor_preserving_behavior,
        _run_salvage_chain,
    )

    repo = _init_test_repo(tmp_path / "repo")
    # A and B leave deliberately different work, so crediting the wrong one is
    # observable rather than a coincidence of identical diffs.
    with pytest.raises(RuntimeError):
        _run_salvage_chain(
            repo,
            a=_budget_kill(_edit_feature_to_false),
            b={"success": True, "side_effect": _refactor_preserving_behavior},
            failure_injection_hook=_die_at("reviewing"),
        )
    res = _resume_run(repo, _make_registry_with_spare_reviewer())

    meta = _route_metadata(repo / ".task_runs" / "TEST-FAILOVER-01")
    assert meta["accepted_implementation_resource"] == "resource_b"
    assert meta["candidate_resource"] is None
    assert res.candidate_origin is None
    assert res.implementation_completion_claim is True

    # B's work is what landed; A's rolled-back fragment is history.
    feature = (repo / "src" / "feature.py").read_text(encoding="utf-8")
    assert "Returns the feature flag" in feature
    assert "return False" not in feature


def test_resume_refuses_a_delta_that_is_not_the_retained_artifact(tmp_path):
    """The identity gate, directly. A retained record plus an unrelated delta in
    the tree must not resolve to the retained producer."""
    from src.control_plane.atomic_io import safe_load_json
    from src.control_plane.git_baseline import GitBaseline, capture_delta
    from tests.test_provider_failover import (
        _make_registry_with_spare_reviewer,
        _make_task,
    )

    repo, run_dir, _ = _crash_during_salvage(tmp_path, "after_salvage_retention")
    # Work that is emphatically not the retained artifact.
    (repo / "src" / "feature.py").write_text(
        "def run():\n    return 'someone else'\n", encoding="utf-8"
    )
    baseline = GitBaseline.from_dict(safe_load_json(run_dir / "baseline.json"))
    orchestrator = _fake_orchestrator(
        repo, "TEST-FAILOVER-01", _make_registry_with_spare_reviewer()
    )

    assert orchestrator._recover_promoted_salvage(
        run_dir, capture_delta(repo, baseline), _make_task()
    ) is None
