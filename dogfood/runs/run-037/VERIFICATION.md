# run-037 verification — BASELINE FIXTURE CLEAN RUN 2/2 (engine 175b57a)
howl orchestrate exit 0, COMPLETE in 6.3 min, 0 reroutes, 0 rework. Claude planned; Codex implemented; HowlPlane verified with the plan's command (20 tests); Claude reviewed CLEAN and accepted.
Design: package `household_tasks`, `--today` clock override, JSON at --file / $HOUSEHOLD_TASKS_FILE / ~/.local/share/household-tasks/tasks.json.
Independent verification (copy, isolated HOME): README "Try it" session run verbatim: due on 01-04 (every 3 from 01-01), done -> "No tasks.", due again exactly 01-07, backdated done 01-07 -> next 01-10, remove. Default XDG path created. Errors: empty name, interval 0, unknown id, future date, invalid date -> rc 1 (README: rejected input exits 1, file unchanged); bad command -> rc 2 (syntax errors exit 2); corrupt file -> rc 1, not overwritten. unittest 20 OK.
No HowlPlane state in the tree. 0 internal-state edits, 0 bypasses, 0 manual completion.
