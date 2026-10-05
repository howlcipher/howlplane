# run-028 verification — CLEAN RUN 1/2 (engine 5a806e3: main + DOG-022 + DOG-025)
howl orchestrate exit 0, Status COMPLETE, Reroutes 0, no rework. Claude planned; Codex implemented in 4m15s (normal range); HowlPlane verified with the plan's command (11 tests); Claude reviewed CLEAN (no request for workflow evidence; traced the README demo) and accepted.
DOG-025 verified: no howl_route/howl_workflow/session-manifest files and no fence token anywhere in the delivered tree.
Design: Python package household_tasks (python3 -m household_tasks), JSON at --data-file / $HOUSEHOLD_TASKS_FILE / ~/.household_tasks.json, atomic writes, --today override.
Independent verification (copy, isolated HOME, demo file in scratch): README demo exact (02-06 not due, 02-07 due 0 days overdue, 02-09 overdue); default file under HOME, real today, done by name, persistence; invalid name/interval/duplicate/unknown id/future date/bad date -> `Error:` rc 1, unknown command rc 2 (as documented); corrupt `{"tasks": null}` reported, not overwritten; directory as file rc 1; IDs not reused after remove; unittest 11 OK.
0 internal-state edits, 0 bypasses, 0 manual completion.
