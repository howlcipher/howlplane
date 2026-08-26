"""tests/test_git_env_isolation.py

Proves that inherited Git repository-selection variables cannot redirect a test
helper's git commands into another repository.

On 2026-08-26 a `pre-push` hook ran this suite with GIT_DIR exported. Helpers
that built throwaway repositories with `cwd=` and no sanitized `env=` operated
on the real repository instead: `core.bare` flipped to true, a committer
identity was injected, an unintended commit landed on the live feature branch,
and 111 tests failed.

Every test here points a selection variable at a *decoy* repository -- never
the real one -- so that a regression fails an assertion instead of damaging the
working tree. The decoy stands in for the repository a hook would have
exported.

`monkeypatch.setenv` deliberately overrides conftest's scrubbing fixture, so
what these tests prove is the helper's own safety, not conftest's.
"""

import hashlib
import os
import subprocess  # nosec B404 - deterministic pytest re-invocation
import sys
from pathlib import Path

import pytest

from src.control_plane.git_env import (
    GIT_REPOSITORY_SELECTION_ENV_VARS,
    run_git_in_repo,
    sanitized_git_env,
)
from tests._git_test_helpers import commit_all, git_in_repo, init_git_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _decoy_repo(path: Path) -> Path:
    """Builds the repository that an inherited variable would redirect into."""
    return init_git_repo(path, files={"decoy.txt": "decoy content\n"})


def _fingerprint(repo: Path) -> dict:
    """Captures every piece of repository state contamination would disturb."""
    config_bytes = (repo / ".git" / "config").read_bytes()
    index_path = repo / ".git" / "index"
    return {
        "head": git_in_repo(repo, ["rev-parse", "HEAD"]).strip(),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "core_bare": git_in_repo(repo, ["config", "--get", "core.bare"]).strip(),
        "user_name": git_in_repo(repo, ["config", "--get", "user.name"]).strip(),
        "user_email": git_in_repo(repo, ["config", "--get", "user.email"]).strip(),
        "status": git_in_repo(repo, ["status", "--porcelain"]),
        "branch": git_in_repo(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
        "object_count": len(list((repo / ".git" / "objects").rglob("*"))),
        "index_sha256": hashlib.sha256(
            index_path.read_bytes() if index_path.is_file() else b""
        ).hexdigest(),
    }


def _inherited_env(variables, decoy: Path) -> dict:
    """Maps selection-variable names onto values pointing at the decoy repo."""
    values = {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_COMMON_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy / ".git" / "objects"),
        "GIT_PREFIX": "decoy/",
        "GIT_SUPER_PREFIX": "decoy/",
        "GIT_CEILING_DIRECTORIES": str(decoy.parent),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "GIT_NAMESPACE": "decoy",
        "GIT_QUARANTINE_PATH": str(decoy / ".git" / "quarantine"),
    }
    return {name: values[name] for name in variables}


# The variable combinations a hook realistically exports. GIT_DIR alone was the
# observed production failure; the rest are the other documented selection
# variables, individually and combined.
INHERITED_ENV_SHAPES = [
    ("GIT_DIR",),
    ("GIT_WORK_TREE",),
    ("GIT_DIR", "GIT_WORK_TREE"),
    ("GIT_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"),
    ("GIT_COMMON_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES"),
    ("GIT_CEILING_DIRECTORIES", "GIT_PREFIX", "GIT_SUPER_PREFIX"),
    ("GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_DISCOVERY_ACROSS_FILESYSTEM"),
    GIT_REPOSITORY_SELECTION_ENV_VARS,
]


@pytest.mark.parametrize("variables", INHERITED_ENV_SHAPES, ids=lambda v: "+".join(v))
def test_helper_ignores_inherited_repository_selection(tmp_path, monkeypatch, variables):
    """The canonical helper builds and commits only in its own directory."""
    decoy = _decoy_repo(tmp_path / "decoy")
    before = _fingerprint(decoy)

    for name, value in _inherited_env(variables, decoy).items():
        monkeypatch.setenv(name, value)

    workspace = tmp_path / "workspace"
    repo = init_git_repo(workspace, files={"work.txt": "work content\n"})
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    second_sha = commit_all(repo, "second commit")

    # The temporary repository really did advance.
    assert git_in_repo(repo, ["rev-parse", "HEAD"]).strip() == second_sha
    tracked = git_in_repo(repo, ["ls-files"]).split()
    assert sorted(tracked) == ["second.txt", "work.txt"]

    # And the decoy is byte-for-byte what it was.
    assert _fingerprint(decoy) == before
    assert "decoy" not in tracked
    assert not (decoy / "work.txt").exists()


