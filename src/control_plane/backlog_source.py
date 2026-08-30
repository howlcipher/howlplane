#!/usr/bin/env python3
"""
backlog_source.py

Read-only selection of the next eligible item from a project's ranked markdown
backlogs (`bugs.md` and `improvements.md`).

This is the implementation behind the `select_next_evidence_backed_task`
authority action, which existed only as a string literal in
`authority_profile.py` until now. No provider is invoked here and nothing is
written: the backlog is parsed, filtered and ranked deterministically, and the
caller decides what to do with the result.

Two properties of the real backlogs drive the parsing rules, and both were
found by reading HowlFrame's files rather than assuming a shape:

1. **A file can hold several tables.** `improvements.md` carries the live
   ranked backlog plus two historical "V2"/"V3" tables with a different column
   count. Parsing every pipe row would offer work shipped months ago as
   pending, so only the table under the first `## Ranked Backlog` heading is
   read, and parsing stops at the next `##`.

2. **`Pending` is not a prefix.** The real status column contains
   `Pending`, `Pending ⚠️ below floor` and `Pending — blocked on #88`. A
   `startswith` test would hand an unattended run an item that is explicitly
   below the ROI floor -- which the backlog says needs human confirmation
   before being worked -- or one blocked on another item. Only an exact
   `Pending` is eligible.
"""

from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from src.control_plane.task_spec import DataClassSerializationMixin

BACKLOG_ITEM_SCHEMA_VERSION = "howlplane.backlog_item/v1"

# The heading that introduces a file's live ranked backlog. Only the first
# occurrence is read.
RANKED_BACKLOG_HEADING = "## Ranked Backlog"

# The one status an unattended run may act on. Everything else -- including
# every other string starting with "Pending" -- is left for a human.
ELIGIBLE_STATUS = "Pending"

# Default backlog files, highest-priority first. Bugs outrank improvements of
# similar score, which is the ordering both backlogs already document.
DEFAULT_BACKLOG_FILES = ("bugs.md", "improvements.md")

_ROW_PATTERN = re.compile(r"^\|\s*(\d+)\s*\|")
_SCORE_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")
_LINK_PATTERN = re.compile(r"^\[(?P<text>.+?)\]\((?P<anchor>#[^)]*)\)$")


class BacklogParseError(ValueError):
    """Raised when a backlog file cannot be parsed into a ranked table."""


@dataclass(frozen=True)
class BacklogItem(DataClassSerializationMixin):
    """One ranked backlog row, with enough context to open a governed task."""

    item_id: str
    source_file: str
    title: str
    status: str
    score: Optional[float]
    anchor: Optional[str] = None
    rationale: Optional[str] = None
    kind: str = "improvement"
    schema: str = BACKLOG_ITEM_SCHEMA_VERSION

    @property
    def task_id(self) -> str:
        """Stable governed-task id for this item."""
        prefix = "BUG" if self.kind == "bug" else "IMP"
        return f"HOWLFRAM-{prefix}-{self.item_id}"

    @property
    def is_eligible(self) -> bool:
        """Whether an unattended run may act on this row without a human."""
        return self.status == ELIGIBLE_STATUS


@dataclass
class BacklogSelection(DataClassSerializationMixin):
    """The result of one selection pass, including why rows were excluded."""

    eligible: List[BacklogItem] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)

    def next_item(self, skip: Sequence[str] = ()) -> Optional[BacklogItem]:
        """Highest-ranked eligible item whose task id is not in `skip`."""
        skipped = set(skip)
        for item in self.eligible:
            if item.task_id not in skipped:
                return item
        return None


def _split_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.split("|")[1:-1]]


def _parse_score(cell: str) -> Optional[float]:
    """Reads the leading number out of a `2.5 (5×1÷2)` score cell."""
    match = _SCORE_PATTERN.match(cell)
    return float(match.group(1)) if match else None


def _parse_title(cell: str) -> tuple:
    """Returns (title, anchor) for a `[text](#anchor)` or plain cell."""
    match = _LINK_PATTERN.match(cell)
    if match:
        return match.group("text"), match.group("anchor")
    return cell.strip("*").strip(), None


