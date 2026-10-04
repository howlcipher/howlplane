# run-024 verification — CLEAN RUN 1/2 (engine 7a516c0: DOG-018..023)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, no rework. Claude planned (VERIFY_COMMAND python3 -m unittest discover -s tests); Codex implemented (DOG-023 preference); HowlPlane verified with the plan's command (passed); Claude reviewed CLEAN citing HowlPlane's results; Claude accepted. Wall clock 09:37-09:42.
Design chosen by the workflow: Python stdlib CLI household.py + SQLite (default ./household.sqlite3, --db), DESIGN.md records the choice.
Independent user verification (copy of the tree, databases removed, README commands verbatim):
- README example: add (--due), list --due --on, complete --on (records 2026-01-02, next due 2026-02-01), history, list --due on 01-31 (none) and 02-01 (due): exactly as documented.
- Default DB in cwd; complete today -> next due +7; status flips DUE -> scheduled; history kept; state survives separate processes.
- Invalid input: bad name/--every/dates (incl. `20261004`)/unknown command -> usage rc 2; unknown id, future or non-increasing completion, unopenable DB -> `Error:` rc 1, no traceback.
- python3 -m unittest discover -s tests: 9 OK.
0 internal-state edits, 0 bypasses, 0 manual completion.
