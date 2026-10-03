# run-006 independent verification (Phase 19)

Target: dogfood-missions/run-006/household-tasks. Howl status COMPLETE, exit 0, engine = howlplane src identical to 82d3cde (HEAD a3c7d7e, tests only).
Session: Codex plan/implement, Cursor review hit EXECUTION_BUDGET_EXCEEDED (genuine 300s timeout, rerouted), AGY review CLEAN, Codex acceptance ACCEPTED.
Checked as a user (`bash scripts/run.sh`, JSON output), separate process per command, temp DB:
- add / list / due --on / complete --on / history: correct.
- recurrence: weekly task completed 2026-10-03 -> next due 2026-10-10; `[]` on 10-09, due on 10-10.
- persistence across processes: yes (SQLite); default data/tasks.sqlite3 beside the module as documented.
- invalid input: empty name, --every 0/abc, unknown id, backdated/duplicate completion, bad date: exit 2 with clear message, no traceback.
- tests: `bash scripts/test.sh` -> 6 tests OK.
- README accurate against observed behavior.
No manual edits to the generated app, no internal state edits, no bypasses. 

CLEAN RUN 2 (of 2, consecutive with run-005): PASS
