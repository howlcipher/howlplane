#!/usr/bin/env python3
"""Report which tools the test tiers need and whether this environment has them.

``make check-tools`` runs this. It answers "why would the full suite fail here?"
before a long run does:

* ``fast``  - needed by ``make test-fast-python`` (the default hermetic tier)
* ``full``  - additionally needed by ``make test-full`` (slow and integration tests)
* ``extra`` - optional Python extras; their tests skip locally and fail in CI

Exit status is 1 when something the requested tier needs is missing or unusable.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

INSTALL = {
    "slopslint": "bash scripts/install_slopslint.sh  (and unset BUN_OPTIONS if it is set to --smol)",
    "howlframe": "build the pinned binary as .github/workflows/test.yml does, or set HOWLFRAME_BIN",
    "go": "install Go 1.26+ (https://go.dev/dl/)",
}
EXTRAS = {"chromadb": "vector", "bs4": "research", "litellm": "agent-frameworks", "pdoc": "dev"}


def module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def binary_problem(name: str, version_substring: Optional[str] = None) -> Optional[str]:
    """None when the tool runs (with the expected version), else why it cannot be used."""
    path = os.environ.get("HOWLFRAME_BIN") if name == "howlframe" and os.environ.get("HOWLFRAME_BIN") else shutil.which(name)
    if not path or not os.access(path, os.X_OK):
        return "not found"
    if name == "howlframe":
        return None
    arg = "version" if name == "go" else "--version"
    try:
        result = subprocess.run([path, arg], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        return f"installed but failed to run: {detail[0] if detail else result.returncode}"
    if version_substring and version_substring not in result.stdout + result.stderr:
        return f"unexpected version (wanted {version_substring})"
    return None


def collect() -> List[Dict[str, str]]:
    rows = [{"name": "python>=3.11", "tier": "fast", "status": "ok" if sys.version_info >= (3, 11) else "missing",
             "detail": sys.version.split()[0], "fix": "use Python 3.11 or newer"}]
    for module in ("pytest", "jsonschema", "yaml"):
        rows.append({"name": module, "tier": "fast", "status": "ok" if module_present(module) else "missing",
                     "detail": "", "fix": 'pip install -e ".[dev]"'})
    for name, version, tier in (("slopslint", "0.1.0", "fast"), ("go", None, "full"), ("howlframe", None, "full")):
        problem = binary_problem(name, version)
        rows.append({"name": name, "tier": tier, "status": "ok" if problem is None else "missing",
                     "detail": problem or "", "fix": INSTALL[name]})
    for module, extra in EXTRAS.items():
        rows.append({"name": module, "tier": "extra", "status": "ok" if module_present(module) else "missing",
                     "detail": f"extra: {extra}", "fix": f'pip install ".[{extra}]"'})
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", choices=["fast", "full"], default="full", help="Tier to gate the exit status on")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = collect()
    needed = {"fast"} if args.tier == "fast" else {"fast", "full"}
    blocking = [r for r in rows if r["tier"] in needed and r["status"] != "ok"]
    if args.json:
        print(json.dumps({"tier": args.tier, "ok": not blocking, "checks": rows}, indent=2))
    else:
        for row in rows:
            mark = "ok     " if row["status"] == "ok" else "MISSING"
            print(f"[{mark}] {row['tier']:<5} {row['name']:<14} {row['detail']}")
        for row in blocking:
            print(f"  fix {row['name']}: {row['fix']}")
        print("Ready for the " + args.tier + " tier." if not blocking else f"Not ready for the {args.tier} tier.")
        if any(r["tier"] == "extra" and r["status"] != "ok" for r in rows):
            print("Missing extras only skip their tests locally; CI (HOWLPLANE_REQUIRE_TOOLS=1) fails on them.")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