def parse_backlog_file(path: Union[str, Path]) -> List[BacklogItem]:
    """Parses the live ranked table out of one backlog file.

    Only the table under the *first* `## Ranked Backlog` heading is read, and
    reading stops at the next `##` heading, so historical tables later in the
    same file are never mistaken for open work.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    start = next(
        (i for i, line in enumerate(lines) if line.startswith(RANKED_BACKLOG_HEADING)),
        None,
    )
    if start is None:
        raise BacklogParseError(
            f"{path} has no '{RANKED_BACKLOG_HEADING}' heading, so it has no "
            f"live ranked table to read."
        )
    end = next(
        (i for i, line in enumerate(lines[start + 1:], start + 1) if line.startswith("## ")),
        len(lines),
    )

    kind = "bug" if path.name == "bugs.md" else "improvement"
    items: List[BacklogItem] = []
    for line in lines[start:end]:
        match = _ROW_PATTERN.match(line)
        if not match:
            continue
        cells = _split_row(line)
        if len(cells) < 3:
            continue
        title, anchor = _parse_title(cells[1])
        items.append(
            BacklogItem(
                item_id=cells[0],
                source_file=path.name,
                title=title,
                anchor=anchor,
                status=cells[2],
                score=_parse_score(cells[3]) if len(cells) > 3 else None,
                rationale=cells[-1] if len(cells) > 4 else None,
                kind=kind,
            )
        )
    return items


class BacklogSource:
    """Deterministic, read-only next-item selection over a project's backlogs."""

    def __init__(
        self,
        repo_root: Union[str, Path],
        backlog_files: Sequence[str] = DEFAULT_BACKLOG_FILES,
        roi_floor: float = 0.5,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.backlog_files = list(backlog_files)
        # The floor both backlogs document. A row at or above it may be worked
        # unattended; one below it is flagged in the backlog itself and needs
        # explicit human confirmation, so it is never selected here.
        self.roi_floor = roi_floor

    def select(self) -> BacklogSelection:
        """Reads every configured backlog and ranks what may be worked.

        Bugs are returned ahead of improvements, and within each file rows keep
        their committed rank order rather than being re-sorted by score: the
        backlog's own ordering already encodes judgement this code cannot
        reconstruct.
        """
        selection = BacklogSelection()
        for filename in self.backlog_files:
            path = self.repo_root / filename
            if not path.is_file():
                continue
            selection.files_read.append(filename)
            for item in parse_backlog_file(path):
                reason = self._ineligibility_reason(item)
                if reason is None:
                    selection.eligible.append(item)
                elif item.status.startswith(ELIGIBLE_STATUS):
                    # Only near-misses are worth recording. Rows that are simply
                    # Done would bury the interesting exclusions.
                    selection.excluded.append({
                        "item_id": item.item_id,
                        "source_file": item.source_file,
                        "status": item.status,
                        "reason": reason,
                    })
        return selection

    def _ineligibility_reason(self, item: BacklogItem) -> Optional[str]:
        """Why this row may not be worked unattended, or None if it may."""
        if not item.is_eligible:
            # Covers "Pending ⚠️ below floor" and "Pending — blocked on #88",
            # which a prefix match would have wrongly admitted.
            return f"STATUS_NOT_ELIGIBLE:{item.status}"
        if item.score is not None and item.score < self.roi_floor:
            return f"BELOW_ROI_FLOOR:{item.score}"
        return None

    def item_detail(self, item: BacklogItem) -> str:
        """Returns the item's detail section, which is its problem statement.

        A governed task needs the symptom, root cause and deterministic
        acceptance the backlog already records; the one-line table rationale is
        not enough to implement from.
        """
        path = self.repo_root / item.source_file
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        start = next(
            (i for i, line in enumerate(lines)
             if line.startswith(f"### {item.item_id}.")),
            None,
        )
        if start is None:
            return ""
        end = next(
            (i for i, line in enumerate(lines[start + 1:], start + 1)
             if line.startswith("### ")),
            len(lines),
        )
        return "\n".join(lines[start:end]).strip()
