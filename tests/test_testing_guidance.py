"""The lifecycle prompts and skills enforce the Test Impact Assessment and agree on staged testing.

These are deliberately coarse: they guard the requirements that matter (where the
TIA is enforced, who looks for duplicate tests, no contradictory "full suite after
every edit") without freezing prose word for word.
"""

from pathlib import Path
import re

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.contract


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("prompt", ["verify_change", "ship_check"])
def test_lifecycle_prompts_require_the_test_impact_assessment(prompt):
    text = _read(f".agents/prompts/{prompt}.md")
    assert "Test Impact Assessment" in text and "tia " in text


def test_ship_check_gates_completion_on_the_recorded_assessment():
    text = _read(".agents/prompts/ship_check.md")
    assert "tia check" in text and "blocks completion" in text and "final working tree" in text


def test_test_falsifier_owns_duplicate_and_obsolete_test_review():
    line = next(l for l in _read(".agents/prompts/review_change.md").splitlines() if "`test-falsifier`" in l)
    for needle in ("duplicate", "obsolete", "names claim", "append-only"):
        assert needle in line


GUIDANCE = [
    "AGENTS.md",
    ".agents/skills/test_and_verify/SKILL.md",
    ".agents/skills/quality_assurance/SKILL.md",
    ".agents/skills/defensive_debugging/SKILL.md",
    ".agents/prompts/verify_change.md",
    ".agents/prompts/ship_check.md",
    "documentation/TESTING.md",
]


@pytest.mark.parametrize("path", GUIDANCE)
def test_no_guidance_demands_the_full_suite_after_every_edit(path):
    text = re.sub(r"\s+", " ", _read(path))
    for sentence in re.split(r"(?<=[.!?]) ", text):
        if re.search(r"after (each|every)[^.]{0,40}(edit|change)", sentence, re.I) and re.search(
            r"full (regression )?(gate|suite)|make test-full", sentence, re.I
        ):
            assert re.search(r"\b(not|never)\b", sentence, re.I), f"{path}: {sentence}"


def test_coverage_is_a_signal_everywhere_it_is_discussed():
    for path in (".agents/skills/test_and_verify/SKILL.md", ".agents/skills/quality_assurance/SKILL.md",
                 "documentation/TESTING.md"):
        text = re.sub(r"\s+", " ", _read(path)).lower()
        assert "signal" in text, path
        assert "strict project coverage thresholds" not in text


def test_working_protocol_matches_the_staged_policy():
    step = next(l for l in _read("improvements.md").splitlines() if l.startswith("7. **Finish the loop:**"))
    assert "before pull-request integration or final completion" in step
    assert "Test Impact Assessment" in step
