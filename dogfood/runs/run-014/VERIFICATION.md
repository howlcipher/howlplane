# run-014 verification — CLEAN RUN 1/2 (engine 4a5d697)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, rework 0. Cursor review CLEAN (270 s) with 5 NON-BLOCKING notes printed in the report (DOG-015); none is a blocking-class defect (checked). Codex acceptance ACCEPTED (50 s). Wall clock 19:48-20:01.
Independent user verification (copy of the tree, XDG_DATA_HOME in scratchpad, README commands verbatim):
- add (incl. --due), list, due, complete, history: correct, rc 0.
- Due boundary: completed 2026-10-03 every 7 -> `--today 2026-10-09 due` = "No tasks due."; `--today 2026-10-10 due` lists it.
- Persistence: $XDG_DATA_HOME/household_tasks/tasks.sqlite3; state survives separate processes; --db demo database works.
- Invalid input: empty name, interval 0/99999, unknown id, future date, duplicate same-day, pre-creation date -> rc 1 "Error: ..."; unknown command -> rc 2. Matches README.
- scripts/test.sh: 11 tests OK + flake8; scripts/demo.sh rc 0.
Minor doc wrinkle (non-blocking, not a Howl defect): the README's `complete 1 --on 2026-10-02` example only works for a task created before that date, as the README's own rule states.
0 internal-state edits, 0 bypasses, 0 manual completion.
