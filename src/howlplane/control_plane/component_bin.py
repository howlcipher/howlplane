"""Find and describe the other Howl components HowlPlane runs.

Every component is run as its own executable with an argument list. This module
is the one place HowlPlane locates them, in the order the Howl interop contract
documents (https://github.com/howlcipher/howl/blob/main/docs/INTEROP.md):

1. ``HOWL<NAME>_BIN`` (and any legacy variable a component still honours); an
   explicit override that is not executable is not silently replaced;
2. ``PATH`` (the caller's ``env["PATH"]`` when one is given);
3. the Howl-managed release under ``$XDG_DATA_HOME/howl/components/<name>/``;
4. ``~/.local/bin/<name>``.

``describe`` reads a component's ``interop --json`` descriptor
(``howl.interop/v1``), so callers can check a command or flag exists, and read
the producer's version, before relying on it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv lists, no shell
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

INTEROP_SCHEMA = "howl.interop/v1"
RESULT_SCHEMA = "howl.result/v1"


def env_var(name: str) -> str:
    """The override variable for a component, e.g. ``HOWLCHANGEOPS_BIN``."""
    return name.upper().replace("-", "_") + "_BIN"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def managed_release(name: str, env: Mapping[str, str]) -> Optional[Path]:
    """The executable of the Howl-managed release of ``name``, if one is installed."""
    home = env.get("HOME") or str(Path.home())
    data_home = Path(env.get("XDG_DATA_HOME") or Path(home) / ".local" / "share")
    component = data_home / "howl" / "components" / name
    current = component / "current"
    if current.is_dir():
        release = current
    elif current.is_file():
        version = current.read_text(encoding="utf-8").strip()
        if not version:
            return None
        release = component / "releases" / version
    else:
        return None
    candidate = release / name
    return candidate if _executable(candidate) else None


def resolve(
    name: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    legacy_env: Iterable[str] = (),
    aliases: Iterable[str] = (),
) -> Optional[str]:
    """Return the path of ``name``'s executable, or None when it cannot be found."""
    env = os.environ if env is None else env
    for var in (env_var(name), *legacy_env):
        value = (env.get(var) or "").strip()
        if value:
            path = Path(value).expanduser()
            return str(path.resolve()) if _executable(path) else None
    for candidate in (name, *aliases):
        found = shutil.which(candidate, path=env.get("PATH"))
        if found:
            return found
    managed = managed_release(name, env)
    if managed is not None:
        return str(managed)
    home = env.get("HOME") or str(Path.home())
    local = Path(home) / ".local" / "bin" / name
    return str(local) if _executable(local) else None


_DESCRIPTORS: Dict[Tuple[str, int], Optional[Dict[str, Any]]] = {}


def describe(executable: str, *, timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    """The component's ``howl.interop/v1`` descriptor, or None for a release without one.

    Cached per executable path and modification time, so an update is noticed.
    """
    try:
        key = (executable, Path(executable).stat().st_mtime_ns)
    except OSError:
        return None
    if key in _DESCRIPTORS:
        return _DESCRIPTORS[key]
    descriptor: Optional[Dict[str, Any]] = None
    try:
        completed = subprocess.run(  # nosec B603
            [executable, "interop", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, dict) and parsed.get("schema") == INTEROP_SCHEMA:
                descriptor = parsed
    except (OSError, subprocess.TimeoutExpired, ValueError):
        descriptor = None
    _DESCRIPTORS[key] = descriptor
    return descriptor


def offers(descriptor: Optional[Mapping[str, Any]], path: Iterable[str], *flags: str) -> bool:
    """Whether the descriptor lists command ``path`` accepting every flag in ``flags``."""
    if not descriptor:
        return False
    want = list(path)
    for command in descriptor.get("commands", []):
        if command.get("path") == want:
            offered = {str(f).lstrip("-") for f in command.get("flags", [])}
            return all(f.lstrip("-") in offered for f in flags)
    return False


def parse_result(stdout: str, component: str) -> Optional[Dict[str, Any]]:
    """The ``howl.result/v1`` envelope a command printed on stdout, or None."""
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    if (
        isinstance(parsed, dict)
        and parsed.get("schema") == RESULT_SCHEMA
        and parsed.get("component") == component
        and isinstance(parsed.get("status"), str)
    ):
        return parsed
    return None
