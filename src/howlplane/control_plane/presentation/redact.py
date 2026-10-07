"""The one redaction primitive for operator-visible text.

Every path that shows log lines, diagnostics or error text to an operator
(initial tail, ``--follow``, the systemd journal, JSON events, diagnostic
files) must pass through ``redact_operator_text``. Add new secret classes here
so they are covered everywhere at once.
"""

import re

_REDACTED = "[REDACTED]"

# A value is only treated as a credential when it looks like one: credential characters, long enough,
# and (where prose could match) containing a digit. Requirements and review notes are full of phrases
# such as "API token once", "Bearer <token>" and "the token: it is hashed"; masking the word after a
# keyword rewrote what users asked for before any agent read it (DOG-029). Placeholders such as
# <token>, $TOKEN and {token} are documentation, not secrets.
_CREDENTIAL = r"[A-Za-z0-9._~+/=-]"
_HAS_DIGIT = rf"(?={_CREDENTIAL}*\d)"
_SECRET_NAME = r"[\w-]*(?:token|password|passwd|secret|api[_-]?key)"

_PATTERNS = [
    # Authorization headers and bearer tokens.
    re.compile(rf"(?i)(authorization\s*[:=]\s*[\"']?(?:bearer\s+|basic\s+)?){_HAS_DIGIT}{_CREDENTIAL}{{8,}}"),
    re.compile(rf"(?i)(\bbearer\s+){_HAS_DIGIT}{_CREDENTIAL}{{8,}}"),
    # name=value for secret-looking names: `=` does not occur in prose, so any non-placeholder value.
    # Stops at quotes so JSON stays valid.
    re.compile(rf"(?i)((?:{_SECRET_NAME})\s*=\s*[\"']?)(?![<${{])[^\s,;\"'\\]+"),
    # name: value is common in prose, so only a credential-shaped value (as in YAML) counts. A quoted
    # JSON key is not matched: HowlPlane redacts its own serialized manifest, whose lease "token" is a
    # coordination value, not a credential.
    re.compile(rf"(?i)((?:{_SECRET_NAME})\s*:\s*[\"']?){_HAS_DIGIT}{_CREDENTIAL}{{6,}}"),
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
