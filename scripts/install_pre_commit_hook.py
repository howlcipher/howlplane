#!/usr/bin/env python3
import os
import subprocess


def main():
    # `git rev-parse --git-path` is correct in worktrees and from any cwd.
    hook_dir = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--git-path", "hooks"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    os.makedirs(hook_dir, exist_ok=True)
    hook_path = os.path.join(hook_dir, "pre-commit")

    content = """#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel)" || exit 1
if git diff --cached --name-only | grep -vE '\\.env\\.(template|example)$' | grep -qE '(^|/)\\.env(\\.|$)'; then
    echo "ERROR: Attempting to commit a .env file. Commit aborted."
    exit 1
fi

# Regenerate the skills manifest (AGENTS.md), index (.agents/skills.json),
# and the .claude/skills/ symlink set when any skill or command-skill
# definition is part of the commit.
if git diff --cached --name-only | grep -qE "^\\.agents/(skills|skill_commands)/"; then
    echo "Skill files changed; regenerating skills manifest and index..."
    PYTHON_BIN="python3"
    if [ -n "$VIRTUAL_ENV" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
        PYTHON_BIN="$VIRTUAL_ENV/bin/python3"
    fi
    if "$PYTHON_BIN" scripts/generate_skills_manifest.py; then
        git add AGENTS.md .agents/skills.json .claude/skills
    else
        echo "ERROR: skills manifest regeneration failed. Commit aborted."
        exit 1
    fi
fi
"""
    with open(hook_path, "w") as f:
        f.write(content)
    os.chmod(hook_path, 0o755)  # nosec B103
    print("Pre-commit hook installed successfully.")


if __name__ == "__main__":
    main()
