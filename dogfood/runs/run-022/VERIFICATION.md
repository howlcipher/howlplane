# run-022 verification — CLEAN RUN 1/2 (engine 22806e8: DOG-018..021)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0. Claude planned (VERIFY_COMMAND python3 -m unittest discover -s tests), implemented, and accepted; Codex reviewed. Rework 2 of 2 rounds, converged: round findings were stored-data validation, then print-before-save; the final Codex review was CLEAN (2 non-blocking notes). HowlPlane verified with the plan's command after every implementation (6 validations passed). 0 permission denials.
Design chosen by the workflow: single-file Python stdlib CLI household.py, JSON at --data / $HOUSEHOLD_DATA / ~/.household-tasks.json, atomic writes.
Independent user verification (copy of the tree, isolated HOME and data paths, README commands verbatim):
- README example: add x2 (incl. --start), due, done by name with --on, list, due --on: correct, rc 0.
- Due boundary: every 3, done 2026-10-01 -> due on 2026-10-04 listed, 2026-10-03 "Nothing due".
- done by id today: next due +3; history keeps both dates in the JSON file; state survives separate processes.
- Default location ~/.household-tasks.json created (mode 600); --data overrides.
- Invalid input: empty or all-digit name, duplicate name, unknown id, future or out-of-order date -> `error:` rc 1; bad --every, bad date, unknown command -> usage rc 2 (as the README states).
- Corrupt data `{"tasks": null}` and a directory as data file -> `error: cannot read data file ...` rc 1, file not overwritten (the DOG-021 classes from runs 019/021).
- python3 -m unittest discover -s tests: 18 OK.
0 internal-state edits, 0 bypasses, 0 manual completion.
Report UX note -> DOG-022: the COMPLETE report prints the two superseded FINDINGS verdicts without marking them as resolved by rework.
