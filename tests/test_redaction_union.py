"""Evidence and operator redaction share one credential pattern set (TUI-00 follow-up).

Before, each primitive missed secrets the other caught: bearer tokens and URL
credentials reached the durable evidence ledger, and private keys reached
operator text. Prose that only mentions a secret stays readable (DOG-029).
"""

import pytest

from howlplane.control_plane.evidence_ledger import redact_sensitive_data
from howlplane.control_plane.presentation.redact import redact_operator_text

pytestmark = pytest.mark.unit

SECRETS = [
    ("Authorization: Bearer abcd1234efgh5678", "abcd1234efgh5678"),
    ("clone https://user:hunter2@host/repo.git", "hunter2"),
    ("-----BEGIN PRIVATE KEY-----\nMIIBsecret\n-----END PRIVATE KEY-----", "MIIBsecret"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\n-----END OPENSSH PRIVATE KEY-----", "b3Blbn"),
    ("api_key=sk-abcdefghijklmnopqrstuv", "sk-abcdefghijklmnopqrstuv"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
]


@pytest.mark.parametrize("redact", [redact_sensitive_data, redact_operator_text])
@pytest.mark.parametrize("text,secret", SECRETS)
def test_every_surface_masks_every_credential_class(redact, text, secret):
    assert secret not in redact(text)


@pytest.mark.parametrize("redact", [redact_sensitive_data, redact_operator_text])
def test_prose_about_secrets_is_kept(redact):
    text = "the token: it is hashed once per session"
    assert redact(text) == text


def test_only_evidence_masks_email_addresses():
    assert redact_sensitive_data("mail ops@example.com") == "mail [REDACTED_EMAIL]"
    assert redact_operator_text("mail ops@example.com") == "mail ops@example.com"
