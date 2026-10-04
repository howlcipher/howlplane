# run-015 verification — CLEAN RUN 2/2 (engine 4a5d697)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, rework 0. Cursor review CLEAN (113 s) with 6 NON-BLOCKING notes in the report (none blocking-class; note 1 led to DOG-017). Codex acceptance ACCEPTED. Wall clock 20:02-20:11.
Independent user verification (copy of the tree, README commands verbatim):
- add (incl. --due), list, list --due, list --on, list --json, complete, history: correct, rc 0.
- Due boundary: completed 2026-10-03 every 7 -> `list --on 2026-10-09 --due` excludes it, `--on 2026-10-10 --due` includes it.
- Persistence: data/tasks.sqlite3; state survives separate processes.
- Invalid input (empty name, interval 0, unknown id, future date, duplicate, bad date, unknown command): rc 2 with a clear message, as the README states.
- unittest 12 OK; scripts/test.sh rc 0; scripts/demo.sh rc 0.
0 internal-state edits, 0 bypasses, 0 manual completion. Runs 014 and 015 are consecutive clean runs on identical engine source.
