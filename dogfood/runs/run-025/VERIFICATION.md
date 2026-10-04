# run-025 verification — CLEAN RUN 2/2 (engine 7a516c0)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, no rework. Claude planned (VERIFY_COMMAND python3 -m unittest discover -s tests -t .); Codex implemented; HowlPlane verified with the plan's command (12 tests); Claude reviewed CLEAN (substantive: requirement-by-requirement and README-claim check, 4 non-blocking notes) and accepted. Wall clock 09:42-09:49.
Design chosen by the workflow: Python stdlib CLI household.py + SQLite (./household.sqlite3, --db), documentation/plan.md.
Independent user verification (copy of the tree, databases removed, README commands verbatim):
- README example: add --due, list --on, complete --on, history, due --on 01-07 (header only) and 01-08 (due): exactly as documented.
- Default DB; complete today -> next due +3, status due -> scheduled; history; persistence across processes.
- Invalid input: bad name/--every/date (`20261004`)/command -> usage rc 2; interval > 36500, unknown id, future or non-increasing completion, missing dir, directory as DB, non-database file -> `error:` rc 1, no traceback.
- python3 -m unittest discover -s tests -t .: 12 OK.
0 internal-state edits, 0 bypasses, 0 manual completion.
