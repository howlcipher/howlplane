#!/usr/bin/env python3
import os

# Git exports repository-selection variables (GIT_DIR and friends) to every
# hook. They override the `cwd=` and `git -C` used by subprocess git calls, so
# a suite run from inside a hook would otherwise operate on THIS repository.
# The suite itself is already safe -- every git call goes through
# src/control_plane/git_env, which sanitizes the environment -- but the hook
# drops them too so that any tool the suite shells out to inherits a clean
# environment as well.
HOOK_ENV_SCRUB = (
    "unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE "
    "GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX "
    "GIT_SUPER_PREFIX GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM "
    "GIT_NAMESPACE GIT_QUARANTINE_PATH\n"
)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    hook_name = "pre-push"
    hooks_dir = os.path.join(repo_root, ".git", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, hook_name)
    hook_content = (
        "#!/usr/bin/env bash\n"
        "# Drop inherited Git repository-selection variables so nothing this\n"
        "# hook runs can be redirected away from its own working directory.\n"
        + HOOK_ENV_SCRUB
        + "echo 'Running pre-push regression test suite...'\n"
        "make test lint build docs\n"
        "if [ $? -ne 0 ]; then\n"
        "    echo 'Regression tests failed! Push aborted.'\n"
        "    exit 1\n"
        "fi\n"
    )
    with open(hook_path, "w") as f:
        f.write(hook_content)
    os.chmod(hook_path, 0o755)  # nosec B103
    print("Pre push hook installed successfully.")


if __name__ == "__main__":
    main()
