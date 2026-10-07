# run-040 independent verification (operator, 2026-10-05)
Deliverable: 35 tests OK; admin create-user/issue-token/revoke/list-users; 4 auth failure forms -> 401; cross-user GET/DELETE -> 404 and the row survives; no plaintext token in a DB dump; upgrade idempotent (users 1, bookmarks 2).
DOG-029 live: the session manifest goal equals the mission text byte for byte; the report Goal has no redaction markers.
New: reviewer note 4 reported `API_TOKEN='[REDACTED]'` in the README; the file holds `API_TOKEN='paste-the-printed-token-here'`. Cause: the diff evidence was redacted -> DOG-030.
Verdict: deliverable meets requirements; run NOT clean (DOG-030).
