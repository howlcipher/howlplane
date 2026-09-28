"""Safe, local campaign resolution for the Factory convenience commands.

This module deliberately owns only launcher plumbing.  The supervisor remains
the single durable execution engine; a campaign merely gives it stable paths
and an isolated Git checkout.
"""


from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import List, Optional, Union
import uuid

from howlplane.control_plane.atomic_io import atomic_write_json, safe_load_json
from howlplane.control_plane.launcher import TargetRepositoryNotFoundError, find_git_repo_root
from howlplane.control_plane.locking import get_systemd_main_pid, is_process_record_active


class CampaignError(ValueError):
    """Raised when a campaign cannot be safely resolved."""


def _canonical_user_home() -> Path:
    """Return the canonical home directory root for this user.

    Atomic/Bazzite-style systems expose the same home directory through both
    ``/home/<user>`` and ``/var/home/<user>``.  ``Path.resolve()`` does not
    collapse the two because neither is a symlink of the other.  A shell
    session may set ``HOME=/home/<user>`` while a systemd user service sees
    ``/var/home/<user>``.  This function picks one stable spelling so every
    Factory path derived from ``Path.home()`` is identical in both contexts.
    """
    home = Path.home().resolve()
    user = home.name
    var_home = Path(f"/var/home/{user}")
    classic_home = Path(f"/home/{user}")
    # Prefer the classic /home spelling when the two resolve to the same
    # physical directory, which is the common atomic-desktop arrangement.
    try:
        if var_home.exists() and classic_home.exists() and var_home.samefile(classic_home):
            return classic_home
    except OSError:
        pass
    return home


_CANONICAL_HOME: Optional[Path] = None


def canonical_path(path: Union[str, Path]) -> Path:
    """Return a stable, absolute, symlink-resolved path.

    Resolves symlinks and normalizes the /home/<user> vs /var/home/<user>
    alias pair so the same physical location is always represented by the
    same string.  All Factory metadata paths are canonicalized before they
    are stored or compared.
    """
    global _CANONICAL_HOME
    if _CANONICAL_HOME is None:
        _CANONICAL_HOME = _canonical_user_home()

    p = Path(path).expanduser().resolve()

    # Fast path: already canonical.
    try:
        p.relative_to(_CANONICAL_HOME)
        return p
    except ValueError:
        pass

    # If the resolved path lives under an aliased home spelling, rewrite it
    # to the canonical home spelling.
    user = _CANONICAL_HOME.name
    for alias in (Path(f"/home/{user}"), Path(f"/var/home/{user}")):
        if alias == _CANONICAL_HOME or not alias.exists():
            continue
        try:
            rel = p.relative_to(alias)
            return _CANONICAL_HOME / rel
        except ValueError:
            continue
    return p


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    path = Path(value).expanduser() if value else fallback
    return canonical_path(path)


def factory_data_home() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "howlplane"


def factory_state_home() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state") / "howlplane" / "factory"


def factory_workspace_root(repository: "RepositoryIdentity") -> Path:
    """The one stable directory under which every Factory worktree of a repository lives.

    Keeping the canonical target and every bounded canary below it lets an
    operator authorize (and CLIs inherit trust for) the repository's Factory
    workspaces once, instead of meeting a never-seen path on every run.
    """
    return factory_data_home() / "worktrees" / repository.campaign_id


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
    root = canonical_path(root)
    remote = _canonical_remote(_optional_git(root, ["remote", "get-url", "origin"]))
    default_branch = _optional_git(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if default_branch.startswith("origin/"):
        default_branch = default_branch.split("/", 1)[1]
    if not default_branch:
        default_branch = _optional_git(root, ["branch", "--show-current"]) or "main"
    commit = _run_git(root, ["rev-parse", "HEAD"])
    dirty = bool(_optional_git(root, ["status", "--porcelain", "--untracked-files=normal"]))
    common = canonical_path(Path(_run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])))
    # A canonical local root is included even with a remote: independent clones
    # must never share mutable Factory state by accident.
    raw_identity = json.dumps({"root": str(root), "remote": remote}, sort_keys=True)
    campaign_id = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:24]
    return RepositoryIdentity(root, remote, default_branch, commit, dirty, common, campaign_id)


