#!/usr/bin/env python3
"""Factory Git, pull-request, CI, and crash-idempotency contracts."""

from dataclasses import fields
import json

import pytest

from src.control_plane.git_integration import (
    GitIntegrationExecutor,
    GitHubCIObserver,
)
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.synthesis.campaign_state import GitIntegrationRecord
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from tests._dogfood_test_helpers import ScriptedRunner


REPOSITORY = "howlcipher/howlplane"


def _existing_pr_query(task_id, number, url):
    branch = f"fix/{task_id}"
    gh = ScriptedRunner()
    gh.on(
        ["pr", "list", "--head", branch, "--json", "number,url,state"],
        stdout=json.dumps(
            [{"number": number, "url": url, "state": "OPEN"}]
        ),
    )
    executor = GitIntegrationExecutor(
        "/fake/repo",
        REPOSITORY,
        envelope=None,
        git_runner=ScriptedRunner(),
        gh_runner=gh,
    )
    action = ProposedAction(
        action_type="create_pull_request",
        target_repo=REPOSITORY,
        arguments={"branch": branch},
    )
    return executor.query_execution_status(
        "decision", "/fake/repo", "/fake/run", action, task_id
    )


def test_existing_pull_request_is_discovered_by_stable_task_branch():
    status, receipt, message = _existing_pr_query(
        "FACTORY-1", 71, "https://github.com/howlcipher/howlplane/pull/71"
    )

    assert status == "already_executed"
    assert receipt is None
    assert "PR already exists" in message


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: marathon Git steps execute directly without consulting "
        "query_execution_status, so a crash after GitHub accepts a PR can replay"
    ),
)
def test_factory_git_step_queries_external_truth_before_reexecution(tmp_path):
    class Executor:
        query_calls = 0
        execute_calls = 0

        def evaluate(self, action, repo_path, run_dir):
            return "ALLOW", "decision", "allowed"

        def query_execution_status(
            self, decision_id, repo_path, run_dir, action, task_id
        ):
            self.query_calls += 1
            return "already_executed", None, "PR #71 already exists"

        def execute(self, decision_id, repo_path, run_dir, action, task_id):
            self.execute_calls += 1
            raise AssertionError("duplicate external mutation attempted")

    executor = Executor()
    engine = MarathonDogfoodEngine(
        base_output_dir=tmp_path / "out",
        campaign_dir=tmp_path / "campaigns",
        target_repo=tmp_path,
        repo_slug=REPOSITORY,
    )
    engine.git_executor = executor
    record = GitIntegrationRecord(
        task_id="FACTORY-1", target_repo=REPOSITORY
    )

    engine._authorize_and_execute_git_step(
        "create_pull_request",
        {"branch": "fix/FACTORY-1"},
        "FACTORY-1",
        tmp_path / "run",
        record,
    )
    assert executor.query_calls == 1
    assert executor.execute_calls == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: existing-PR reconciliation reports only a message and "
        "does not return durable PR number or URL metadata needed to resume CI"
    ),
)
def test_existing_pull_request_reconciliation_returns_resume_metadata():
    names = {field.name for field in fields(GitIntegrationRecord)}
    assert "idempotency_key" in names

    status, receipt, message = _existing_pr_query(
        "FACTORY-2", 72, "https://example.test/pr/72"
    )
    assert status == "already_executed"
    assert receipt is not None
    assert receipt.native_receipt["pr_number"] == 72


def _ruleset(required):
    checks = [{"context": name} for name in required]
    return json.dumps(
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": checks},
            }
        ]
    )


@pytest.mark.parametrize(
    "bucket,state",
    [
        ("fail", "FAILURE"),
        ("cancel", "CANCELLED"),
        ("pending", "PENDING"),
        pytest.param(
            "skipping",
            "SKIPPED",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "BLOCKING_AUTONOMOUS_LOW_RISK_MERGE: a skipped required "
                    "check is currently classified as terminal green"
                ),
            ),
        ),
    ],
)
def test_ci_non_success_states_never_authorize_merge(bucket, state):
    gh = ScriptedRunner()
    gh.on(
        ["api", f"repos/{REPOSITORY}/rules/branches/main"],
        stdout=_ruleset(["test-python"]),
    )
    gh.on(
        ["pr", "checks", "73", "--json", "name,state,bucket,link"],
        stdout=json.dumps(
            [
                {
                    "name": "test-python",
                    "state": state,
                    "bucket": bucket,
                    "link": "",
                }
            ]
        ),
    )
    observation = GitHubCIObserver(
        gh_runner=gh, git_runner=ScriptedRunner()
    ).observe_once("/fake/repo", 73, REPOSITORY)
    assert observation.authorizes_merge() is False


def test_absent_required_check_never_authorizes_merge():
    gh = ScriptedRunner()
    gh.on(
        ["api", f"repos/{REPOSITORY}/rules/branches/main"],
        stdout=_ruleset(["test-python", "test-go"]),
    )
    gh.on(
        ["pr", "checks", "74", "--json", "name,state,bucket,link"],
        stdout=json.dumps(
            [
                {
                    "name": "test-python",
                    "state": "SUCCESS",
                    "bucket": "pass",
                    "link": "",
                }
            ]
        ),
    )
    observation = GitHubCIObserver(
        gh_runner=gh, git_runner=ScriptedRunner()
    ).observe_once("/fake/repo", 74, REPOSITORY)
    assert observation.all_required_observed is False
    assert observation.authorizes_merge() is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_AUTONOMOUS_LOW_RISK_MERGE: GitIntegrationRecord does not bind "
        "the observed CI verdict to the pull request head SHA"
    ),
)
def test_new_commit_invalidates_prior_ci_observation():
    names = {field.name for field in fields(GitIntegrationRecord)}
    assert "ci_observed_head_sha" in names
    assert "current_pr_head_sha" in names
