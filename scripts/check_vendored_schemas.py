#!/usr/bin/env python3
"""Check vendored schema copies against the producer commit their SOURCE.md pins.

Usage:
    check_vendored_schemas.py SOURCE.md UPSTREAM_PATH=LOCAL_PATH [...]

SOURCE.md must name the producer (``github.com/howlcipher/<repo>`` or
``Repository: howlcipher/<repo>``) and a 40-hex ``Pinned commit``. The check
fails when the pinned commit is not on the producer's default branch, or when
a local copy differs from the file at that commit. A copy that matches the pin
but differs from the producer's current default branch only warns: re-vendoring
is a deliberate change (docs/INTEROP.md in howlcipher/howl, section 6).
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def main(argv):
    if len(argv) < 2 or any("=" not in pair for pair in argv[1:]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    source = Path(argv[0]).read_text(encoding="utf-8")
    repo = re.search(r"(?:github\.com/|Repository:\s*)howlcipher/([A-Za-z0-9_.-]+)", source)
    pin = re.search(r"Pinned commit:\**\s*`?([0-9a-f]{40})", source)
    if not repo or not pin:
        print(f"{argv[0]}: cannot find the producer repository and a 40-hex pinned commit", file=sys.stderr)
        return 2
    repo, pin = repo.group(1).removesuffix(".git"), pin.group(1)

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://github.com/howlcipher/{repo}.git"
        git("clone", "--quiet", "--filter=blob:none", "--no-checkout", url, tmp, cwd=".")
        on_main = subprocess.run(["git", "merge-base", "--is-ancestor", pin, "origin/HEAD"], cwd=tmp).returncode == 0
        if not on_main:
            print(f"FAIL {argv[0]}: pinned commit {pin[:12]} is not on {repo}'s default branch")
            failures += 1
        for pair in argv[1:]:
            upstream, local = pair.split("=", 1)
            have = Path(local).read_bytes()
            pinned = git("show", f"{pin}:{upstream}", cwd=tmp)
            if have != pinned:
                print(f"FAIL {local}: differs from {repo}@{pin[:12]}:{upstream}")
                failures += 1
                continue
            head = git("show", f"origin/HEAD:{upstream}", cwd=tmp)
            note = "" if head == pinned else f" (WARN: {repo}'s default branch has a newer version; re-vendor deliberately)"
            print(f"ok   {local} matches {repo}@{pin[:12]}{note}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
