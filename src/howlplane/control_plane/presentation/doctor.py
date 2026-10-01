"""Diagnostic tables for the doctor commands.

Pure presentation over data the readiness code already computes. One display
vocabulary (READY / LIMITED / NEEDS ACTION / UNAVAILABLE) is mapped from the
underlying states here; the JSON output keeps the original fields untouched.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from howlplane.control_plane.presentation.style import Style

READY, LIMITED, NEEDS_ACTION, UNAVAILABLE = "READY", "LIMITED", "NEEDS ACTION", "UNAVAILABLE"
_SEVERITY = {READY: "ok", LIMITED: "attention", NEEDS_ACTION: "attention", UNAVAILABLE: "error"}
_LIMIT_WORDS = {"SESSION_EXHAUSTED": "session limit", "QUOTA_EXHAUSTED": "quota exhausted",
                "RATE_LIMITED": "rate limited"}


def worker_state(summary: Mapping[str, Any], workspace_state: Optional[str] = None) -> Tuple[str, str]:
    """(display status, short detail) for one agent summary."""
    if not summary.get("installed"):
        return UNAVAILABLE, "not installed"
    if summary.get("authenticated") is False:
        return UNAVAILABLE, "not authenticated"
    capacity = (summary.get("capacity") or {}).get("state")
    if capacity in _LIMIT_WORDS:
        return LIMITED, _LIMIT_WORDS[capacity]
    if workspace_state == "TRUST_REQUIRED":
        return NEEDS_ACTION, "workspace trust required"
    if summary.get("unattended_execution") is False:
        return NEEDS_ACTION, "interactive only"
    if summary.get("unattended_execution") is None:
        return READY, "unattended use unverified"
    return READY, ""


def worker_rows(summaries: Sequence[Mapping[str, Any]], workspace: Optional[Mapping[str, Any]] = None
                ) -> List[Tuple[str, str, str]]:
    rows = []
    for summary in summaries:
        ws = None
        if workspace and summary["agent"] in workspace.get("agents", {}):
            ws = workspace["agents"][summary["agent"]].get("effective_state")
        status, detail = worker_state(summary, ws)
        rows.append((summary["name"], status, detail))
    return rows


def summary_line(rows: Sequence[Tuple[str, str, str]]) -> List[str]:
    counts = {s: sum(1 for r in rows if r[1] == s) for s in (READY, LIMITED, NEEDS_ACTION, UNAVAILABLE)}
    labels = [(READY, "ready"), (LIMITED, "temporarily limited"), (NEEDS_ACTION, "need action"),
              (UNAVAILABLE, "unavailable")]
    return [f"{counts[key]} worker{'s' if counts[key] != 1 else ''} {text}" for key, text in labels if counts[key]]


def render_worker_summary(summaries: Sequence[Mapping[str, Any]], workspace: Optional[Mapping[str, Any]] = None,
                          style: Optional[Style] = None) -> List[str]:
    """Table + one-line summary + next step, shown above the detailed per-worker blocks."""
    style = style or Style()
    rows = worker_rows(summaries, workspace)
    lines = style.table(["Worker", "Status", "Detail"], rows, severity_col=1,
                        severity_of=lambda cell: _SEVERITY.get(cell, "info"))
    lines += [""] + summary_line(rows)
    statuses = {r[1] for r in rows}
    lines += ["", style.section("Next")]
    if statuses <= {READY}:
        lines.append("No action required.")
    elif statuses <= {READY, LIMITED}:
        lines.append("No action required. Limited workers recover when their limit resets.")
    else:
        lines += ["Fix the workers marked NEEDS ACTION or UNAVAILABLE (per-worker detail: howlplane agents doctor), then re-check:",
                  "", style.command_block("howlplane agents doctor --refresh")]
    lines.append("")
    return lines


def render_checks_table(checks: Sequence[Any], style: Optional[Style] = None) -> List[str]:
    """Workspace diagnostics (``howlplane doctor``) as Component / Status / Detail."""
    style = style or Style()
    words = {"ok": "OK", "warning": "WARNING", "error": "ERROR"}
    sev = {"OK": "ok", "WARNING": "attention", "ERROR": "error"}
    rows = [(c.name, words.get(c.status, c.status.upper()), c.message) for c in checks]
    lines = style.table(["Component", "Status", "Detail"], rows, severity_col=1,
                        severity_of=lambda cell: sev.get(cell, "info"))
    actions = [(c.name, (c.details or {}).get("action")) for c in checks if (c.details or {}).get("action")]
    if actions:
        lines += ["", style.section("Next")]
        lines += [f"{name}: {action}" for name, action in actions]
    return lines
