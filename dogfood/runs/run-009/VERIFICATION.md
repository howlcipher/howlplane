# run-009 independent verification (Phase 19)

Target: dogfood-missions/run-009/household-tasks. Howl status COMPLETE, exit 0, engine = howlplane dogfood/rework-and-default-verify (code: bounded rework DOG-011, derived verify DOG-009, plus DOG-004..007, 010).
Session: Codex plan/implement; verification derived live ("No --verify given; using the project's test command: bash scripts/test.sh", passed); Cursor review hit the 300 s execution budget and was rerouted; AGY review CLEAN; Codex acceptance ACCEPTED. The rework loop did not fire (no findings were returned).
Checked as a user, separate process per command, temp DB:
- add / list / due --on / complete --on / history / --json: correct.
- recurrence: weekly task completed 2026-10-03 -> next due 2026-10-10; "No tasks." on 10-09, due on 10-10.
- persistence across processes: yes (SQLite); default ~/.household_tasks/tasks.sqlite3 as documented (checked under temp HOME).
- invalid input: empty name, --every 0/abc, unknown id, backdated and future completion, bad date: exit 2, clear message, no traceback.
- tests: bash scripts/test.sh -> 15 tests OK.
- README accurate against observed behavior.
No manual edits to the generated app, no internal state edits, no bypasses, no recovery steps needed.

CLEAN RUN 1 (of 2) on the new code: PASS
