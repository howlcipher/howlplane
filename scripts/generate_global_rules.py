#!/usr/bin/env python3
"""Generate AGENTS.global.md from the marked section of AGENTS.md.

AGENTS.md stays the single canonical rulebook. Global installs load only the part
that applies outside this repository, which saves context in every session.
Run with --check to fail when the generated file is out of date.
"""
import argparse
import pathlib
import sys

START = "<!-- GLOBAL_RULES_START -->"
END = "<!-- GLOBAL_RULES_END -->"
HEADER = """# Global Engineering Context

<!-- Generated from AGENTS.md by scripts/generate_global_rules.py. Do not edit. -->

Outside the HowlPlane repository, relative paths such as `.agents/` and `documentation/` resolve under `~/.howlplane/`. Honor every file in `~/.howlplane/.agents/rules/`. Domain skills are linked into each agent's own skill directory. A project's own `AGENTS.md` also applies when working in that project.

"""


def render(agents_text):
    """Return the global rules text, or raise ValueError on damaged markers."""
    if agents_text.count(START) != 1 or agents_text.count(END) != 1:
        raise ValueError("AGENTS.md must contain each GLOBAL_RULES marker exactly once")
    before, rest = agents_text.split(START)
    if END not in rest:
        raise ValueError("GLOBAL_RULES_END must follow GLOBAL_RULES_START")
    return HEADER + rest.split(END)[0].strip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    target = root / "AGENTS.global.md"
    expected = render((root / "AGENTS.md").read_text(encoding="utf-8"))
    current = target.read_text(encoding="utf-8") if target.exists() else None
    if args.check:
        if current != expected:
            print("AGENTS.global.md is out of date; run scripts/generate_global_rules.py", file=sys.stderr)
            return 1
        return 0
    if current != expected:
        target.write_text(expected, encoding="utf-8")
        print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
