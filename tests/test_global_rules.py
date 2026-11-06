"""AGENTS.global.md is generated from AGENTS.md and holds only portable rules."""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "generate_global_rules", REPO_ROOT / "scripts" / "generate_global_rules.py"
)
generate_global_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_global_rules)


def _read(name):
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_generated_file_matches_the_canonical_rulebook():
    assert _read("AGENTS.global.md") == generate_global_rules.render(_read("AGENTS.md"))


@pytest.mark.parametrize(
    "heading", ["## Core Directives", "## Continuous Test Governance", "## Grounding Protocol", "## Epistemic Humility"]
)
def test_portable_rules_are_included(heading):
    assert heading in _read("AGENTS.global.md")


@pytest.mark.parametrize(
    "repository_only", ["## Domain Routing", "## Agent Entry Points", "## Prompt Library", "SKILLS_MANIFEST_START"]
)
def test_repository_only_guidance_is_excluded(repository_only):
    assert repository_only not in _read("AGENTS.global.md")


def test_global_rules_are_much_smaller_than_the_full_rulebook():
    assert len(_read("AGENTS.global.md")) < len(_read("AGENTS.md")) / 2


def test_global_rules_say_where_relative_paths_resolve():
    text = _read("AGENTS.global.md")
    assert "~/.howlplane/.agents/rules/" in text


@pytest.mark.parametrize(
    "broken",
    [
        "no markers",
        f"{generate_global_rules.START}\nonly start",
        f"only end\n{generate_global_rules.END}",
        f"{generate_global_rules.END}\nreversed\n{generate_global_rules.START}",
        f"{generate_global_rules.START}\n{generate_global_rules.START}\n{generate_global_rules.END}",
    ],
)
def test_damaged_markers_are_refused(broken):
    with pytest.raises(ValueError):
        generate_global_rules.render(broken)
