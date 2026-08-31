#!/usr/bin/env python3
"""Self-tests proving the factory acceptance fakes are deterministic."""

from datetime import datetime, timedelta, timezone

import pytest

from factory_acceptance_harness import (
    CRASH_MATRIX_BOUNDARIES,
    CrashInjector,
    FakeAuthority,
    FakeClock,
    FakeRepository,
    InjectedCrash,
    ProviderRecord,
    ProviderTransition,
    ScriptedProviderPool,
    assert_bounded_waits,
)


START = datetime(2026, 8, 31, tzinfo=timezone.utc)


def test_fake_clock_advances_days_without_wall_clock_sleep():
    clock = FakeClock(START)
    clock.sleep(72 * 60 * 60)
    assert clock.now() == START + timedelta(hours=72)
    assert clock.sleep_calls == [259200]


def test_provider_transitions_apply_only_when_fake_time_reaches_them():
    clock = FakeClock(START)
    pool = ScriptedProviderPool(
        clock,
        [ProviderRecord("claude")],
        [
            ProviderTransition(
                START + timedelta(hours=1),
                "claude",
                "SESSION_EXHAUSTED",
                START + timedelta(hours=6),
            ),
            ProviderTransition(
                START + timedelta(hours=6),
                "claude",
                "AVAILABLE",
            ),
        ],
    )
    assert pool.has_available_providers() is True
    clock.advance(60 * 60)
    assert pool.has_available_providers() is False
    assert pool.inventory()[0]["retry_after"] == (
        START + timedelta(hours=6)
    ).isoformat()
    clock.advance(5 * 60 * 60)
    assert pool.has_available_providers() is True


def test_non_generative_reprobe_does_not_increment_inference_attempts():
    clock = FakeClock(START)
    record = ProviderRecord(
        "codex", capacity="QUOTA_EXHAUSTED", retry_after=START + timedelta(hours=1)
    )
    pool = ScriptedProviderPool(clock, [record])
    assert pool.reprobe_due() == []
    clock.advance(60 * 60)
    assert pool.reprobe_due() == ["codex"]
    assert record.readiness_checks == 1
    assert record.generative_attempts == 0


def test_crash_injector_is_one_shot_at_each_named_boundary():
    injector = CrashInjector("after_observation_persisted")
    with pytest.raises(InjectedCrash, match="after_observation_persisted"):
        injector.reach("after_observation_persisted")
    injector.reach("after_observation_persisted")
    assert injector.seen == [
        "after_observation_persisted",
        "after_observation_persisted",
    ]


@pytest.mark.parametrize("boundary", CRASH_MATRIX_BOUNDARIES)
def test_every_required_crash_matrix_boundary_is_one_shot_injectable(boundary):
    injector = CrashInjector(boundary)
    with pytest.raises(InjectedCrash, match=boundary):
        injector.reach(boundary)
    injector.reach(boundary)


def test_fake_authority_never_allows_self_expansion():
    authority = FakeAuthority(
        authorized_repositories=["howlcipher/howlplane"],
        allowed_actions=["commit_task_changes", "authority_profile_modification"],
        never_delegatable=["authority_profile_modification"],
    )
    assert authority.evaluate(
        "howlcipher/howlplane", "commit_task_changes"
    ) == "ALLOW"
    assert authority.evaluate(
        "howlcipher/howlplane", "authority_profile_modification"
    ) == "PARK_HUMAN"
    assert authority.evaluate(
        "howlcipher/ungranted", "commit_task_changes"
    ) == "PARK_HUMAN"


def test_pull_request_creation_reconciles_by_branch_after_crash():
    injector = CrashInjector("after_pull_request_accepted")
    repository = FakeRepository("howlcipher/howlplane", injector)
    repository.create_branch("fix/WI-1", "abc123")
    with pytest.raises(InjectedCrash):
        repository.create_pull_request("fix/WI-1")

    recovered = repository.create_pull_request("fix/WI-1")
    assert recovered.number == 1
    assert len(repository.pull_requests) == 1
    assert repository.observe_pull_request("fix/WI-1") is recovered


def test_merge_refuses_absent_failed_or_cancelled_required_checks():
    repository = FakeRepository("howlcipher/howlplane")
    repository.create_branch("fix/WI-1", "abc123")
    pull_request = repository.create_pull_request("fix/WI-1")
    for state in (None, "FAILURE", "CANCELLED", "PENDING"):
        pull_request.checks = {} if state is None else {"test": state}
        with pytest.raises(PermissionError, match="required checks"):
            repository.merge(pull_request.number, ["test"])


def test_merge_requires_at_least_one_required_check():
    repository = FakeRepository("howlcipher/howlplane")
    repository.create_branch("fix/WI-1", "abc123")
    pull_request = repository.create_pull_request("fix/WI-1")
    with pytest.raises(PermissionError, match="required checks"):
        repository.merge(pull_request.number, [])


def test_merge_succeeds_once_only_when_required_checks_are_green():
    repository = FakeRepository("howlcipher/howlplane")
    repository.create_branch("fix/WI-1", "abc123")
    pull_request = repository.create_pull_request("fix/WI-1")
    repository.set_checks(pull_request.number, {"test": "SUCCESS"})

    merge_sha = repository.merge(pull_request.number, ["test"])

    assert merge_sha == "merge-abc123"
    assert pull_request.state == "MERGED"
    assert repository.merged_shas == {merge_sha}


def test_wait_detector_rejects_hot_loop_and_accepts_bounded_waits():
    clock = FakeClock(START)
    clock.sleep(30)
    assert_bounded_waits(clock)

    broken = FakeClock(START)
    broken.sleep(0)
    with pytest.raises(AssertionError):
        assert_bounded_waits(broken)
