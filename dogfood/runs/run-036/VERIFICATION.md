# run-036 verification — BASELINE FIXTURE CLEAN RUN 1/2 (engine 175b57a)
howl orchestrate exit 0, COMPLETE in 4.5 min, 0 reroutes, 0 rework. Claude planned; Codex implemented; HowlPlane verified with the plan's command (10 tests); Claude reviewed CLEAN and accepted.
Design: package `household`, JSON at --data / $HOUSEHOLD_TASKS_FILE / ~/.household_tasks.json.
Independent verification (copy, isolated HOME): README session verbatim correct (due on add, Nothing due after complete, next due +7); due boundary next-due 10-11: as-of 10-10 none, 10-11 due, 10-13 overdue (2 days); default file and env path honoured; persistence across invocations; errors: empty name/interval 0/future date/bad date/bad command rc 2, unknown id/corrupt file/directory rc 1, corrupt file not overwritten; completion before creation rejected (documented). unittest 10 OK.
No HowlPlane state in the tree (leak scan empty). 0 internal-state edits, 0 bypasses, 0 manual completion.
