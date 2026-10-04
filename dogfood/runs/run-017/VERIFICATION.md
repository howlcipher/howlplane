# run-017 verification — CLEAN RUN 2/2 (engine a7f3bc4)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, no rework, Cursor review AUDIT_STATUS CLEAN (0 blocking), Codex acceptance completed. Derived verification `bash scripts/test.sh` rc 0. Session 7ccae04a. Wall clock 22:25-22:34.
Design chosen by the workflow: Python 3 stdlib CLI app/household.py via scripts/run.sh, SQLite at ./data/tasks.sqlite3; design record documentation/design.md. (Different from run-016's design; run-to-run variance as noted in Addendum 2.)
Independent user verification (copy of the tree with data/ removed, README commands verbatim):
- add, list, complete (records date, prints next due), history, due, due --as-of: correct, rc 0.
- Due boundary: every 7, completed 2026-10-01 -> `due --as-of 2026-10-07` empty, `--as-of 2026-10-08` includes it.
- Persistence: data/tasks.sqlite3 created; state survives separate processes; --db selects another file.
- Invalid input: empty name, every 0/abc/365001, unknown id, future date, date not after last completion, bad --as-of, unknown command: nonzero exit with a clear stderr message, as the README states.
- scripts/test.sh rc 0 (flake8 + 6 integration tests OK).
0 internal-state edits, 0 bypasses, 0 manual completion.
