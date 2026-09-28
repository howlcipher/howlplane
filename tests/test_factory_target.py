"""
test_factory_target.py

Unit tests for the generalized Factory target/workspace abstraction.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from howlplane.control_plane.factory.target import (
    FactoryTarget,
    FactoryTargetMode,
    Workspace,
)
from tests._factory_test_helpers import make_git_repo, set_xdg_paths


def _set_symlinked_xdg_home(tmp_path, monkeypatch):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(linked_home / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(linked_home / "data"))
    return real_home, linked_home


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


def test_campaign_allows_symlink_above_factory_owned_root(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import resolve_campaign

    repo = make_git_repo(tmp_path)
    real_home, _ = _set_symlinked_xdg_home(tmp_path, monkeypatch)

    campaign = resolve_campaign(repo)

    assert campaign.state_dir.is_relative_to(real_home)
    assert campaign.target_dir.is_relative_to(real_home)


def test_campaign_metadata_accepts_equivalent_symlinked_path_spelling(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import prepare_campaign, resolve_campaign

    repo = make_git_repo(tmp_path)
    real_home, linked_home = _set_symlinked_xdg_home(tmp_path, monkeypatch)
    campaign = resolve_campaign(repo)
    campaign.state_dir.mkdir(parents=True)
    linked_target = linked_home / campaign.target_dir.relative_to(real_home)
    campaign.metadata_path.write_text(
        json.dumps({
            "schema": "howlplane.factory.campaign/v1",
            "campaign_id": campaign.repository.campaign_id,
            "repository_root": str(campaign.repository.root),
            "remote": campaign.repository.remote,
            "source_common_git_dir": str(campaign.repository.common_git_dir),
            "target_repo": str(linked_target),
        }),
        encoding="utf-8",
    )

    prepared = prepare_campaign(campaign)

    assert prepared.target_dir.is_dir()


def test_campaign_refuses_symlink_below_factory_owned_root(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.campaign import CampaignError, discover_repository, resolve_campaign

    repo = make_git_repo(tmp_path)
    set_xdg_paths(monkeypatch, tmp_path)
    identity = discover_repository(repo)
    workspace_root = tmp_path / "data" / "howlplane" / "worktrees"
    workspace_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / identity.campaign_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CampaignError, match="contains a symlink"):
        resolve_campaign(repo)


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


def _workspace_repositories(tmp_path, names):
    repositories = []
    for name in names:
        repo = make_git_repo(tmp_path, name=name)
        slug = f"howlcipher/{name}"
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{slug}.git"],
            cwd=repo,
            check=True,
        )
        repositories.append({"path": str(repo), "repository": slug})
    return repositories


def test_workspace_resolver_prepares_distinct_repository_targets_and_metadata(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.target import Workspace, WorkspaceRepositoryResolver

    set_xdg_paths(monkeypatch, tmp_path)
    repositories = _workspace_repositories(
        tmp_path,
        ["howl", "howlplane", "howlframe", "grocery-optimizer"],
    )
    resolver = WorkspaceRepositoryResolver(
        Workspace.from_dict({"repositories": repositories}),
        "master-campaign",
        tmp_path / "master-state",
    )

    prepared = resolver.prepare_all()

    assert len({target.slug for target in prepared}) == 4
    assert len({target.target_dir for target in prepared}) == 4
    assert len({target.identity.common_git_dir for target in prepared}) == 4
    for target in prepared:
        metadata = json.loads(target.metadata_path.read_text(encoding="utf-8"))
        assert metadata["master_campaign_id"] == "master-campaign"
        assert metadata["repository"] == target.slug
        assert metadata["source_common_git_dir"] == str(target.identity.common_git_dir)
        assert target.target_dir != target.identity.root


def _prepare_twice(workspace, master_state):
    from howlplane.control_plane.factory.target import WorkspaceRepositoryResolver

    first = WorkspaceRepositoryResolver(workspace, "master-campaign", master_state)
    first.prepare_all()
    second = WorkspaceRepositoryResolver(workspace, "master-campaign", master_state)
    second.prepare_all()
    first_targets = {target.slug: target.target_dir for target in first.repositories}
    second_targets = {target.slug: target.target_dir for target in second.repositories}
    assert first_targets == second_targets
    return first_targets


def test_workspace_resolver_restart_reconstructs_same_targets(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.target import Workspace

    set_xdg_paths(monkeypatch, tmp_path)
    workspace = Workspace.from_dict({"repositories": _workspace_repositories(tmp_path, ["howl", "howlplane"])})
    targets = _prepare_twice(workspace, tmp_path / "master-state")
    assert len(targets) == 2


def test_workspace_resolver_rejects_unknown_and_mismatched_git_identity(tmp_path, monkeypatch):
    from howlplane.control_plane.factory.target import (
        Workspace,
        WorkspaceRepositoryResolver,
        WorkspaceResolutionError,
    )

    set_xdg_paths(monkeypatch, tmp_path)
    repositories = _workspace_repositories(tmp_path, ["howl"])
    resolver = WorkspaceRepositoryResolver(
        Workspace.from_dict({"repositories": repositories}),
        "master-campaign",
        tmp_path / "master-state",
    )
    with pytest.raises(WorkspaceResolutionError, match="Unknown workspace repository"):
        resolver.resolve("howlcipher/not-in-workspace")

    repositories[0]["repository"] = "howlcipher/howlframe"
    with pytest.raises(WorkspaceResolutionError, match="identity mismatch"):
        WorkspaceRepositoryResolver(
            Workspace.from_dict({"repositories": repositories}),
            "master-campaign",
            tmp_path / "other-state",
        )


def test_canonical_path_resolves_symlinks(tmp_path):
    from howlplane.control_plane.factory.campaign import canonical_path

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    assert canonical_path(link_dir) == real_dir.resolve()


def test_canonical_path_normalizes_home_var_home_alias_when_present():
    """On Bazzite/atomic hosts /home/<user> and /var/home/<user> alias the same directory.

    This test is a no-op elsewhere, but it provides regression coverage for the
    most recent live Factory failure: cross-project metadata reuse caused by
    path spelling differences between shell and systemd child processes.
    """
    from howlplane.control_plane.factory.campaign import canonical_path

    classic = Path("/home/howlcipher")
    var_home = Path("/var/home/howlcipher")
    if not classic.exists() or not var_home.exists():
        pytest.skip("no /home and /var/home aliases on this system")
    try:
        if not classic.samefile(var_home):
            pytest.skip("/home and /var/home are not the same directory")
    except OSError:
        pytest.skip("cannot compare /home and /var/home")

    sample = classic / ".local" / "share" / "howlplane"
    assert canonical_path(var_home / ".local" / "share" / "howlplane") == sample
    assert canonical_path(classic / ".local" / "share" / "howlplane") == sample


def test_workspace_resolver_restart_accepts_equivalent_path_spelling(tmp_path, monkeypatch):
    """Restart must not treat a path alias as a different repository's metadata."""
    from howlplane.control_plane.factory.target import Workspace

    real_home, linked_home = _set_symlinked_xdg_home(tmp_path, monkeypatch)
    # Simulate one process spelling the data home via the real path and another
    # via the symlinked path; canonicalization must make them identical.
    monkeypatch.setenv("XDG_DATA_HOME", str(real_home / "data"))

    workspace = Workspace.from_dict({"repositories": _workspace_repositories(tmp_path, ["howl"])})
    targets = _prepare_twice(workspace, tmp_path / "master-state")
    assert len(targets) == 1
