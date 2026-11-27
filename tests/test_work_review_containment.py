"""`howlplane work` reviewers run read-only, and a CLI that cannot enforce it never reviews (issues.md #27).

The `work` path names its reviewers `correctness-reviewer`, `security-reviewer`
and so on. The agy, Devin and Cursor launchers used to pick read-only mode only
for the literal roles `planning`, `review` and `acceptance`, so every `work`
review ran agy with `--mode accept-edits` (seen in HP-SELF-25). Codex likewise
gave the test falsifier a writable workspace. Now one predicate,
`is_mutating_role`, decides for every launcher; agy, whose `--mode plan` does
not enforce read-only operation (DOG-037), is excluded from such roles by the
pool; and a review attempt that changes the repository is discarded.
"""

import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane import orchestration
from howlplane.control_plane.agent_execution import (
    AgentBackendRegistry,
    AgentExecutionResult,
    AgyBackend,
    CodexBackend,
    CursorBackend,
    DevinCLIBackend,
)
from howlplane.control_plane.provider_execution_profile import is_mutating_role
from howlplane.control_plane.review_runner import (
    MUTATED_REPOSITORY_OUTCOME,
    invoke_reviewer_with_failover,
    reviewer_failure_message,
)
from howlplane.control_plane.reviewers import REVIEWER_ROLES
from howlplane.control_plane.task_spec import TaskSpec
from tests.test_ai_resource_pool import CountingBackend, make_pool, make_profile, make_task

pytestmark = pytest.mark.contract

READ_ONLY = ("planning", "review", "acceptance", "verification", "writing:summary", *sorted(REVIEWER_ROLES))
EVERY_ROLE = (*READ_ONLY, "implementation", "remediation")


def command(backend, role):
    return backend().build_command(TaskSpec(task_id="T", repository="r", objective="o"), Path("/repo"), role, "p")


@pytest.mark.parametrize("role", EVERY_ROLE)
def test_every_launcher_runs_a_role_that_may_not_change_the_repository_read_only(role):
    mutating = is_mutating_role(role)
    assert mutating is (role in ("implementation", "remediation"))

    agy = command(AgyBackend, role)
    assert agy[agy.index("--mode") + 1] == ("accept-edits" if mutating else "plan")
    devin = command(DevinCLIBackend, role)
    assert devin[devin.index("--permission-mode") + 1] == ("accept-edits" if mutating else "auto")
    assert ("workspace-write" in command(CodexBackend, role)) is mutating
    cursor = command(CursorBackend, role)
    expected_mode = None if mutating else ("plan" if role == "planning" else "ask")
    assert (cursor[cursor.index("--mode") + 1] if "--mode" in cursor else None) == expected_mode


def test_the_backends_declare_which_cli_cannot_enforce_read_only():
    assert AgyBackend.enforces_read_only is False
    assert AgentBackendRegistry.read_only_unenforced() == frozenset({"agy"})
    assert orchestration.READ_ONLY_UNENFORCED == AgentBackendRegistry.read_only_unenforced()


class UnenforcedBackend(CountingBackend):
    enforces_read_only = False


@pytest.mark.parametrize("role", ["review", "correctness-reviewer", "security-reviewer", "test-falsifier"])
def test_the_pool_never_offers_a_read_only_role_to_a_cli_that_cannot_enforce_it(role):
    profiles = [make_profile("enforced"), make_profile("unenforced")]
    pool = make_pool(profiles, backends={"enforced": CountingBackend("enforced"),
                                         "unenforced": UnenforcedBackend("unenforced")})

    decision = pool.select_resource(make_task(), role=role)

    assert [r.resource_id for r in decision.eligible_resources] == ["enforced"]
    exclusion = decision.exclusion_for("unenforced")
    assert (exclusion.reason, exclusion.stage) == ("READ_ONLY_NOT_ENFORCED", "authority")
    assert "unenforced" not in pool.select_candidates(task=make_task(), role=role)


def test_a_cli_that_cannot_enforce_read_only_still_implements():
    profiles = [make_profile("unenforced")]
    pool = make_pool(profiles, backends={"unenforced": UnenforcedBackend("unenforced")})

    for role in ("implementation", "remediation"):
        assert pool.select_resource(make_task(), role=role).selected.resource_id == "unenforced"


# -- the failover loop ---------------------------------------------------------------------------

def git_repo(path: Path) -> Path:
    path.mkdir()
    for args in (["init", "-q"], ["-c", "user.email=t@example.invalid", "-c", "user.name=t",
                                  "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "candidate.py").write_text("print('the change under review')\n")
    return path


class Reviewer:
    """A reviewer CLI stand-in; `writes` makes it change the tree it reviews."""

    def __init__(self, resource_id, launches, writes=False, enforces_read_only=True):
        self.resource_id = resource_id
        self.launches = launches
        self.writes = writes
        self.enforces_read_only = enforces_read_only

    def is_available(self):
        return True

    def execute(self, task, cwd, role, prompt_override=None, **kwargs):
        self.launches.append(self.resource_id)
        if self.writes:
            (Path(cwd) / "candidate.py").write_text("print('rewritten by the reviewer')\n")
        return AgentExecutionResult(agent_id=self.resource_id, role=role, command=self.resource_id,
                                    exit_code=0, stdout="findings: []\n", stderr="",
                                    duration_seconds=0.1, success=True)


def review(tmp_path, reviewers):
    launches = []
    built = {rid: Reviewer(rid, launches, **options) for rid, options in reviewers.items()}
    task = TaskSpec(task_id="T-27", repository="r", objective="o")
    winner, result, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer", candidates=list(reviewers), task=task, cwd=git_repo(tmp_path / "repo"),
        prompt_override="review", backend_lookup=built.get)
    return winner, attempts, launches


def test_failover_never_launches_a_reviewer_that_cannot_enforce_read_only(tmp_path):
    winner, attempts, launches = review(tmp_path, {"unenforced": {"enforces_read_only": False}, "enforced": {}})

    assert winner == "enforced" and launches == ["enforced"]
    assert attempts[0] == {**attempts[0], "provider": "unenforced", "outcome": "unavailable",
                           "reason": "read_only_not_enforced", "consumed_launch_budget": False}


def test_a_reviewer_that_changes_the_repository_is_discarded_and_no_one_else_sees_the_tree(tmp_path):
    winner, attempts, launches = review(tmp_path, {"writer": {"writes": True}, "clean": {}})

    assert winner is None
    assert launches == ["writer"], "the next reviewer is never shown the changed tree"
    assert attempts[-1]["outcome"] == MUTATED_REPOSITORY_OUTCOME
    message = reviewer_failure_message("correctness-reviewer", attempts)
    assert "writer changed the repository during the correctness-reviewer review" in message


def test_a_reviewer_that_leaves_the_tree_alone_is_accepted(tmp_path):
    winner, attempts, launches = review(tmp_path, {"clean": {}})

    assert winner == "clean" and attempts[-1]["outcome"] == "completed"
    assert reviewer_failure_message("correctness-reviewer", []) == "All candidate reviewers failed or were unavailable"