@pytest.mark.parametrize("variables", INHERITED_ENV_SHAPES, ids=lambda v: "+".join(v))
def test_run_git_in_repo_reads_only_its_own_repository(tmp_path, monkeypatch, variables):
    """Read commands resolve against the named directory, not the decoy."""
    decoy = _decoy_repo(tmp_path / "decoy")
    workspace = init_git_repo(tmp_path / "workspace", files={"work.txt": "work\n"})

    for name, value in _inherited_env(variables, decoy).items():
        monkeypatch.setenv(name, value)

    listed = run_git_in_repo(workspace, ["ls-files"]).stdout.split()
    assert listed == ["work.txt"]
    toplevel = run_git_in_repo(workspace, ["rev-parse", "--show-toplevel"]).stdout.strip()
    assert Path(toplevel).resolve() == workspace.resolve()


def test_sanitized_git_env_drops_selection_and_keeps_credentials(monkeypatch):
    """Only repository selection is scrubbed; identity and transport survive."""
    for name in GIT_REPOSITORY_SELECTION_ENV_VARS:
        monkeypatch.setenv(name, "/should/be/dropped")
    preserved = {
        "GIT_AUTHOR_NAME": "Author",
        "GIT_COMMITTER_EMAIL": "committer@example.com",
        "GIT_SSH_COMMAND": "ssh -i /key",
        "GIT_ASKPASS": "/usr/bin/askpass",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/etc/gitconfig",
        "PATH": os.environ.get("PATH", ""),
    }
    for name, value in preserved.items():
        monkeypatch.setenv(name, value)

    env = sanitized_git_env()

    for name in GIT_REPOSITORY_SELECTION_ENV_VARS:
        assert name not in env, f"{name} was not scrubbed"
    for name, value in preserved.items():
        assert env[name] == value, f"{name} was wrongly scrubbed"


def test_sanitized_git_env_does_not_mutate_the_process_environment(monkeypatch):
    """Sanitizing returns a copy; the caller's own environment is untouched."""
    monkeypatch.setenv("GIT_DIR", "/decoy/.git")
    sanitized_git_env()
    assert os.environ["GIT_DIR"] == "/decoy/.git"


def test_suite_passes_under_a_hook_shaped_environment(tmp_path):
    """Reproduces the pre-push shape: pytest re-invoked with GIT_DIR exported.

    This is the end-to-end guarantee -- the repository is safe with no patched
    hook and no conftest scrubbing, because the child process inherits GIT_DIR
    through `env=` exactly as a Git hook would export it.
    """
    decoy = _decoy_repo(tmp_path / "decoy")
    before = _fingerprint(decoy)

    env = dict(os.environ)
    env["GIT_DIR"] = str(decoy / ".git")
    env["GIT_WORK_TREE"] = str(decoy)

    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", "tests/test_git_baseline.py", "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )

    assert result.returncode == 0, (
        f"Suite failed under an inherited Git environment.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert _fingerprint(decoy) == before


def test_real_repository_was_not_contaminated_by_the_suite():
    """Guard: the checkout running these tests must never be written to.

    `core.bare` flipping to true and a `user.email` appearing in the local
    config were the two visible symptoms of the production incident.
    """
    core_bare = run_git_in_repo(REPO_ROOT, ["config", "--local", "--get", "core.bare"])
    assert core_bare.stdout.strip() in ("", "false"), "core.bare was modified"

    for key in ("user.email", "user.name"):
        injected = run_git_in_repo(REPO_ROOT, ["config", "--local", "--get", key])
        assert injected.stdout.strip() != "HowlPlane CI", f"local {key} was injected"
        assert injected.stdout.strip() != "ci@howlplane.local", f"local {key} was injected"
