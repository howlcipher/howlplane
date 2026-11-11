"""Real Git contracts for pinned tool isolation."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import time

import pytest

import howlplane
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


def commit_pin(repo, data, number):
    (repo / 'version').write_text(str(number))
    git(repo, 'add', '.')
    git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: version')
    return ensure_pinned_tool(repo, data).parent


def test_prune_removes_exactly_the_oldest_beyond_keep(repo, tmp_path):
    data = tmp_path / 'data'
    pins = [commit_pin(repo, data, number) for number in range(5)]
    # Modification order is the reverse of creation order, to show mtime decides.
    for number, pin in enumerate(reversed(pins)):
        os.utime(pin, (100 + number, 100 + number))
    prune_pinned_tools(repo, data, keep=2)
    assert [p.exists() for p in reversed(pins)] == [False, False, False, True, True]


def test_hand_deleted_registered_pin_does_not_raise(repo, tmp_path):
    data = tmp_path / 'data'
    old = commit_pin(repo, data, 0)
    current = commit_pin(repo, data, 1)
    shutil.rmtree(old)
    assert str(old) in git(repo, 'worktree', 'list', '--porcelain')
    prune_pinned_tools(repo, data, keep=0)
    assert not current.exists()
    git(repo, 'reset', '--hard', 'HEAD~1')
    src = ensure_pinned_tool(repo, data)
    assert src == old / 'src'
    assert (src / 'howlplane/tool.py').read_text() == 'original'
    shutil.rmtree(old)
    prune_pinned_tools(repo, data, keep=3)
    assert ensure_pinned_tool(repo, data) == src and src.is_dir()


def test_remove_failure_is_debug_logged_and_removal_continues(repo, tmp_path, caplog):
    data = tmp_path / 'data'
    pins = [commit_pin(repo, data, number) for number in range(3)]
    for number, pin in enumerate(pins):
        os.utime(pin, (100 + number, 100 + number))
    git(repo, 'worktree', 'lock', str(pins[0]))  # the oldest cannot be removed
    with caplog.at_level(logging.DEBUG):
        prune_pinned_tools(repo, data, keep=0)
    assert pins[0].exists()
    assert not pins[1].exists() and not pins[2].exists()
    failures = [r for r in caplog.records if str(pins[0]) in r.getMessage()]
    assert failures and all(r.levelno == logging.DEBUG for r in failures)


def test_reused_pin_is_refreshed_so_pruning_keeps_it(repo, tmp_path):
    data = tmp_path / 'data'
    pins = [commit_pin(repo, data, number) for number in range(4)]
    for number, pin in enumerate(pins):
        os.utime(pin, (100 + number, 100 + number))  # the current pin is the oldest
    git(repo, 'reset', '--hard', 'HEAD~3')
    assert ensure_pinned_tool(repo, data).parent == pins[0]
    prune_pinned_tools(repo, data, keep=3)
    assert [p.exists() for p in pins] == [True, False, True, True]


def test_prune_never_removes_current_pin_even_with_future_unused_pins(repo, tmp_path):
    data = tmp_path / 'data'
    pins = [commit_pin(repo, data, number) for number in range(4)]
    future = time.time() + 10 * 86400
    for number, pin in enumerate(pins[1:]):
        os.utime(pin, (future + number, future + number))
    git(repo, 'reset', '--hard', 'HEAD~3')
    assert ensure_pinned_tool(repo, data).parent == pins[0]
    prune_pinned_tools(repo, data, keep=3, current=pins[0])
    assert [p.exists() for p in pins] == [True, False, True, True]


# --- CLI wiring -------------------------------------------------------------

STUB_CLI = '''\
import json, os, sys


RAW_JSON = '{ "raw":   [1,\\n 2] }  '


def main():
    code = int(os.environ.get('STUB_EXIT', '7'))
    if '--json' in sys.argv:
        sys.stdout.write(RAW_JSON)  # deliberate whitespace, no trailing newline
        return code
    print(json.dumps({
        'file': __file__, 'argv': sys.argv[1:],
        'pinned': os.environ.get('HOWLPLANE_PINNED_TOOL'),
        'home': os.environ.get('HOWLPLANE_HOME'),
        'pythonpath': os.environ.get('PYTHONPATH'),
    }))
    return code
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    from howlplane.control_plane import cli
    path = tmp_path / 'stubrepo'
    package = path / 'src/howlplane'
    (package / 'control_plane').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'control_plane/__init__.py').write_text('')
    (package / 'control_plane/cli.py').write_text(STUB_CLI)
    git(path, 'init')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.test')
    git(path, 'add', '.')
    git(path, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: stub')
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'AGENTS.md').write_text('rules')
    data = tmp_path / 'data'
    monkeypatch.delenv('HOWLPLANE_PINNED_TOOL', raising=False)
    monkeypatch.delenv('HOWLPLANE_DIR', raising=False)
    monkeypatch.setenv('HOWLPLANE_HOME', str(home))
    monkeypatch.setenv('PYTHONPATH', 'original-path')
    real_data_dir = cli._data_dir
    monkeypatch.setattr(cli, '_package_dir', lambda: package)
    monkeypatch.setattr(cli, '_data_dir', lambda: data)
    parent_calls = []
    for name in ('work', 'resume', 'orchestrate', 'verify', 'status'):
        monkeypatch.setitem(
            cli.HANDLERS, name,
            lambda parsed, name=name: parent_calls.append(name) or 3,
        )
    return type('Stub', (), dict(
        cli=cli, repo=path, home=home, data=data, calls=parent_calls,
        real_data_dir=staticmethod(real_data_dir),
        sha=git(path, 'rev-parse', 'HEAD'),
    ))


def test_cli_reexecutes_from_pin_and_returns_child_exit_code(stub, capfd):
    argv = ['verify', '--repo', str(stub.repo), '--task-id', 'two words']
    assert stub.cli.main(argv) == 7
    out, err = capfd.readouterr()
    child = json.loads(out)
    assert out == json.dumps(child) + '\n'  # nothing added by the parent
    pin = stub.data / 'howlplane/tool'
    assert Path(child['file']).is_relative_to(pin.resolve())
    assert Path(child['file']).parts[-5] == stub.sha
    assert child['argv'] == argv
    assert child['pinned'] == stub.sha
    assert child['pythonpath'] == 'original-path'
    assert child['home'] == str(stub.home)
    assert stub.calls == []
    assert err.splitlines() == [f'HowlPlane is running from commit {stub.sha}.']


@pytest.mark.parametrize('code', [0, 1, 7, 42])
def test_cli_returns_the_childs_exit_code_unchanged(stub, monkeypatch, capfd, code):
    monkeypatch.setenv('STUB_EXIT', str(code))
    assert stub.cli.main(['verify', '--repo', str(stub.repo)]) == code
    assert json.loads(capfd.readouterr().out)['argv'][0] == 'verify'
    assert stub.calls == []


@pytest.mark.parametrize('argv', [
    ['work', 'an objective'],
    ['resume', 'T-1'],
    ['orchestrate', 'a goal'],
    ['verify', '--task-id', 'T-1'],
], ids=['work', 'resume', 'orchestrate', 'verify'])
def test_cli_isolates_every_required_command(stub, capfd, argv):
    argv = argv + ['--repo', str(stub.repo)]
    assert stub.cli.main(argv) == 7
    out, err = capfd.readouterr()
    child = json.loads(out)
    assert Path(child['file']).parts[-5] == stub.sha
    assert Path(child['file']).is_relative_to((stub.data / 'howlplane/tool').resolve())
    assert child['argv'] == argv
    assert child['pinned'] == stub.sha
    assert stub.calls == []
    assert err.splitlines() == [f'HowlPlane is running from commit {stub.sha}.']


def test_cli_passes_resolved_control_plane_root_to_child(stub, tmp_path, monkeypatch, capfd):
    root = tmp_path / 'cproot'
    root.mkdir()
    (root / 'AGENTS.md').write_text('rules')
    monkeypatch.delenv('HOWLPLANE_HOME')
    monkeypatch.delenv('AI_KNOWLEDGE_LIBRARY', raising=False)
    argv = ['verify', '--repo', str(stub.repo), '--control-plane-dir', str(root)]
    assert stub.cli.main(argv) == 7
    child = json.loads(capfd.readouterr().out)
    assert child['home'] == str(root.resolve())


def test_cli_json_success_output_is_the_childs_exact_bytes(stub, capfd):
    argv = ['resume', 'T-1', '--repo', str(stub.repo), '--json']
    assert stub.cli.main(argv) == 7
    out, _ = capfd.readouterr()
    assert out == '{ "raw":   [1,\n 2] }  '
    assert stub.calls == []


def test_cli_data_dir_default_uses_xdg_data_home(stub, tmp_path, monkeypatch, capfd):
    xdg = tmp_path / 'xdg'
    monkeypatch.setenv('XDG_DATA_HOME', str(xdg))
    monkeypatch.setattr(stub.cli, '_data_dir', stub.real_data_dir)
    assert stub.cli.main(['verify', '--repo', str(stub.repo)]) == 7
    assert Path(json.loads(capfd.readouterr().out)['file']).is_relative_to(
        (xdg / 'howlplane/tool').resolve())
    assert not stub.data.exists()


def test_cli_data_dir_default_falls_back_to_home_local_share(stub, tmp_path, monkeypatch, capfd):
    fake_home = tmp_path / 'userhome'
    fake_home.mkdir()
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.setattr(stub.cli, '_data_dir', stub.real_data_dir)
    assert stub.cli.main(['verify', '--repo', str(stub.repo)]) == 7
    assert Path(json.loads(capfd.readouterr().out)['file']).is_relative_to(
        (fake_home / '.local/share/howlplane/tool').resolve())
    assert not stub.data.exists()


@pytest.mark.parametrize('count', [1, 2, 3])
def test_cli_passes_original_flags_and_warns_with_the_actual_change_count(
        stub, capfd, count):
    package = stub.repo / 'src/howlplane'
    (package / '__init__.py').write_text('changed')
    for number in range(count - 1):
        (package / f'new{number}.py').write_text('x')
    argv = ['--debug', 'resume', 'T-1', '--repo', str(stub.repo)]
    assert stub.cli.main(argv) == 7
    out, err = capfd.readouterr()
    assert json.loads(out)['argv'] == argv
    assert err.splitlines() == [
        f'HowlPlane is running from commit {stub.sha}.',
        f'{count} file(s) under src/howlplane have uncommitted changes '
        'that the pinned copy will not use.',
    ]


def test_cli_conflicting_home_is_replaced_by_the_resolved_root(
        stub, tmp_path, capfd):
    root = tmp_path / 'cproot'
    root.mkdir()
    (root / 'AGENTS.md').write_text('rules')
    # HOWLPLANE_HOME still points at stub.home, which differs from the override.
    argv = ['verify', '--repo', str(stub.repo), '--control-plane-dir', str(root)]
    assert stub.cli.main(argv) == 7
    child = json.loads(capfd.readouterr().out)
    assert child['home'] == str(root.resolve())
    assert child['home'] != str(stub.home)


def test_cli_prune_keeps_current_pin_despite_future_unused_pins(stub, capfd):
    shas = [stub.sha]
    for number in range(3):
        (stub.repo / 'n').write_text(str(number))
        git(stub.repo, 'add', '.')
        git(stub.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: n')
        shas.append(git(stub.repo, 'rev-parse', 'HEAD'))
    tool = stub.data / 'howlplane/tool'
    for sha in shas:
        git(stub.repo, 'reset', '--hard', sha)
        ensure_pinned_tool(stub.repo, stub.data)
    pins = {sha: next(tool.glob(f'*/{sha}')) for sha in shas}
    future = time.time() + 10 * 86400
    for number, sha in enumerate(shas[1:]):
        os.utime(pins[sha], (future + number, future + number))
    git(stub.repo, 'reset', '--hard', shas[0])
    assert stub.cli.main(['work', 'an objective', '--repo', str(stub.repo)]) == 7
    child = json.loads(capfd.readouterr().out)
    assert Path(child['file']).parts[-5] == shas[0]
    assert pins[shas[0]].exists()
    assert not pins[shas[1]].exists()  # oldest unused pin
    assert pins[shas[2]].exists() and pins[shas[3]].exists()


def test_cli_prunes_to_three_pins(stub, capfd):
    for number in range(4):
        (stub.repo / 'n').write_text(str(number))
        git(stub.repo, 'add', '.')
        git(stub.repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: n')
        assert stub.cli.main(['work', 'an objective', '--repo', str(stub.repo)]) == 7
    pins = [p for p in (stub.data / 'howlplane/tool').glob('*/*') if p.is_dir()]
    assert len(pins) == 3


@pytest.mark.parametrize('case', ['other_command', 'other_repo', 'pinned_env', 'pinned_empty'])
def test_cli_does_not_reexecute(stub, tmp_path, monkeypatch, capfd, case):
    repo = stub.repo
    command = 'verify'
    if case == 'other_command':
        command = 'status'
    elif case == 'other_repo':
        repo = tmp_path / 'elsewhere'
        repo.mkdir()
        git(repo, 'init')
    else:
        monkeypatch.setenv('HOWLPLANE_PINNED_TOOL', '' if case == 'pinned_empty' else 'sha')
    assert stub.cli.main([command, '--repo', str(repo)]) == 3
    out, err = capfd.readouterr()
    assert stub.calls == [command]
    assert (out, err) == ('', '')
    assert not stub.data.exists()


@pytest.mark.parametrize('as_json', [False, True])
def test_cli_pin_failure_exits_1_without_running_anything(
        tmp_path, stub, monkeypatch, capfd, as_json):
    empty = tmp_path / 'emptyrepo'
    (empty / 'src/howlplane').mkdir(parents=True)
    git(empty, 'init')
    monkeypatch.setattr(stub.cli, '_package_dir', lambda: empty / 'src/howlplane')
    argv = ['resume', 'T-1', '--repo', str(empty)] + (['--json'] if as_json else [])
    assert stub.cli.main(argv) == 1
    out, err = capfd.readouterr()
    assert out == '' and stub.calls == []
    assert 'fatal:' in err and 'Could not prepare' in err
    if as_json:
        document = json.loads(err.strip().splitlines()[-1])
        assert document['schema'] == 'howlplane.error/v1'
        assert 'fatal:' in document['message']


# --- Real entry points ------------------------------------------------------

def test_package_dir_is_the_imported_howlplane_package():
    from howlplane.control_plane import cli
    imported = Path(howlplane.__file__).resolve().parent
    assert cli._package_dir() == imported
    assert cli._package_dir() == Path(cli.__file__).resolve().parents[1]
    assert (cli._package_dir() / 'control_plane/cli.py').is_file()


def copy_package(destination):
    """Copy the imported howlplane package (this checkout's src/howlplane)."""
    shutil.copytree(
        Path(howlplane.__file__).resolve().parent, destination,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
    )


ENTRY_SCRIPT = (
    'import os, sys\n'
    'src = sys.argv.pop(1)\n'
    'sys.path.insert(0, src)\n'
    'import howlplane\n'
    'if not os.path.realpath(howlplane.__file__).startswith(os.path.realpath(src) + os.sep):\n'
    '    sys.exit(99)\n'
    'from howlplane.control_plane import cli\n'
    'sys.exit(cli.main(sys.argv[1:]))\n'
)


def test_real_entry_runs_the_command_in_the_pinned_child(tmp_path):
    """No seam is replaced: package lookup, pinning and re-execution are all real."""
    repo = tmp_path / 'selfhost'
    package = repo / 'src/howlplane'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.test')
    copy_package(package)
    git(repo, 'add', '.')
    git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'feature: self host copy')
    sha = git(repo, 'rev-parse', 'HEAD')
    dirs = {
        name: tmp_path / name
        for name in ('home', 'userhome', 'data', 'state', 'diagnostics')
    }
    for path in dirs.values():
        path.mkdir()
    (dirs['home'] / 'AGENTS.md').write_text('rules')
    env = {
        key: value for key, value in os.environ.items()
        if key not in ('HOWLPLANE_PINNED_TOOL', 'HOWLPLANE_DEBUG', 'HOWLPLANE_DIR',
                       'AI_KNOWLEDGE_LIBRARY', 'PYTHONPATH')
    }
    env.update(
        # A temporary HOME keeps stray writes out of the real one; the user base
        # keeps packages installed with `pip install --user` importable.
        HOME=str(dirs['userhome']), PYTHONUSERBASE=site.getuserbase(), HOWLPLANE_HOME=str(dirs['home']),
        XDG_DATA_HOME=str(dirs['data']), XDG_STATE_HOME=str(dirs['state']),
        HOWLPLANE_DIAGNOSTICS_DIR=str(dirs['diagnostics']),
    )
    command = [
        sys.executable, '-c', ENTRY_SCRIPT, str(repo / 'src'),
        '--debug', 'resume', 'NO-SUCH-TASK', '--repo', str(repo),
    ]
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 99, 'the subprocess imported the wrong howlplane'
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    pin = dirs['data'].resolve() / 'howlplane/tool' / key / sha
    assert f'HowlPlane is running from commit {sha}.' in result.stderr.splitlines()
    assert pin.is_dir() and (pin / 'src/howlplane/control_plane/cli.py').is_file()
    assert 'TaskRunNotFoundError' in result.stderr
    named = [Path(name).resolve() for name in re.findall(r'File "([^"]+)"', result.stderr)]
    assert [p for p in named if p.is_relative_to(pin / 'src')], result.stderr
    assert not [p for p in named if p.is_relative_to((repo / 'src').resolve())], result.stderr
    assert result.returncode == 1
    diagnostics = list(dirs['diagnostics'].glob('HP-*.txt'))
    assert len(diagnostics) == 1
    text = diagnostics[0].read_text()
    assert 'TaskRunNotFoundError' in text and 'NO-SUCH-TASK' in text


def test_main_without_arguments_reads_sys_argv(stub, monkeypatch, capfd):
    argv = ['howlplane', '--debug', 'resume', 'two words', '--repo', str(stub.repo)]
    monkeypatch.setattr(stub.cli, 'cmd_home', lambda parsed: stub.calls.append('home') or 3)
    monkeypatch.setenv('STUB_EXIT', '5')
    monkeypatch.setattr(sys, 'argv', argv)
    assert stub.cli.main() == 5
    out, err = capfd.readouterr()
    child = json.loads(out)
    assert child['argv'] == argv[1:]
    assert child['pinned'] == stub.sha
    assert stub.calls == []
    assert err.splitlines() == [f'HowlPlane is running from commit {stub.sha}.']


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
