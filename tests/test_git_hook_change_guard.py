"""HowlPlane's own commits keep the user's git hooks but refuse hooks that changed mid-task (DOG-034).

An agent can write `.git/hooks/*` and `.git/config` (core.hooksPath). Reviewers
never see those in the diff, and git would run them when HowlPlane commits the
reviewed work. The user's hooks, present when the task branch was created, keep
running as before.
"""

import pytest

from howlplane.control_plane.git_integration import GitIntegrationError, GitIntegrationExecutor
from tests._git_test_helpers import git_in_repo, init_git_repo

pytestmark = pytest.mark.contract


def hook(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    path.chmod(0o755)


@pytest.fixture
def task(tmp_path):
    origin = tmp_path / "origin.git"
    git_in_repo(tmp_path, ["init", "--bare", "-b", "main", str(origin)])
    repo = init_git_repo(tmp_path / "repo", files={"app.py": "x = 1\n"})
    git_in_repo(repo, ["remote", "add", "origin", str(origin)])
    git_in_repo(repo, ["push", "-q", "origin", "main"])
    user_marker = tmp_path / "user-hook-ran"
    hook(repo / ".git" / "hooks" / "pre-commit", user_marker)
    executor = GitIntegrationExecutor(repo_root=repo, repo_slug="o/r", envelope=None)
    executor.create_task_branch("T-1")
    (repo / "app.py").write_text("x = 2\n")
    return repo, executor, user_marker, tmp_path / "planted-hook-ran"


def test_the_users_own_hook_still_runs_on_howlplane_commits(task):
    repo, executor, user_marker, _ = task
    executor.stage_and_commit(["app.py"], "fix: x")
    assert user_marker.read_text() == "ran\n"


@pytest.mark.parametrize("plant", ["new hook", "changed hook", "hooksPath redirect"])
def test_hooks_changed_during_the_task_block_the_commit_unrun(task, plant):
    repo, executor, user_marker, planted = task
    head = git_in_repo(repo, ["rev-parse", "HEAD"])
    if plant == "new hook":
        hook(repo / ".git" / "hooks" / "commit-msg", planted)
    elif plant == "changed hook":
        hook(repo / ".git" / "hooks" / "pre-commit", planted)
    else:
        hook(repo / "elsewhere" / "pre-commit", planted)
        git_in_repo(repo, ["config", "core.hooksPath", str(repo / "elsewhere")])

    with pytest.raises(GitIntegrationError, match="hook setup changed while the task ran"):
        executor.stage_and_commit(["app.py"], "fix: x")

    assert not planted.exists() and not user_marker.exists()
    assert git_in_repo(repo, ["rev-parse", "HEAD"]) == head
