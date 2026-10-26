"""Data files HowlPlane reads at run time, from a checkout or an installed wheel.

A source checkout keeps them at the repository root (``schemas/``,
``contracts/``, ``integrations/``). An installed wheel has no repository root,
so the files other components' edges depend on are also packaged under
``howlplane/control_plane/data/`` (byte-identical copies, enforced by
``tests/test_packaged_data.py``).
"""

from __future__ import annotations

from pathlib import Path

#: The repository root of a source checkout (src/howlplane/control_plane/ -> root).
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
PACKAGED_ROOT = Path(__file__).resolve().parent / "data"

#: Repository-relative files that ship inside the package.
PACKAGED_FILES = (
    ("schemas", "evidence-entry.schema.json"),
    ("contracts", "howlinstinct", "howlinstinct.decision_receipt.v1.schema.json"),
    ("integrations", "howlframe", "candidate_evaluator.hfbc"),
    ("integrations", "howlframe", "project_context_audit.hfbc"),
)


def is_checkout() -> bool:
    """Whether this module runs from a source checkout rather than an installed wheel."""
    return (CHECKOUT_ROOT / "pyproject.toml").is_file() and (CHECKOUT_ROOT / "src" / "howlplane").is_dir()


def data_file(*parts: str) -> Path:
    """The checkout's copy of a repository-relative file when present, else the packaged copy."""
    checkout = CHECKOUT_ROOT.joinpath(*parts)
    if checkout.exists():
        return checkout
    return PACKAGED_ROOT.joinpath(*parts)