def _metadata_for_state_dir(state_dir: Path) -> Optional[dict]:
    path = state_dir / "campaign.json"
    if not path.is_file():
        return None
    try:
        return safe_load_json(path)
    except Exception:
        return None


def generate_bounded_campaign_id(repository: RepositoryIdentity) -> str:
    repo_name = re.sub(r"[^A-Za-z0-9-]+", "-", repository.root.name.lower()).strip("-") or "repo"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    unique_suffix = uuid.uuid4().hex[:6]
    return f"{repo_name}-canary-{ts}-{unique_suffix}"


def _expected_unit_name_for_state_dir(state_dir: Path, campaign_id: Optional[str] = None) -> str:
    state_dir_name = state_dir.name
    if campaign_id and state_dir_name == campaign_id:
        return f"howlplane-factory-{campaign_id}"
    safe = re.sub(r"[^A-Za-z0-9-]+", "-", state_dir_name).strip("-")
    safe = safe[:200] or "factory"
    return f"howlplane-factory-{safe}"


def resolve_campaign(
    start_dir: Optional[Union[str, Path]] = None,
    *,
    state_dir: Optional[Union[str, Path]] = None,
    target_repo: Optional[Union[str, Path]] = None,
    prefer_active: bool = False,
    bounded: bool = False,
) -> FactoryCampaign:
    repository = discover_repository(start_dir)
    if bounded and not state_dir:
        canary_id = generate_bounded_campaign_id(repository)
        raw_state = factory_state_home() / canary_id
        raw_target = (
            canonical_path(target_repo)
            if target_repo
            else factory_workspace_root(repository) / "canaries" / canary_id / "target"
        )
    else:
        raw_state = canonical_path(state_dir) if state_dir else factory_state_home() / repository.campaign_id
        if target_repo:
            raw_target = canonical_path(target_repo)
        else:
            raw_target = factory_workspace_root(repository) / "target"
            metadata = _metadata_for_state_dir(raw_state)
            if metadata and metadata.get("target_repo"):
                raw_target = canonical_path(metadata["target_repo"])
    state_root = factory_state_home().absolute() if not state_dir else raw_state.absolute().parent
    managed_target_root = (factory_data_home() / "worktrees").absolute()
    target_root = (
        managed_target_root
        if raw_target.absolute().is_relative_to(managed_target_root)
        else raw_target.absolute().parent
    )
    _refuse_symlink(raw_state.absolute(), state_root)
    _refuse_symlink(raw_target.absolute(), target_root)
    resolved_state = canonical_path(raw_state)
    resolved_target = canonical_path(raw_target)
    if not prefer_active:
        return FactoryCampaign(repository, resolved_state, resolved_target)
    candidates = _list_campaign_state_dirs(repository)
    if not candidates:
        return FactoryCampaign(repository, resolved_state, resolved_target)
    active = [c for c in candidates if _is_process_active_for_state_dir(c)]
    if len(active) > 1:
        names = ", ".join(str(c.name) for c in active)
        raise CampaignError(
            f"Multiple active Factory campaigns for this repository: {names}. "
            "Use --state-dir to select one explicitly."
        )
    # Prefer a single active campaign; otherwise reuse a single stopped one.
    chosen = active[0] if active else (candidates[0] if len(candidates) == 1 else None)
    if chosen is not None:
        metadata = _metadata_for_state_dir(chosen)
        target = _target_dir_from_metadata(metadata, resolved_target)
        return FactoryCampaign(repository, canonical_path(chosen), target)
    # Multiple stopped historical campaigns exist; fall back to the canonical/default state dir.
    return FactoryCampaign(repository, resolved_state, resolved_target)


def _matches_repository(metadata: dict, repository: RepositoryIdentity) -> bool:
    recorded_root = canonical_path(metadata.get("repository_root", "")) if metadata.get("repository_root") else None
    return (
        metadata.get("schema") == "howlplane.factory.campaign/v1"
        and metadata.get("campaign_id") == repository.campaign_id
        and recorded_root == repository.root
        and metadata.get("remote") == repository.remote
    )


