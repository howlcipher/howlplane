"""Keep the operator's real agent CLIs out of ordinary test runs.

The collection hook deselects `live` tests, but a test can still reach a real
provider without that marker: discovery uses ``shutil.which`` and launches go
through ``subprocess``, so an installed ``agy``, ``codex``, ``claude``,
``agent``, ``devin``, ``gemini`` or ``ollama`` on the developer's PATH gets
probed and called. CI has none of them, so the same suite routed differently
there and locally, ran real provider sessions for hours, and failed tests that
pass in CI.

`path_without` builds a PATH on which those executables do not resolve while
every other tool in the same directories still does. Mirrored tools are
symlinks, so a tool that finds its own files through an unresolved
``dirname "$0"`` looks in the mirror; it would fail the same way under any
symlinked install.
"""

import os
import re
import shlex
from pathlib import Path

_INTERPRETER = re.compile(r"python(\d+(\.\d+)?)?")


def provider_binaries() -> frozenset[str]:
    """Every agent CLI HowlPlane discovers or launches, from its own registries."""
    from howlplane.control_plane import agent_readiness, orchestration
    from howlplane.control_plane.agent_execution import AgentBackendRegistry, SubprocessAgentBackend

    names = {spec.binary for spec in agent_readiness.SPECS.values()}
    names.update(orchestration.BINARIES.values())
    names.update(backend.binary_name for backend in AgentBackendRegistry._instances.values()
                 if isinstance(backend, SubprocessAgentBackend))
    names.add("ollama")
    return frozenset(names)


def _is_hidden(path: Path, names: frozenset[str]) -> bool:
    return path.name in names and os.access(path, os.X_OK) and not path.is_dir()


def _mirror(directory: Path, names: frozenset[str], mirror: Path) -> None:
    # A virtualenv's interpreter finds pyvenv.cfg next to the path it was run
    # by, so in a venv bin directory it is exec'd by its own path, not linked.
    venv = (directory.parent / "pyvenv.cfg").is_file()
    mirror.mkdir(parents=True)
    for child in directory.iterdir():
        if _is_hidden(child, names):
            continue
        if venv and _INTERPRETER.fullmatch(child.name):
            wrapper = mirror / child.name
            wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(str(child))} \"$@\"\n", encoding="utf-8")
            wrapper.chmod(0o755)
        else:
            (mirror / child.name).symlink_to(child)


def path_without(names: frozenset[str], path: str, mirror_root: Path) -> str:
    """Return ``path`` with every executable in ``names`` unresolvable.

    A directory holding one of them is replaced by a mirror under
    ``mirror_root`` of everything else it contains. Empty and relative entries
    are dropped: they name whatever directory a test happens to run in, which a
    mirror cannot stand for, and CI's PATH has none. A directory that cannot be
    listed is dropped too, since it cannot be mirrored safely.
    """
    entries: list[str] = []
    mirrors: dict[Path, str | None] = {}
    for entry in path.split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        directory = Path(entry)
        if not directory.is_dir() or not any(_is_hidden(directory / name, names) for name in names):
            entries.append(entry)
            continue
        key = directory.resolve()
        if key not in mirrors:
            mirror = mirror_root / str(len(mirrors))
            try:
                _mirror(directory, names, mirror)
                mirrors[key] = str(mirror)
            except OSError:
                mirrors[key] = None
        if mirrors[key] is not None:
            entries.append(mirrors[key])
    return os.pathsep.join(entries)
