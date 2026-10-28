"""A private HowlPlane install for tests that run CLI commands.

A command given neither `--ledger-file` nor `-C` finds its control plane through
HOWLPLANE_HOME, then ~/.config/howlplane/config.toml, then this checkout, and
keeps its evidence ledger under that root. On a developer machine the config
file names the operator's own HowlPlane, so such a test recorded fake decisions
in the operator's ledger. The root built here links this checkout's tracked
resources and gives the command a private `logs/`, so it behaves as installed
while its state stays in the test's directory.
"""

from functools import lru_cache
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
# Top-level entries the product writes state into, never linked.
_STATE = {"logs"}


@lru_cache(maxsize=1)
def _tracked_top_level():
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return sorted({path.split("/", 1)[0] for path in listed.split("\0") if path} - _STATE)


def private_control_plane(monkeypatch, tmp_path: Path) -> Path:
    """Points HOWLPLANE_HOME at a fresh install under `tmp_path` and returns its root."""
    root = tmp_path / "howlplane_home"
    (root / "logs").mkdir(parents=True)
    for name in _tracked_top_level():
        (root / name).symlink_to(REPO / name)
    monkeypatch.setenv("HOWLPLANE_HOME", str(root))
    return root
