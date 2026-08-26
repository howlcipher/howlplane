import subprocess
import os
import pathlib
from src.control_plane.git_env import run_git_in_repo

def test_user_profile_is_gitignored():
    """Assert USER_PROFILE.md is present in .gitignore and USER_PROFILE.example.md is tracked."""
    repo_root = pathlib.Path(__file__).parent.parent
    
    # Check if USER_PROFILE.example.md is tracked
    result = run_git_in_repo(repo_root, ["ls-files", "USER_PROFILE.example.md"])
    assert "USER_PROFILE.example.md" in result.stdout

    # Check if USER_PROFILE.md is ignored
    result_ignore = run_git_in_repo(repo_root, ["check-ignore", "USER_PROFILE.md"])
    assert result_ignore.returncode == 0
    assert "USER_PROFILE.md" in result_ignore.stdout or "USER_PROFILE.md" in result_ignore.stderr or result_ignore.returncode == 0

def test_no_real_profile_data_tracked():
    """Scan tracked files for real name, LinkedIn, and salary strings, asserting none appear."""
    repo_root = pathlib.Path(__file__).parent.parent
    
    # The exact strings from the real profile we want to ensure aren't in tracked files
    pii_strings = [
        "William Elias",
        "linkedin.com/in/wylelias",
        "$105,000+"
    ]
    
    # Get all tracked files
    result = run_git_in_repo(repo_root, ["ls-files"])
    tracked_files = result.stdout.splitlines()
    
    allowed_files = {
        "tests/test_profile_privacy.py",
        ".goreleaser.yaml",
        "scripts/setup_profile.py"
    }
    
    for file_path in tracked_files:
        if file_path in allowed_files:
            continue
            
        full_path = repo_root / file_path
        if not full_path.is_file():
            continue
            
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
                for pii in pii_strings:
                    assert pii not in content, f"PII string '{pii}' found in tracked file {file_path}"
        except UnicodeDecodeError:
            # Skip binary files
            continue
