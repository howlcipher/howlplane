"""An idle managed Factory worktree follows the repository; a busy one is left alone."""

import subprocess

from howlplane.control_plane.factory.campaign import prepare_campaign, resolve_campaign
from tests._factory_test_helpers import make_git_repo, set_xdg_paths


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text("x\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)


def _campaign(repo):
    return prepare_campaign(resolve_campaign(repo))


def test_idle_worktree_advances_to_new_backlog_commits(tmp_path, monkeypatch):
    set_xdg_paths(monkeypatch, tmp_path)
    repo = make_git_repo(tmp_path)
    campaign = _campaign(repo)
    _commit(repo, "issues.md")
    campaign = _campaign(repo)
    assert (campaign.target_dir / "issues.md").exists()
    assert _git(campaign.target_dir, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")


def test_dirty_worktree_is_not_moved(tmp_path, monkeypatch):
    set_xdg_paths(monkeypatch, tmp_path)
    repo = make_git_repo(tmp_path)
    campaign = _campaign(repo)
    old = _git(campaign.target_dir, "rev-parse", "HEAD")
    (campaign.target_dir / "wip.txt").write_text("in flight\n", encoding="utf-8")
    _commit(repo, "issues.md")
    campaign = _campaign(repo)
    assert _git(campaign.target_dir, "rev-parse", "HEAD") == old
    assert (campaign.target_dir / "wip.txt").exists()


def test_worktree_on_a_branch_is_not_moved(tmp_path, monkeypatch):
    set_xdg_paths(monkeypatch, tmp_path)
    repo = make_git_repo(tmp_path)
    campaign = _campaign(repo)
    _git(campaign.target_dir, "checkout", "-q", "-b", "factory/work")
    old = _git(campaign.target_dir, "rev-parse", "HEAD")
    _commit(repo, "issues.md")
    campaign = _campaign(repo)
    assert _git(campaign.target_dir, "rev-parse", "HEAD") == old
