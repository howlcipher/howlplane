# run-031 independent verification (operator, 2026-10-04)
Target dogfood-missions/run-031/bookmarks, base 195012a; 4 files changed (+214/-19), uncommitted.
- `python3 -m unittest discover -s tests -t .`: 10 tests OK (4 original unchanged + 6 new).
- Legacy DB: copy of old.db (made by the pre-fix code, sha256 a49473...) served by the new code with no manual step: 3 rows intact incl. tags, journal_mode switched to WAL, later count 106 = 3 + 100 + 3.
- Concurrency: 100 POSTs released together by a Barrier against the real ThreadingHTTPServer: 100x 201, 100 unique ids (pre-fix store: 24/30 IntegrityError).
- Tag filter: legacy row tagged python/lang: tag=py -> [], tag=python -> [1], tag=go -> [] (golang row not matched).
- Ordering: same-second inserts list as [107,106,105]; 103 rows paged with limit=7 -> 103 unique, sorted (created_at, id) descending.
- API shape unchanged: keys created_at, id, tags, title, url; status codes unchanged.
- README additions accurate and candid (case-sensitive tags, WAL sidecars/backup, retries may duplicate).
- Leak scan: none.
Observations (not findings): tag matching became case-sensitive (was ASCII-case-insensitive via LIKE); documented, consistent with "exact tag". Test helper ResourceWarnings come from the operator-written original helper.
Verdict: CLEAN.
