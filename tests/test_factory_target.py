"""
test_factory_target.py

Unit tests for the generalized Factory target/workspace abstraction.
"""

import pytest
import yaml

from howlplane.control_plane.factory.target import (
    FactoryTarget,
    FactoryTargetMode,
    Workspace,
)
from tests._factory_test_helpers import make_git_repo, set_xdg_paths


def test_factory_target_repo_mode_does_not_require_isolation(tmp_path):
    target = FactoryTarget(
        mode=FactoryTargetMode.REPO,
        target_repo=tmp_path,
        controller_checkout=tmp_path,
    )
    target.ensure_isolated_self_target()  # no-op for repo mode


def test_factory_target_self_rejects_same_checkout(tmp_path):
    target = FactoryTarget(
        mode=FactoryTargetMode.SELF,
        target_repo=tmp_path,
        controller_checkout=tmp_path,
    )
    with pytest.raises(ValueError) as exc:
        target.ensure_isolated_self_target()
    assert "refuses to run against the controller checkout" in str(exc.value)


def test_factory_target_self_accepts_separate_candidate(tmp_path):
    controller = tmp_path / "controller"
    candidate = tmp_path / "candidate"
    controller.mkdir()
    candidate.mkdir()
    target = FactoryTarget(
        mode=FactoryTargetMode.SELF,
        target_repo=candidate,
        controller_checkout=controller,
    )
    target.ensure_isolated_self_target()


def test_workspace_from_yaml(tmp_path):
    ws_path = tmp_path / "workspace.yaml"
    ws_path.write_text(
        yaml.safe_dump(
            {
                "repositories": [
                    {"path": str(tmp_path / "a"), "repository": "owner/a", "priority_hint": 2.0},
                    {"path": str(tmp_path / "b"), "roles": ["library"]},
                ],
                "dependencies": ["owner/a"],
                "objective": "Improve reliability",
            }
        ),
        encoding="utf-8",
    )
    ws = Workspace.from_file(ws_path)
    assert len(ws.repositories) == 2
    assert ws.repositories[0].repository == "owner/a"
    assert ws.repositories[0].priority_hint == 2.0
    assert ws.repositories[1].roles == ["library"]
    assert ws.dependencies == ["owner/a"]
    assert ws.objective == "Improve reliability"


def test_workspace_string_repositories_are_resolved(tmp_path):
    ws = Workspace.from_dict({"repositories": [str(tmp_path)]})
    assert ws.repositories[0].path == tmp_path.resolve()


def test_workspace_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Workspace.from_file(tmp_path / "missing.yaml")


def test_factory_target_ecosystem_loads_workspace(tmp_path):
    ws_path = tmp_path / "ws.yaml"
    ws_path.write_text(
        yaml.safe_dump({"repositories": [str(tmp_path)], "objective": "x"}),
        encoding="utf-8",
    )
    target = FactoryTarget(
        mode=FactoryTargetMode.ECOSYSTEM,
        target_repo=tmp_path,
        workspace=Workspace.from_file(ws_path),
    )
    assert target.workspace is not None
    assert target.workspace.objective == "x"


def test_campaign_resolution_is_stable_from_subdirectory_and_uses_xdg(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import prepare_campaign, resolve_campaign

    repo = make_git_repo(tmp_path)
    nested = repo / "nested" / "directory"
    nested.mkdir(parents=True)
    set_xdg_paths(monkeypatch, tmp_path)
    first = resolve_campaign(repo)
    second = resolve_campaign(nested)
    assert first.repository.campaign_id == second.repository.campaign_id
    assert first.state_dir == tmp_path / "state" / "howlplane" / "factory" / first.repository.campaign_id
    prepare_campaign(first)
    assert first.target_dir != repo
    assert (first.target_dir / "README.md").read_text(encoding="utf-8") == "initial\n"
    assert prepare_campaign(second).target_dir == first.target_dir


def test_campaign_worktree_leaves_dirty_user_checkout_untouched(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import prepare_campaign, resolve_campaign

    repo = make_git_repo(tmp_path)
    (repo / "README.md").write_text("user change\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    set_xdg_paths(monkeypatch, tmp_path)
    campaign = prepare_campaign(resolve_campaign(repo))
    assert campaign.repository.dirty is True
    assert (repo / "README.md").read_text(encoding="utf-8") == "user change\n"
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (campaign.target_dir / "README.md").read_text(encoding="utf-8") == "initial\n"


def test_campaign_identity_distinguishes_same_basename_repositories(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import resolve_campaign

    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first = make_git_repo(first_root, name="same")
    second = make_git_repo(second_root, name="same")
    set_xdg_paths(monkeypatch, tmp_path)
    assert resolve_campaign(first).repository.campaign_id != resolve_campaign(second).repository.campaign_id
