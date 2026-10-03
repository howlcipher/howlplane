# run-005 independent verification (Phase 19)

Target: dogfood-missions/run-005/household-tasks. Howl status COMPLETE, exit 0, engine = howlplane @82d3cde (src unchanged through a3c7d7e, which only touched tests).
Session: Codex plan/implement, Cursor review -> AUDIT_NO_VERDICT (DOG-003 path exercised for real), AGY review CLEAN, Codex acceptance ACCEPTED.
Checked as a user (module is `household.py`, per README), separate process per command, temp DB:
- add / list / due --on / complete --on / history: correct.
- recurrence: weekly task completed 2026-10-03 -> next due 2026-10-10; not due 10-09, due 10-10.
- persistence across processes: yes (SQLite); default path ~/.local/share/household-tasks/tasks.sqlite3 matches README (checked under temp HOME).
- invalid input: empty name, --every 0/abc, unknown id, duplicate/backdated and future completion, bad date: nonzero exit with clear message, no traceback.
- tests: `bash scripts/test.sh` -> 8 tests OK.
- README accurate against observed behavior.
No manual edits to the generated app, no internal state edits, no bypasses. Artifact: artifacts/run-005-household-tasks-complete.tgz.

CLEAN RUN 1 (of 2, on fixed code): PASS
