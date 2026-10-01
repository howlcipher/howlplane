"""Completion gate: a code-changing task cannot reach ``complete`` without a Test Impact Assessment.

The code-change decision is derived only from the repository delta the control
plane itself captured. No agent-supplied flag is consulted. Paths that are
purely documentation do not require an assessment; everything else (source,
tests, configuration, schemas, unknown file types) does.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Iterable, Optional

from .evidence_ledger import EvidenceLedger
from .test_impact import check_assessment

GATE_BLOCKED_ACTION = "completion_blocked_tia"

_DOC_ONLY_PATTERNS = (
    "*.md",
    "*.txt",
    "docs/*",
    "documentation/*",
)


def is_doc_only_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in _DOC_ONLY_PATTERNS)


def is_code_changing(paths: Iterable[str]) -> bool:
    """True when any changed path is not documentation-only."""
    return any(not is_doc_only_path(p) for p in paths)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    code_changing: bool
    reason: str = ""


def evaluate_completion_gate(
    ledger: Optional[EvidenceLedger],
    task_id: str,
    changed_paths: Iterable[str],
) -> GateResult:
    """Decide whether ``task_id`` may transition to ``complete``."""
    paths = list(changed_paths)
    if not is_code_changing(paths):
        return GateResult(True, False)
    if ledger is None:
        return GateResult(
            False,
            True,
            "task cannot complete: it changed code and no evidence ledger is available "
            "to verify a Test Impact Assessment",
        )
    _doc, reason = check_assessment(ledger, task_id)
    if reason:
        return GateResult(
            False,
            True,
            f"task cannot complete because the Test Impact Assessment is not satisfied ({reason}); "
            f"record it with `howlplane tia record --task-id {task_id} --file tia.json`, "
            f"then re-run or resume the task",
        )
    return GateResult(True, True)
