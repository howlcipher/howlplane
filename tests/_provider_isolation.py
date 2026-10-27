"""Keep the operator's real agent CLIs out of ordinary test runs.

The collection hook deselects `live` tests, but a test can still reach a real
provider without that marker: discovery uses ``shutil.which`` and launches go
through ``subprocess``, so an installed ``agy``, ``codex``, ``claude``,
``agent``, ``devin`` or ``ollama`` on the developer's PATH gets probed and
called. CI has none of them, so the same suite routed differently there and
locally, ran real provider sessions for hours, and failed tests that pass in CI.

`path_without` builds a PATH on which those executables do not resolve while
every other tool in the same directories still does.
"""

import os
from pathlib import Path


def provider_binaries() -> frozenset[str]:
    """Every agent CLI HowlPlane discovers on PATH, from its own registries."""
    from howlplane.control_plane import agent_readiness, orchestration

    names = {spec.binary for spec in agent_readiness.SPECS.values()}
    names.update(orchestration.BINARIES.values())
    names.add("ollama")
    return frozenset(names)


def _holds_any(directory: Path, names: frozenset[str]) -> bool:
    return any(os.access(directory / name, os.X_OK) and not (directory / name).is_dir() for name in names)


def path_without(names: frozenset[str], path: str, mirror_root: Path) -> str:
    """Return ``path`` with every executable in ``names`` unresolvable.

    A directory holding one of them is replaced by a mirror under
    ``mirror_root`` that symlinks everything else it contains. A directory that
    cannot be listed is dropped, since it cannot be mirrored safely.
    """
    entries: list[str] = []
    mirrors: dict[Path, str | None] = {}
    for entry in path.split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_dir() or not _holds_any(directory, names):
            entries.append(entry)
            continue
        key = directory.resolve()
        if key not in mirrors:
            mirror = mirror_root / str(len(mirrors))
            mirror.mkdir(parents=True)
            try:
                for child in directory.iterdir():
                    if child.name not in names:
                        (mirror / child.name).symlink_to(child)
                mirrors[key] = str(mirror)
            except OSError:
                mirrors[key] = None
        if mirrors[key] is not None:
            entries.append(mirrors[key])
    return os.pathsep.join(entries)
