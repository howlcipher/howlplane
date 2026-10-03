# run-012 verification
Engine: howlplane dogfood/DOG-013-review-convergence 82414bf. Exit 0, Status COMPLETE, Independent audit CLEAN (Cursor, 116 s, first pass), Codex acceptance ACCEPTED. 0 reroutes, 0 rework rounds. Wall clock 19:20-19:29.
Independent user verification (copy of the tree in scratchpad, README commands verbatim):
- add (incl. --start), list, due, complete (incl. --on), history: correct output, rc 0.
- Recurrence: completion 2026-10-02 + 3 days -> next due 2026-10-05; `due --as-of 2026-10-05` lists only that task. Correct.
- Persistence: data/tasks.sqlite3 created; state survives separate processes.
- Invalid input: empty name, --every 0/abc, bad --as-of, unknown command -> rc 2 with usage; unknown id, future date, duplicate completion -> rc 1 "Error: ...". Matches README.
- scripts/build.sh rc 0; scripts/test.sh: 14 tests OK.
- README: concise usage, matches behavior.
Result: CLEAN RUN on engine 82414bf. However the engine then changed (DOG-015, a1d8ce2), so the streak restarts at run-013.
Gap noticed: the CLEAN review's text was not printed and the manifest was deleted on COMPLETE, so its non-blocking notes cannot be audited (DOG-015).
