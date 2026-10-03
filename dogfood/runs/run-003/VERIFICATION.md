# run-003 independent verification (Phase 19)

Target: dogfood-missions/run-003/household-tasks. Howl status COMPLETE, exit 0, engine = howlplane @b07a4e0.
Checked as a user, separate process per command, temp DB:
- add / list / due --on / complete --on / history: correct output.
- recurrence: weekly task completed 2026-10-03 -> next due 2026-10-10; not due 10-09, due 10-10.
- persistence across processes: yes (SQLite); default DB at ~/.household_tasks/tasks.sqlite3 (checked under temp HOME).
- invalid input: empty name, --every 0/abc, unknown id, duplicate/backdated/future completion, bad date: all rc=2 with clear message, no traceback.
- tests: python3 -m unittest discover -s tests -> 5 tests OK; flake8 7.3.0 clean.
- usage docs: README accurate against observed behavior.
Notes: files left uncommitted in the target (Howl is told not to commit). Artifact: artifacts/run-003-household-tasks-complete.tgz.
Manual edits to generated app: none. Internal state edits: none. Bypasses: none.

CLEAN RUN 1: PASS

RETRACTION (after run-004): CLEAN RUN 1 does not count toward the two-run target. The DOG-003 defect was present in run-003 (Cursor empty review) and acceptance tolerated it by chance. The streak restarts after the DOG-003 fix.
