# run-016 verification — CLEAN RUN 1/2 (engine a7f3bc4, main after DOG-017 merge)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, Rework rounds 1 of 2 (Cursor findings sent back once, converged), Cursor review AUDIT_STATUS CLEAN (0 blocking, 4 non-blocking notes), Codex acceptance completed. Derived verification: `bash scripts/test.sh`. Wall clock 22:07-22:24.
Design chosen by the workflow: single-file Python 3 stdlib CLI (household_tasks.py) + SQLite at $XDG_DATA_HOME/household_tasks/tasks.sqlite3; design record documentation/workflow.md.
Independent user verification (copy of the tree, isolated XDG_DATA_HOME, README commands verbatim):
- add, list, due, complete (records date, shows next due), history: correct, rc 0.
- Due boundary: every 3, completed 2024-01-02 -> `due --on 2024-01-04` empty, `--on 2024-01-05` includes it.
- Persistence: default DB created under XDG dir; state survives separate processes.
- Invalid input (empty name, every 0/-1/abc, bad date, unknown command) rc 2 with usage message; unknown id, future date, date before latest completion rc 1 with "Error:" message, matching the README exit-code contract.
- scripts/test.sh rc 0 (flake8 + 14 tests); `python3 -m unittest discover -s tests` 14 OK.
Observation (non-blocking, no finding): the delivered working tree contains a gitignored .demo/tasks.sqlite3 from the agent's own demo; a copied tree therefore starts the second README block on a non-empty DB. Rerun on a fresh DB gives the expected output. A git clone would not include it.
0 internal-state edits, 0 bypasses, 0 manual completion.
