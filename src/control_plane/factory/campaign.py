"""Safe, local campaign resolution for the Factory convenience commands.

This module deliberately owns only launcher plumbing.  The supervisor remains
the single durable execution engine; a campaign merely gives it stable paths
and an isolated Git checkout.
"""


from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Optional, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.launcher import TargetRepositoryNotFoundError, find_git_repo_root


class CampaignError(ValueError):
    """Raised when a campaign cannot be safely resolved."""


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


def factory_data_home() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "howlplane"


def factory_state_home() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state") / "howlplane" / "factory"


def _run_git(repo: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignError(f"Git is unavailable while resolving this project: {exc}") from exc
    if result.returncode:
        raise CampaignError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _optional_git(repo: Path, args: list[str]) -> str:
    try:
        return _run_git(repo, args)
    except CampaignError:
        return ""


def _canonical_remote(remote: str) -> str:
    remote = remote.strip()
    if remote.endswith(".git"):
        remote = remote[:-4]
    if remote.startswith("git@") and ":" in remote:
        host_path = remote[4:].replace(":", "/", 1)
        return host_path.lower()
    return remote.rstrip("/").lower()


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    remote: str
    default_branch: str
    commit: str
    dirty: bool
    common_git_dir: Path
    campaign_id: str


@dataclass(frozen=True)
class FactoryCampaign:
    repository: RepositoryIdentity
    state_dir: Path
    target_dir: Path

    @property
    def metadata_path(self) -> Path:
        return self.state_dir / "campaign.json"


def discover_repository(start_dir: Optional[Union[str, Path]] = None) -> RepositoryIdentity:
    try:
        root = find_git_repo_root(start_dir)
    except TargetRepositoryNotFoundError as exc:
        raise CampaignError(str(exc)) from exc
    root = root.resolve()
    remote = _canonical_remote(_optional_git(root, ["remote", "get-url", "origin"]))
    default_branch = _optional_git(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if default_branch.startswith("origin/"):
        default_branch = default_branch.split("/", 1)[1]
    if not default_branch:
        default_branch = _optional_git(root, ["branch", "--show-current"]) or "main"
    commit = _run_git(root, ["rev-parse", "HEAD"])
    dirty = bool(_optional_git(root, ["status", "--porcelain", "--untracked-files=normal"]))
    common = Path(_run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])).resolve()
    # A canonical local root is included even with a remote: independent clones
    # must never share mutable Factory state by accident.
    raw_identity = json.dumps({"root": str(root), "remote": remote}, sort_keys=True)
    campaign_id = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:24]
    return RepositoryIdentity(root, remote, default_branch, commit, dirty, common, campaign_id)


def resolve_campaign(
    start_dir: Optional[Union[str, Path]] = None,
    *,
    state_dir: Optional[Union[str, Path]] = None,
    target_repo: Optional[Union[str, Path]] = None,
) -> FactoryCampaign:
    repository = discover_repository(start_dir)
    raw_state = Path(state_dir).expanduser() if state_dir else factory_state_home() / repository.campaign_id
    raw_target = Path(target_repo).expanduser() if target_repo else factory_data_home() / "worktrees" / repository.campaign_id / "target"
    _refuse_symlink(raw_state.absolute())
    _refuse_symlink(raw_target.absolute())
    resolved_state = raw_state.resolve()
    resolved_target = raw_target.resolve()
    return FactoryCampaign(repository, resolved_state, resolved_target)


def _refuse_symlink(path: Path) -> None:
    """Reject a managed path containing a symlink after its owned root."""
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise CampaignError(f"Factory-managed path contains a symlink and is refused: {current}")
        current = current.parent


def _metadata(campaign: FactoryCampaign) -> dict:
    repo = campaign.repository
    return {
        "schema": "howlplane.factory.campaign/v1",
        "campaign_id": repo.campaign_id,
        "repository_root": str(repo.root),
        "remote": repo.remote,
        "default_branch": repo.default_branch,
        "source_common_git_dir": str(repo.common_git_dir),
        "target_repo": str(campaign.target_dir),
    }


def _validate_metadata(campaign: FactoryCampaign) -> None:
    if not campaign.metadata_path.exists():
        return
    try:
        data = safe_load_json(campaign.metadata_path)
    except Exception as exc:
        raise CampaignError(f"Campaign metadata is unreadable: {exc}") from exc
    expected = _metadata(campaign)
    for key in ("schema", "campaign_id", "repository_root", "remote", "source_common_git_dir", "target_repo"):
        if data.get(key) != expected[key]:
            raise CampaignError("Campaign metadata does not belong to this repository; refusing cross-project reuse")


def _validate_target(campaign: FactoryCampaign) -> bool:
    target = campaign.target_dir
    if not target.exists():
        return False
    if target.is_symlink():
        raise CampaignError(f"Factory target must not be a symlink: {target}")
    try:
        common = Path(_run_git(target, ["rev-parse", "--path-format=absolute", "--git-common-dir"])).resolve()
    except CampaignError as exc:
        raise CampaignError(f"Factory target exists but is not a healthy Git worktree: {target}") from exc
    if common != campaign.repository.common_git_dir:
        raise CampaignError("Factory target belongs to a different repository; refusing to reuse it")
    if target.resolve() == campaign.repository.root:
        raise CampaignError("Factory target collides with the user checkout; refusing to mutate it")
    return True


def prepare_campaign(campaign: FactoryCampaign) -> FactoryCampaign:
    """Persist metadata and create or validate the one managed worktree."""
    _refuse_symlink(campaign.state_dir)
    _refuse_symlink(campaign.target_dir)
    campaign.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_metadata(campaign)
    if not _validate_target(campaign):
        campaign.target_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            _run_git(
                campaign.repository.root,
                ["worktree", "add", "--detach", str(campaign.target_dir), campaign.repository.commit],
            )
        except CampaignError as exc:
            raise CampaignError(f"Could not prepare isolated Factory worktree: {exc}") from exc
        _validate_target(campaign)
    atomic_write_json(campaign.metadata_path, _metadata(campaign))
    return campaign
