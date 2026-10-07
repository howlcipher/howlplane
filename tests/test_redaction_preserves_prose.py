"""Redaction masks credentials, not the words around them (DOG-029).

Run-039: the goal "prints a newly generated API token once ... issue a new token
for an existing user ... never the token itself ... `Authorization: Bearer <token>`
... revoked token returns 401" reached the agents as "API token <redacted>, ...
token <redacted> an existing user ... Bearer <redacted> ... revoked token
<redacted> 401", because orchestration's private pattern took the word after
"token " as a secret. Review notes in the report were mangled the same way.
"""

import json

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.presentation.redact import redact_operator_text
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import install_all, persist


pytestmark = pytest.mark.contract

PROSE = [
    "Add an admin tool that prints a newly generated API token once and can issue a new token for an existing user.",
    "Store only a hash of each token, never the token itself.",
    "Every endpoint requires `Authorization: Bearer <token>`. A revoked token returns 401.",
    "Note on the token: it is hashed with SHA-256 before storage.",
    "Use bearer authentication; the password field is optional.",
    "Set GITHUB_TOKEN=$GITHUB_TOKEN or pass --api-key {key} in the template.",
    "The secret: keep it out of logs.",
]

SECRETS = [
    ("engine failure token=supersecret", "supersecret"),
    ("Authorization: Bearer abc123def456ghi", "abc123def456ghi"),
    ('curl -H "Authorization: Bearer eyJhbGciOi.J9x1"', "eyJhbGciOi.J9x1"),
    ("password: hunter22", "hunter22"),
    ("export GITHUB_TOKEN=ghp_abcdefghijklmnop12", "ghp_abcdefghijklmnop12"),
    ("https://user:pa55word@example.com/repo", "pa55word"),
    ("key sk-proj-abcdef123456 leaked", "sk-proj-abcdef123456"),
    ("client_secret = 'q8w7e6r5t4'", "q8w7e6r5t4"),
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_and_placeholders_survive_unchanged(text):
    assert module.redact(text) == text
    assert redact_operator_text(text) == text


@pytest.mark.parametrize("text, secret", SECRETS)
def test_credentials_are_still_masked(text, secret):
    assert secret not in module.redact(text)
    assert "[REDACTED]" in module.redact(text)


def test_orchestration_uses_the_canonical_primitive():
    for text, _ in SECRETS:
        assert module.redact(text) == redact_operator_text(text)


def test_the_goal_reaches_the_session_and_its_manifest_verbatim(tmp_path, monkeypatch):
    goal = " ".join(PROSE)
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)

    doc = module.setup(arguments(repo, input=goal), repo)
    path = persist(doc)
    module.save(path, doc, doc["lease"]["token"])

    assert doc["goal"] == goal
    assert json.loads(path.read_text())["goal"] == goal
