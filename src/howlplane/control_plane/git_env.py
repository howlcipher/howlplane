"""git_env.py

Canonical sanitized-environment launcher for Git subprocesses.

Git exports repository-selection variables to every hook it runs, and those
variables take precedence over both the ``cwd=`` passed to ``subprocess.run``
and the ``git -C <dir>`` argument. A process that inherits them therefore
operates on the *exporting* repository no matter which directory it names.

This was observed in production on 2026-08-26: a ``pre-push`` hook ran the test
suite, whose helpers build throwaway repositories with ``git init`` / ``git
config`` / ``git commit``. Every one of those calls landed on the real
repository instead, flipping ``core.bare`` to true, injecting a committer
identity, and committing onto the live feature branch.

Empirically verified against git 2.53.0 by replaying that helper sequence
(``init`` / ``config`` / ``add`` / ``commit``) in a fresh directory while a
single variable pointed at a decoy repository:

===================================  ==========================================
Variable                             Observed effect on the decoy
===================================  ==========================================
GIT_DIR                              config rewritten, HEAD moved, objects added
GIT_COMMON_DIR                       config rewritten, objects added
GIT_OBJECT_DIRECTORY                 objects added
GIT_WORK_TREE                        working tree read from the decoy
GIT_INDEX_FILE                       decoy index staged against
GIT_ALTERNATE_OBJECT_DIRECTORIES     decoy objects resolvable
GIT_PREFIX / GIT_SUPER_PREFIX        path interpretation (documented)
GIT_CEILING_DIRECTORIES              repository discovery (documented)
GIT_DISCOVERY_ACROSS_FILESYSTEM      repository discovery (documented)
GIT_NAMESPACE / GIT_QUARANTINE_PATH  ref/object visibility (documented)
===================================  ==========================================

Only repository selection is scrubbed. Credential, transport, and identity
variables (``GIT_SSH*``, ``GIT_ASKPASS``, ``GIT_AUTHOR_*``, ``GIT_COMMITTER_*``,
``GIT_TERMINAL_PROMPT``, ``GIT_CONFIG_*``) are deliberately preserved so that
sanitizing does not break authenticated remotes or an operator's configured
identity.
"""

import os
import subprocess  # nosec B404 - deterministic git invocation without a shell
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

# Variables that select which repository, work tree, index, or object store a
# git invocation acts on. Each one can override `cwd=` and `git -C`, so all of
# them are removed before launching git against a specific directory.
GIT_REPOSITORY_SELECTION_ENV_VARS: Tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_SUPER_PREFIX",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH",
)


def sanitized_git_env(
    base: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Returns an environment copy with repository-selection variables removed.

    Args:
        base: Environment to sanitize. Defaults to the current process env.
        overrides: Variables applied after sanitizing. An override may
            deliberately reinstate a selection variable; callers that do so are
            opting into the redirection.
    """
    env = dict(os.environ if base is None else base)
    for name in GIT_REPOSITORY_SELECTION_ENV_VARS:
        env.pop(name, None)
    if overrides:
        env.update(overrides)
    return env


def run_git_in_repo(
    repo_root: Union[str, Path],
    args: List[str],
    timeout: int = 60,
    env_overrides: Optional[Mapping[str, str]] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Runs ``git -C <repo_root> <args>`` under a sanitized environment.

    This is the only supported way to invoke git against a specific directory.
    Passing `cwd=` or `git -C` alone is not sufficient, because an inherited
    `GIT_DIR` overrides both.
    """
    return subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "-C", str(repo_root)] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=sanitized_git_env(overrides=env_overrides),
    )
