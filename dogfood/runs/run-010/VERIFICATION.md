# run-010 independent verification (Phase 19)

Target: dogfood-missions/run-010/household-tasks. Howl status COMPLETE, exit 0, engine = howlplane dogfood/rework-and-default-verify @aedf82f (unchanged since run-009).
Session: zero failures, zero reroutes. Codex plan/implement; verification derived live and passed; Cursor (`--mode ask`) completed the review itself with a CLEAN verdict (first time Cursor produced a clean review); Codex acceptance ACCEPTED. Rework loop did not fire (no findings).
Checked as a user, separate process per command, temp DB, using this app's own README (flags `--due`, `HOUSEHOLD_TASKS_DB`):
- add / list / due --on / complete --on / history: correct.
- recurrence: weekly task completed 2026-10-03 -> next due 2026-10-10; "No due tasks." on 10-09, due on 10-10.
- persistence across processes: yes (SQLite); default ~/.household_tasks/tasks.sqlite3 and HOUSEHOLD_TASKS_DB both work as documented.
- invalid input: empty name, --every 0/abc, unknown id, backdated and future completion, bad date: nonzero exit (2 or 1), clear message, no traceback.
- tests: bash scripts/test.sh -> 7 tests OK.
- README accurate against observed behavior.
No manual edits to the generated app, no internal state edits, no bypasses, no recovery steps.

CLEAN RUN 2 (of 2, consecutive with run-009) on the new code: PASS
