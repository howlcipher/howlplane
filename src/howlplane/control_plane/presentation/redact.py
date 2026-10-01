"""The one redaction primitive for operator-visible text.

Every path that shows log lines, diagnostics or error text to an operator
(initial tail, ``--follow``, the systemd journal, JSON events, diagnostic
files) must pass through ``redact_operator_text``. Add new secret classes here
so they are covered everywhere at once.
"""

import re

_REDACTED = "[REDACTED]"

_PATTERNS = [
    # Authorization headers and bearer tokens.
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s\"',;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    # key=value / key: value for secret-looking names. Stops at quotes so JSON stays valid.
    re.compile(r"(?i)((?:[\w-]*(?:token|password|passwd|secret|api[_-]?key))\s*[=:]\s*)[^\s,;\"'\\]+"),
    # Credentials embedded in URLs.
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
]
_BARE_TOKENS = re.compile(
    r"\b(?:sk[-_][A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{8,}|AIza[0-9A-Za-z_-]{20,})"
)


def redact_operator_text(text: str) -> str:
    """Mask credentials in free text while keeping the rest of the line readable."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + _REDACTED + (m.group(2) if m.lastindex and m.lastindex > 1 else ""), text)
    return _BARE_TOKENS.sub(_REDACTED, text)