def _is_process_active_for_state_dir(state_dir: Path) -> bool:
    record_path = state_dir / "campaign" / "process.json"
    if not record_path.is_file():
        return False
    try:
        data = safe_load_json(record_path)
    except Exception:
        return False

    if data.get("status") == "stopped":
        return False

    cmd = data.get("command") or []
    if "--state-dir" in cmd:
        try:
            idx = cmd.index("--state-dir")
            if idx + 1 < len(cmd):
                cmd_state = canonical_path(cmd[idx + 1])
                if cmd_state != canonical_path(state_dir):
                    return False
        except (ValueError, IndexError):
            pass

    unit_name = data.get("unit_name")
    if unit_name:
        meta = _metadata_for_state_dir(state_dir)
        campaign_id = meta.get("campaign_id") if meta else None
        expected_unit = _expected_unit_name_for_state_dir(state_dir, campaign_id)
        if unit_name != expected_unit:
            return False

    if not is_process_record_active(data):
        return False

    pid = data.get("pid", 0)
    if data.get("backend") == "systemd" and unit_name:
        main_pid = get_systemd_main_pid(unit_name)
        if main_pid > 0:
            pid = main_pid

    if pid > 0:
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.is_file():
            try:
                content = proc_cmdline.read_bytes().decode("utf-8", errors="ignore")
                if str(canonical_path(state_dir)) not in content:
                    return False
            except OSError:
                pass

    return True


def _list_campaign_state_dirs(repository: RepositoryIdentity) -> List[Path]:
    base = factory_state_home()
    if not base.is_dir():
        return []
    candidates: List[Path] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        metadata = _metadata_for_state_dir(entry)
        if metadata and _matches_repository(metadata, repository):
            candidates.append(entry)
    return sorted(candidates)


def campaign_from_state_dir(state_dir: Union[str, Path]) -> Optional[FactoryCampaign]:
    """Build a campaign directly from its durable metadata, without Git discovery.

    Useful when the caller has supplied an explicit --state-dir and may not be
    inside the source repository.
    """
    path = canonical_path(state_dir)
    metadata = _metadata_for_state_dir(path)
    if not metadata:
        return None
    # Reuse the canonical resolution path with the recorded source root. The
    # metadata validation inside resolve_campaign confirms the state directory
    # still belongs to the repository it claims.
    return resolve_campaign(
        start_dir=metadata["repository_root"],
        state_dir=path,
    )


def _refuse_symlink(path: Path, owned_root: Path) -> None:
    """Reject symlinks at or below the Factory-owned root."""
    path = path.absolute()
    owned_root = owned_root.absolute()
    try:
        path.relative_to(owned_root)
    except ValueError as exc:
        raise CampaignError(f"Factory-managed path escapes its owned root: {path}") from exc
    current = path
    while current != owned_root.parent:
        if current.exists() and current.is_symlink():
            raise CampaignError(f"Factory-managed path contains a symlink and is refused: {current}")
        current = current.parent


def _target_dir_from_metadata(metadata: Optional[dict], fallback: Path) -> Path:
    if metadata and metadata.get("target_repo"):
        return canonical_path(metadata["target_repo"])
    return fallback


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
    for key in ("schema", "campaign_id", "remote"):
        if data.get(key) != expected[key]:
            raise CampaignError("Campaign metadata does not belong to this repository; refusing cross-project reuse")
    for key in ("repository_root", "source_common_git_dir", "target_repo"):
        value = data.get(key)
        if not value or canonical_path(value) != canonical_path(expected[key]):
            raise CampaignError("Campaign metadata does not belong to this repository; refusing cross-project reuse")


def _validate_target(campaign: FactoryCampaign) -> bool:
    target = campaign.target_dir
    if not target.exists():
        return False
    if target.is_symlink():
        raise CampaignError(f"Factory target must not be a symlink: {target}")
    try:
        common = canonical_path(Path(_run_git(target, ["rev-parse", "--path-format=absolute", "--git-common-dir"])))
    except CampaignError as exc:
        raise CampaignError(f"Factory target exists but is not a healthy Git worktree: {target}") from exc
    if common != campaign.repository.common_git_dir:
        raise CampaignError("Factory target belongs to a different repository; refusing to reuse it")
    if canonical_path(target) == campaign.repository.root:
        raise CampaignError("Factory target collides with the user checkout; refusing to mutate it")
    return True


def prepare_campaign(campaign: FactoryCampaign) -> FactoryCampaign:
    """Persist metadata and create or validate the one managed worktree."""
    _refuse_symlink(campaign.state_dir, campaign.state_dir.parent)
    _refuse_symlink(campaign.target_dir, campaign.target_dir.parent)
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
