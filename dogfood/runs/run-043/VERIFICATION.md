# run-043 independent verification (operator, 2026-10-08)
Engine: howlplane main bb7ba21. Session 8914e917: Claude plan, Codex implementation, Claude review (CLEAN, first pass), Claude acceptance. 5.1 min, exit 0, 0 reroutes, rework rounds 0.

## Deliverable
- `python3 -m unittest discover -s tests`: 21 tests OK (11 original + 10 new). `git diff 5169bf2 -- tests/test_pingbot.py` removes no line: original tests unchanged.
- Behavioural probes through the real CLI against a local HTTP webhook (runs/run-043/verify043.sh), 26/26 PASS:
  - file token with base64 padding sent as `Authorization: Bearer c2Vj...OQ==` intact; env token wins over file; no token or empty token -> no Authorization header; payload byte-identical to before.
  - show-config: 28-char token -> `****OQ==`; 12 chars -> `****1234`; 11 chars and short -> `****`; unset token not listed and output identical to the pre-change output; secret-named unknown file keys (DEPLOY_SECRET, my_api_key) masked; non-secret lines unchanged.
  - `send --verbose` prints `Authorization: Bearer ****`, never the token.
  - HTTP 401/403 -> exit 3, stderr exactly `pingbot: authentication failed (HTTP 40x): check PINGBOT_API_TOKEN`; HTTP 500 -> exit 1 `pingbot: webhook returned HTTP 500`; network error exit 1; malformed line -> exit 2 `line 2: expected KEY=value`; config errors never contain the token.
- README: documents PINGBOT_API_TOKEN, the masking rule, exit code 3 and "environment or a config file only you can read, never in the repository"; every claim checked above or by reading the code (split on first `=`, whitespace stripped, printable-ASCII-only tokens -> config error without the token). One unrequested but accurate line ("Set PINGBOT_URL ..."); reviewer flagged it non-blocking.
- Design addition beyond the request: tokens with control or non-ASCII characters are rejected as a configuration error (documented, tested, reviewer noted). Not a requirement violation.

## Howl-specific checks
- User WIP: `sha256sum -c dogfood-missions/run-043/wip.sha256` OK for pingbot/templates.py and notes/release-checklist.md; `git diff pingbot/templates.py` identical to the pre-run diff. HEAD still 5169bf2, nothing committed or stashed.
- No Howl state in the repo: no session, manifest or lease files; only ignored __pycache__.
- Redaction (DOG-029): the report Goal equals MISSION.md byte for byte (2250 chars), including `Authorization: Bearer <token>`, `PINGBOT_API_TOKEN=<your-token>==`, "the token itself", "token never leaks". Zero `[REDACTED]`/`<redacted>` markers in stdout/stderr; the only match for "redacted" is the reviewer's own prose.
- Reviewer diff (DOG-027/030): the review scoped by HowlPlane's diff ("The diff touches only README.md, cli.py, client.py, config.py and the tests. templates.py and notes/ are not in it"), correctly excluding the pre-existing WIP; it quoted literal token-shaped test values (`abc123==`, `K = a=b = c`) with no masking.
- Out of scope, did not fire: DOG-026 rework (verification passed first time), DOG-034 (no commit).

Verdict: CLEAN.
