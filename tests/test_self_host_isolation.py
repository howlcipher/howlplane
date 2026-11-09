"""Real Git contracts for pinned tool isolation."""
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from howlplane.control_plane.self_host import (
    BOOTSTRAP, SelfHostError, ensure_pinned_tool, prune_pinned_tools, reexec_command,
    reexec_environment, should_isolate, uncommitted_tool_changes,
)

pytestmark = pytest.mark.integration


def git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.test')
    (path / 'src/howlplane').mkdir(parents=True)
    (path / 'src/howlplane/tool.py').write_text('original')
    git(path, 'add', '.')
    git(path, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: initial')
    return path


@pytest.mark.parametrize('command', ['work', 'resume', 'orchestrate', 'verify'])
def test_isolation_exact_package_and_environment(repo, tmp_path, command):
    package = repo / 'src/howlplane'
    assert should_isolate(command, repo, package / '..' / 'howlplane', {})
    for wrong in [repo, package / 'control_plane', repo / 'other',
                  repo / 'howlplane', tmp_path / 'other/src/howlplane']:
        assert not should_isolate(command, repo, wrong, {})
    for value in ['', 'sha']:
        assert not should_isolate(command, repo, package, {'HOWLPLANE_PINNED_TOOL': value})
    assert not should_isolate('status', repo, package, {})


def test_pin_identity_reuse_branch_and_deleted_recovery(repo, tmp_path):
    data = tmp_path / 'data'
    sha = git(repo, 'rev-parse', 'HEAD')
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    src = ensure_pinned_tool(repo, data)
    assert src == data / 'howlplane/tool' / key / sha / 'src'
    pin = src.parent
    assert git(pin, 'rev-parse', 'HEAD') == sha
    assert git(pin, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    cache = src / 'howlplane/__pycache__/tool.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'cached tool')
    assert ensure_pinned_tool(repo, data) == src
    assert cache.read_bytes() == b'cached tool'
    git(pin, 'checkout', '-b', 'pin-branch')
    assert ensure_pinned_tool(repo, data) == src
    assert git(pin, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    shutil.rmtree(pin)
    assert ensure_pinned_tool(repo, data) == src
    assert (src / 'howlplane/tool.py').read_text() == 'original'


def test_symlinked_paths_share_isolation_and_pin(repo, tmp_path):
    linked_repo = tmp_path / 'linked_repo'
    linked_repo.symlink_to(repo, target_is_directory=True)
    linked_package = tmp_path / 'linked_package'
    linked_package.symlink_to(repo / 'src/howlplane', target_is_directory=True)
    assert should_isolate('work', linked_repo, repo / 'src/howlplane', {})
    assert should_isolate('work', repo, linked_package, {})
    data = tmp_path / 'data'
    data.mkdir()
    linked_data = tmp_path / 'linked_data'
    linked_data.symlink_to(data, target_is_directory=True)
    src = ensure_pinned_tool(linked_repo, linked_data)
    assert src == ensure_pinned_tool(repo, data)
    git(src.parent, 'checkout', '-b', 'linked_pin')
    assert ensure_pinned_tool(linked_repo, linked_data) == src
    assert git(src.parent, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    prune_pinned_tools(linked_repo, linked_data, keep=0)
    assert not src.parent.exists()


def test_creation_error_names_git_failure(repo, tmp_path):
    data = tmp_path / 'data'
    src = ensure_pinned_tool(repo, data)
    git(repo, 'worktree', 'remove', '--force', str(src.parent))
    src.parent.mkdir()
    (src.parent / 'occupied').write_text('occupied')
    with pytest.raises(SelfHostError) as error:
        ensure_pinned_tool(repo, data)
    assert 'already exists' in str(error.value)
    assert str(src.parent) in str(error.value)


def test_pruning_counts_only_registered_own_pins(repo, tmp_path, caplog):
    data = tmp_path / 'data'
    pins = []
    for number in range(4):
        (repo / 'version').write_text(str(number))
        git(repo, 'add', '.')
        git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: version')
        pin = ensure_pinned_tool(repo, data).parent
        os.utime(pin, (100 + number, 100 + number))
        pins.append(pin)
        if number == 2:
            unrelated = pin.parent / 'unrelated'
            unrelated.mkdir()
            prune_pinned_tools(repo, data)
            assert all(p.exists() for p in pins)
    other = tmp_path / 'other'
    other.mkdir()
    git(other, 'init')
    git(other, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'feature: initial')
    foreign = pins[0].parent / 'foreign'
    git(other, 'worktree', 'add', '--detach', str(foreign))
    outside = tmp_path / 'outside'
    git(repo, 'worktree', 'add', '--detach', str(outside))
    pins.sort(reverse=True)
    for number, pin in enumerate(pins):
        os.utime(pin, (100 + number, 100 + number))
    prune_pinned_tools(repo, data, keep=2)
    assert all(not p.exists() for p in pins[:2])
    assert all(p.exists() for p in pins[2:])
    assert foreign.exists() and unrelated.exists() and outside.exists()
    # Locked registered pins cause removal to fail without raising.
    git(repo, 'worktree', 'lock', str(pins[2]))
    with caplog.at_level(logging.DEBUG):
        prune_pinned_tools(repo, data, keep=0)
    assert pins[2].exists()
    assert not pins[3].exists()
    assert any(
        r.levelno == logging.DEBUG and str(pins[2]) in r.getMessage()
        for r in caplog.records
    )
    assert all(r.levelno <= logging.DEBUG for r in caplog.records)


def test_reexec_environment_preserves_imports_and_home():
    env = {'PYTHONPATH': 'original', 'OTHER': 'value'}
    result = reexec_environment(env, 'sha', Path('/original/root'))
    assert result == {**env, 'HOWLPLANE_PINNED_TOOL': 'sha', 'HOWLPLANE_HOME': '/original/root'}
    assert env == {'PYTHONPATH': 'original', 'OTHER': 'value'}
    assert reexec_environment({'HOWLPLANE_HOME': ''}, 'sha', '/root')['HOWLPLANE_HOME'] == ''
    assert 'PYTHONPATH' not in reexec_environment({}, 'sha', '/root')
    assert reexec_environment({}, 'sha', Path('/root'))['HOWLPLANE_HOME'] == '/root'
    assert reexec_environment(
        {'HOWLPLANE_HOME': '/custom/home'}, 'sha', Path('/root')
    )['HOWLPLANE_HOME'] == '/custom/home'


def test_bootstrap_imports_pin_and_passes_arguments(tmp_path):
    package = tmp_path / 'pin with spaces/howlplane/control_plane'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text(
        'import json, sys\ndef main():\n'
        ' print(json.dumps([__file__, sys.argv]))\n return 7\n'
    )
    src = package.parent.parent
    command = reexec_command(sys.executable, src, ['verify', 'two words'])
    assert command[2] == BOOTSTRAP
    assert command[:2] == [sys.executable, '-c']
    assert command[3:] == [str(src), 'verify', 'two words']
    decoy = tmp_path / 'decoy/howlplane/control_plane'
    decoy.mkdir(parents=True)
    (decoy.parent / '__init__.py').write_text('')
    (decoy / '__init__.py').write_text('')
    (decoy / 'cli.py').write_text('def main():\n print("decoy")\n return 0\n')
    result = subprocess.run(
        command, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(decoy.parent.parent)},
    )
    assert result.returncode == 7
    assert json.loads(result.stdout) == [str(package / 'cli.py'), ['howlplane', 'verify', 'two words']]


def test_uncommitted_paths_cover_staged_unstaged_untracked(repo):
    package = repo / 'src/howlplane'
    names = ['staged.py', 'unstaged.py', 'deleted.py', 'renamed.py', 'internal.py']
    for name in names:
        (package / name).write_text(name)
    (repo / '.gitignore').write_text('*.ignored\n__pycache__/\n')
    (repo / 'outside').write_text('original')
    git(repo, 'add', '.')
    git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: fixtures')
    (package / 'temp.ignored').write_text('ignored')
    cache = package / '__pycache__/tool.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'ignored cache')
    assert uncommitted_tool_changes(repo) == []
    (package / 'staged.py').write_text('staged')
    git(repo, 'add', '.')
    (package / 'unstaged.py').write_text('unstaged')
    (package / 'tool.py').write_text('staged tool')
    git(repo, 'add', 'src/howlplane/tool.py')
    (package / 'tool.py').write_text('original')
    for name in ['addition.py', 'unlinked.py']:
        (package / name).write_text('new')
        git(repo, 'add', str(package / name))
    (package / 'unlinked.py').unlink()
    (package / 'new file.py').write_text('untracked')
    (repo / 'outside').write_text('changed')
    git(repo, 'rm', 'src/howlplane/deleted.py')
    git(repo, 'mv', 'src/howlplane/renamed.py', 'moved_outside.py')
    git(repo, 'mv', 'src/howlplane/internal.py', 'src/howlplane/destination.py')
    assert uncommitted_tool_changes(repo) == [
        'src/howlplane/addition.py',
        'src/howlplane/deleted.py',
        'src/howlplane/destination.py',
        'src/howlplane/internal.py',
        'src/howlplane/new file.py',
        'src/howlplane/renamed.py',
        'src/howlplane/staged.py',
        'src/howlplane/tool.py',
        'src/howlplane/unlinked.py',
        'src/howlplane/unstaged.py',
    ]


def test_reuse_restores_moved_head(repo, tmp_path):
    data = tmp_path / 'data'
    src = ensure_pinned_tool(repo, data)
    sha = git(repo, 'rev-parse', 'HEAD')
    (src / 'howlplane/tool.py').write_text('other commit')
    git(src.parent, 'add', '.')
    git(src.parent, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: moved')
    assert ensure_pinned_tool(repo, data) == src
    assert git(src.parent, 'rev-parse', 'HEAD') == sha
    assert git(src.parent, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    assert (src / 'howlplane/tool.py').read_text() == 'original'


@pytest.mark.parametrize('empty_repo', [False, True])
def test_invalid_head_fails_closed(tmp_path, empty_repo):
    if empty_repo:
        git(tmp_path, 'init')
    with pytest.raises(SelfHostError) as error:
        ensure_pinned_tool(tmp_path, tmp_path / 'data')
    assert 'fatal:' in str(error.value)
    with pytest.raises(SelfHostError):
        uncommitted_tool_changes(tmp_path)
    if not empty_repo:
        with pytest.raises(SelfHostError):
            prune_pinned_tools(tmp_path, tmp_path / 'data')



def test_missing_git_is_wrapped(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(SelfHostError, match='git:') as error:
        ensure_pinned_tool(tmp_path, tmp_path / 'data')
    assert isinstance(error.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize('damage', ['unstaged', 'staged', 'deleted_src'])
def test_reuse_restores_uncommitted_damage(repo, tmp_path, damage):
    data = tmp_path / 'data'
    src = ensure_pinned_tool(repo, data)
    if damage == 'deleted_src':
        shutil.rmtree(src)
    else:
        (src / 'howlplane/tool.py').write_text('dirty')
        if damage == 'staged':
            git(src.parent, 'add', '.')
    assert ensure_pinned_tool(repo, data) == src
    assert (src / 'howlplane/tool.py').read_text() == 'original'
    assert git(src.parent, 'status', '--porcelain') == ''
    assert (repo / 'src/howlplane/tool.py').read_text() == 'original'


def test_prune_negative_keep_preserves_pins(repo, tmp_path):
    data = tmp_path / 'data'
    src = ensure_pinned_tool(repo, data)
    with pytest.raises(ValueError, match='keep must be nonnegative'):
        prune_pinned_tools(repo, data, keep=-1)
    assert (src / 'howlplane/tool.py').read_text() == 'original'


def test_prune_preserves_registered_cache_root(repo, tmp_path):
    data = tmp_path / 'data'
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    root = data / 'howlplane/tool' / key
    git(repo, 'worktree', 'add', '--detach', str(root))
    src = ensure_pinned_tool(repo, data)
    prune_pinned_tools(repo, data, keep=0)
    assert (root / 'src/howlplane/tool.py').read_text() == 'original'
    assert not src.parent.exists()
    assert str(root) in git(repo, 'worktree', 'list', '--porcelain')
