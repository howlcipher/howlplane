"""Prepare committed tool copies independently of the checkout being edited."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import subprocess
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "sys.argv[0] = 'howlplane'; "
    "from howlplane.control_plane.cli import main; sys.exit(main())"
)


class SelfHostError(RuntimeError):
    """A pinned tool could not be prepared by Git."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ['git', '-C', str(repo), *args], capture_output=True,
            text=True, check=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise SelfHostError((exc.stderr or exc.stdout or str(exc)).strip()) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelfHostError(f'git: {exc}') from exc
    return result.stdout


def _tool_root(target_repo: Path, data_dir: Path) -> Path:
    key = hashlib.sha256(str(target_repo.resolve()).encode()).hexdigest()[:16]
    return Path(data_dir).resolve() / 'howlplane' / 'tool' / key


def _worktrees(repo: Path) -> set[Path]:
    return {
        Path(field.partition(' ')[2]).resolve()
        for field in _git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0')
        if field.startswith('worktree ')
    }


def should_isolate(command: str, target_repo: Path, package_dir: Path,
                   env: Mapping[str, str]) -> bool:
    """Only isolate commands running the exact target package checkout."""
    return (
        command in {'work', 'resume', 'orchestrate', 'verify'}
        and 'HOWLPLANE_PINNED_TOOL' not in env
        and Path(package_dir).resolve()
        == (Path(target_repo).resolve() / 'src' / 'howlplane').resolve()
    )


def ensure_pinned_tool(target_repo: Path, data_dir: Path) -> Path:
    """Return the source root of a detached worktree at the current HEAD."""
    repo = Path(target_repo).resolve()
    sha = _git(repo, 'rev-parse', '--verify', 'HEAD').strip()
    pin = _tool_root(repo, data_dir) / sha
    if pin.exists() and pin in _worktrees(repo):
        _git(pin, 'checkout', '--detach', '--force', sha)
        return pin / 'src'
    _git(repo, 'worktree', 'prune')
    pin.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, 'worktree', 'add', '--detach', str(pin), sha)
    return pin / 'src'


def prune_pinned_tools(target_repo: Path, data_dir: Path, keep: int = 3) -> None:
    """Remove oldest registered pins belonging to this repository only."""
    if keep < 0:
        raise ValueError('keep must be nonnegative')
    repo = Path(target_repo).resolve()
    root = _tool_root(repo, data_dir)
    pins = [
        path for path in _worktrees(repo)
        if path.is_dir() and path != root and path.is_relative_to(root)
    ]
    pins.sort(key=lambda path: (path.stat().st_mtime, str(path)))
    for pin in pins[:max(0, len(pins) - keep)]:
        try:
            _git(repo, 'worktree', 'remove', '--force', str(pin))
        except SelfHostError as exc:
            logger.debug('Could not remove pinned tool %s: %s', pin, exc)


def reexec_command(python: str, pinned_src: Path, argv: Sequence[str]) -> list[str]:
    """Build a bootstrap invocation without changing inherited import paths."""
    return [python, '-c', BOOTSTRAP, str(pinned_src), *argv]


def reexec_environment(env: Mapping[str, str], sha: str,
                       control_plane_root: Path) -> dict[str, str]:
    """Keep state in the original root and imports unchanged for children."""
    result = dict(env)
    result['HOWLPLANE_PINNED_TOOL'] = sha
    result.setdefault('HOWLPLANE_HOME', str(control_plane_root))
    return result


def uncommitted_tool_changes(target_repo: Path) -> list[str]:
    """List staged, unstaged, and untracked tool paths relative to the repo."""
    repo = Path(target_repo).resolve()
    staged = _git(repo, 'diff', '--cached', 'HEAD', '--name-only',
                  '--no-renames', '-z', '--', 'src/howlplane')
    changed = _git(repo, 'diff', 'HEAD', '--name-only',
                   '--no-renames', '-z', '--', 'src/howlplane')
    untracked = _git(repo, 'ls-files', '--others', '--exclude-standard', '-z',
                     '--', 'src/howlplane')
    return sorted(set(filter(None, (staged + changed + untracked).split('\0'))))
