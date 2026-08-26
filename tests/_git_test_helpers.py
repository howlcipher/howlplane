"""tests/_git_test_helpers.py

The single supported way for tests to build and mutate disposable Git
repositories.

Tests used to call `subprocess.run(["git", "init"], cwd=tmp_path)` directly.
That is unsafe: Git exports GIT_DIR and friends to the hooks it runs, and those
variables override `cwd=` (and `git -C`), so under a `pre-push` hook every such
call landed on the real repository instead of the temporary one. See
`src/control_plane/git_env` for the empirical variable-by-variable evidence.

Not itself a test module -- pytest only collects `test_*.py`/`*_test.py`.
"""

from pathlib import Path
from typing import List, Optional, Union

from src.control_plane.git_env import run_git_in_repo

DEFAULT_TEST_USER_EMAIL = "ci@howlplane.local"
DEFAULT_TEST_USER_NAME = "HowlPlane CI"


def git_in_repo(repo: Union[str, Path], args: List[str]) -> str:
    """Runs one git command against `repo`, raising on failure.

    Always use this (or `init_git_repo`) instead of a bare `subprocess.run`,
    so an inherited GIT_DIR cannot redirect the command elsewhere.
    """
    result = run_git_in_repo(repo, args)
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout


def init_git_repo(
    path: Union[str, Path],
    files: Optional[dict] = None,
    initial_commit: bool = True,
    branch: str = "main",
    commit_message: str = "Initial commit",
) -> Path:
    """Creates a disposable Git repository at `path` with a local identity.

    Args:
        path: Directory to initialize. Created if missing.
        files: Optional repo-relative path -> content mapping written before
            the initial commit.
        initial_commit: When False, files are written and staged but not
            committed, leaving the repository without a HEAD.
        branch: Initial branch name.
        commit_message: Message for the initial commit.
    """
    repo = Path(path)
    repo.mkdir(parents=True, exist_ok=True)
    git_in_repo(repo, ["init", "-b", branch])
    git_in_repo(repo, ["config", "user.email", DEFAULT_TEST_USER_EMAIL])
    git_in_repo(repo, ["config", "user.name", DEFAULT_TEST_USER_NAME])

    for relative_path, content in (files or {}).items():
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if initial_commit:
        commit_all(repo, commit_message)
    return repo


def commit_all(repo: Union[str, Path], message: str) -> str:
    """Stages every change in `repo` and commits it, returning the new SHA."""
    git_in_repo(repo, ["add", "-A"])
    git_in_repo(repo, ["commit", "-m", message])
    return git_in_repo(repo, ["rev-parse", "HEAD"]).strip()
