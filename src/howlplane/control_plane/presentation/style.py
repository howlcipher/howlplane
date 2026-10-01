"""Small terminal styling layer for human CLI output.

Everything that decides *whether* and *how* to style lives here so handlers
never embed ANSI codes. Meaning is always carried by words (a badge like
``[OK]``/``WAITING``); color is only reinforcement. Output degrades to plain
ASCII when stdout is not a terminal, ``NO_COLOR`` is set, ``TERM=dumb`` or
``--color never`` is given. JSON paths never call this module.
"""

import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence, TextIO

COLOR_CHOICES = ("auto", "always", "never")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_CODES = {
    "ok": "32",         # green
    "info": "36",       # cyan
    "waiting": "34",    # blue
    "warn": "33",       # yellow
    "error": "31",      # red
    "muted": "2",       # dim
    "command": "1;36",  # bold cyan
    "title": "1",       # bold
}

_SEVERITY_STYLE = {"ok": "ok", "info": "info", "attention": "warn", "error": "error"}


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def terminal_width(default: int = 80, stream: Optional[TextIO] = None) -> int:
    try:
        width = shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        width = default
    return max(40, min(width, 120))


@dataclass(frozen=True)
class Style:
    """Resolved rendering policy for one stream."""

    color: bool = False
    unicode: bool = False
    width: int = 80
    interactive: bool = False

    def paint(self, kind: str, text: str) -> str:
        code = _CODES.get(kind)
        if not self.color or not code or not text:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def severity(self, severity: str, text: str) -> str:
        return self.paint(_SEVERITY_STYLE.get(str(severity), "info"), text)

    def muted(self, text: str) -> str:
        return self.paint("muted", text)

    def command(self, text: str) -> str:
        return self.paint("command", text)

    @property
    def dot(self) -> str:
        return "·" if self.unicode else "-"

    @property
    def rule_char(self) -> str:
        return "─" if self.unicode else "-"

    @property
    def arrow(self) -> str:
        return "→" if self.unicode else "->"

    # Layout helpers -------------------------------------------------------

    def header(self, title: str, badge: str, severity: str = "info") -> List[str]:
        return [f"{self.paint('title', title)} {self.dot} {self.severity(severity, badge)}", ""]

    def section(self, title: str) -> str:
        return self.paint("title", title)

    def kv(self, rows: Sequence[tuple], indent: int = 0, severities: Optional[Mapping[str, str]] = None) -> List[str]:
        rows = [(k, v) for k, v in rows if v not in (None, "")]
        if not rows:
            return []
        pad = max(len(k) for k, _ in rows) + 2
        sev = severities or {}
        out = []
        for key, value in rows:
            shown = self.severity(sev[key], str(value)) if key in sev else str(value)
            out.append(" " * indent + self.muted(f"{key:<{pad}}") + shown)
        return out

    def command_block(self, command: str, indent: int = 2) -> str:
        return " " * indent + self.command(command)

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]],
              severity_col: Optional[int] = None, severity_of=None) -> List[str]:
        rows = [[str(c) for c in row] for row in rows]
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        last = len(headers) - 1
        budget = self.width - sum(widths[:last]) - 2 * last
        if budget >= 12:
            widths[last] = min(widths[last], budget)
        lines = [self.muted("  ".join(h.ljust(widths[i]) if i < last else h for i, h in enumerate(headers)))]
        indent = " " * (sum(widths[:last]) + 2 * last)
        for row in rows:
            cells = []
            for i, cell in enumerate(row[:last]):
                padded = cell.ljust(widths[i])
                if severity_col == i and severity_of:
                    padded = self.severity(severity_of(cell), padded)
                cells.append(padded)
            # The last column wraps (never truncates) so copied output keeps every detail.
            wrapped = textwrap.wrap(row[last], widths[last], break_long_words=True) or [""]
            lines.append("  ".join(cells + [wrapped[0]]).rstrip())
            lines += [indent + part for part in wrapped[1:]]
        return lines


def resolve_style(stream: Optional[TextIO] = None, mode: Optional[str] = None,
                  env: Optional[Mapping[str, str]] = None) -> Style:
    """Decide styling for ``stream`` (default stdout).

    ``mode`` is the ``--color`` value; ``always`` overrides everything except
    that ``NO_COLOR`` is only overridden by an explicit ``--color always``.
    """
    stream = stream if stream is not None else sys.stdout
    env = os.environ if env is None else env
    mode = (mode or env.get("HOWLPLANE_COLOR") or "auto").lower()
    if mode not in COLOR_CHOICES:
        mode = "auto"
    try:
        tty = bool(stream.isatty())
    except (AttributeError, ValueError):
        tty = False
    dumb = env.get("TERM", "") == "dumb"
    if mode == "always":
        color = True
    elif mode == "never":
        color = False
    else:
        color = tty and not dumb and not env.get("NO_COLOR")
    encoding = (getattr(stream, "encoding", None) or "").lower()
    unicode_ok = "utf" in encoding and not dumb and mode != "never" and tty
    width = terminal_width() if tty else 80
    ci = bool(env.get("CI"))
    return Style(color=color, unicode=unicode_ok, width=width, interactive=tty and not dumb and not ci)


def format_duration(seconds: Optional[float]) -> Optional[str]:
    """``06m 14s`` style; None when unknown (never guessed)."""
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes:02d}m {secs:02d}s"
    return f"{secs}s"
