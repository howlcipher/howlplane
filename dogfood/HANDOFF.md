# Howl Dogfood Handoff

## Mission
Prove a normal user can ask Howl for a household recurring-task CLI app (add, list, complete with timestamp, due-again detection, local persistence, tests, usage docs) via the public CLI and get a working result. Two consecutive clean runs required.

## Current Status
PASS (two consecutive clean runs: run-005, run-006). See FINAL_REPORT.md

## Current Run
run-006 (finished, clean)

## Current Phase
run-002 started (fresh target dogfood-missions/run-002/household-tasks, same mission) to regress DOG-001. run-001 session 791d33f0 left resumable at acceptance; ignore it.

## Last User Command
`howl orchestrate "<mission in runs/run-001/00-mission.txt>" --repo /run/media/system/tallgeese/dev/dogfood-missions/run-001/household-tasks` (after `howl factory prepare --repo ... --yes`, `howl agents doctor --repo ...`)

## Last Result
Exit 2, HANDOFF REQUIRED at acceptance; app works (11 tests pass) but acceptance rejection reason is invisible. Logs in runs/run-001/02-orchestrate.{stdout,stderr}; exit code in 02-exit.txt once finished.

## Active Finding
NONE. DOG-001, DOG-002, DOG-003 resolved. Do not change howlplane src before run-006 ends.

## Root Cause
n/a

## Current Repair
none

## Latest Commits
howlplane dogfood/DOG-001-persist-verdict-text a3c7d7e (tests only; push in progress, /tmp/claude-1000/push-dog003b.log) on top of 82d3cde DOG-003 fix (first push FAILED slopslint gate, see FINDINGS) ; b07a4e0 DOG-002 and 110091a DOG-001 PUSHED
howlplane dogfood/DOG-001-persist-verdict-text b07a4e0 DOG-002 fix (push in progress, /tmp/claude-1000/push-dog002.log)
howlplane dogfood/DOG-001-persist-verdict-text 110091a (PUSHED)
howlplane dogfood/campaign-household-tasks 8251ad0 (local only)

## Repositories Modified
howlplane: branch dogfood/campaign-household-tasks (campaign records only)

## Tests Completed
none

## Remaining Work
Campaign complete. Only follow-ups: (howlplane PR #138 merged as aeac4b9; howlplane checkout is back on main) see FINAL_REPORT.md recommended work.
(Original plan, now done:)
1. Create target repo /run/media/system/tallgeese/dev/dogfood-missions/run-001/household-tasks (git init).
2. `howl agents doctor --repo <target>`; `howl factory prepare --repo <target> --yes`.
3. `howl orchestrate "<mission text>" --repo <target> --verify <cmd>`.
4. Independently verify output (Phase 19).

## Resume Instructions
Campaign records live in the worktree /run/media/system/tallgeese/dev/howlplane-dogfood-campaign (branch dogfood/campaign-household-tasks). The howlplane checkout (/run/media/system/tallgeese/dev/howlplane) must stay on the repair branch, because the installed engine is an editable install of that working tree (so the checked-out branch IS what `howl orchestrate` runs).
1. cd /run/media/system/tallgeese/dev/howlplane-dogfood-campaign
2. Read this file, findings/FINDINGS.md, journal.
3. `howl orchestrate inspect --json --repo <target>` to see any live session before starting anything new.

## Follow-up campaign in progress
After PASS, the user asked to do all 4 recommended follow-ups. Done in code: DOG-004..007 (howlplane 338478b + docs on branch dogfood/followups-reviewer-and-recovery, push in progress, log /tmp/claude-1000/push-followups.log), DOG-008 (howl dogfood/readme-orchestrate 95b78ac pushed, PR not yet opened). Next: regression runs run-007, run-008 via the public CLI on the new code; then PRs.

## Warnings
howlplane has a pre-push hook that runs the full suite (`make test lint build docs`, several minutes). Pushes are slow, not hung. Campaign branch commit bc1ad5d push was in flight at 21:15.
Many other agents' worktrees exist; do not modify them. Do not use local LLM providers.

## Status after follow-ups (updated)
- Clean-run streak for the NEW code: 0. run-007 (recovered + false-success caught), run-008 (HANDOFF REQUIRED on a real finding) are not clean.
- howlplane branch dogfood/followups-reviewer-and-recovery pushed through ce14f29 (DOG-004..007, DOG-010 corrected). howl branch dogfood/readme-orchestrate pushed (95b78ac).
- Open: DOG-009 (default --verify, design), DOG-011 (no rework loop, design; waiting on the user's choice).
- Resume: read findings DOG-009/DOG-011; if a rework design is chosen, implement on a new branch from dogfood/followups-reviewer-and-recovery, then run two fresh missions.

## Status update (run-009)
- run-009 CLEAN RUN 1/2 on the new code (howlplane dogfood/rework-and-default-verify). run-010 in progress for clean run 2.
- Branch/PR state: howlplane PR #139 (dogfood/followups-reviewer-and-recovery) and howl PR #14 open, unmerged; dogfood/rework-and-default-verify pushed, no PR yet (stacked on #139).

## Status update (run-010)
PASS on the new code: runs 009 and 010 are two consecutive clean runs (engine aedf82f). See FINAL_REPORT.md addendum. Remaining: PR for dogfood/rework-and-default-verify (stacked on #139), merges are the user's. A live firing of the rework loop is still unobserved.

## Status update (resume check, 2026-10-03)
Campaign: PASS, still valid. All PRs merged; howlplane main a0ada6a has the same src as aedf82f (the engine for runs 009/010). Nothing pending, no active finding, no unpushed work.
Optional next work (not required for PASS): (1) observe the DOG-011 rework loop firing live; (2) DOG-012 candidate, Cursor review latency close to the 300 s review budget.
Resume: if howlplane src changes after a0ada6a, run two fresh missions (run-011, run-012) with the reproduction in FINAL_REPORT.md before re-asserting PASS.
Host fault found and repaired (2026-10-03): the first push of this branch failed the pre-push hook at pytest plugin load (`langsmith` -> `ModuleNotFoundError: httpx`, then `pydantic`). About 16 packages had vanished from ~/.local/lib/python3.14/site-packages (not via apt; cause unknown), including pydantic, which howlplane's own pydantic-settings dependency needs. Restored with `python3 -m pip install --user --break-system-packages <pkgs>`; `pip check` shows no missing packages. Not a Howl defect. If a push fails at test collection again, run `python3 -m pip check` first.

## Status update (DOG-012, 2026-10-03)
Status: VERIFYING. Engine change: howlplane dogfood/DOG-012-review-budget 9a9a59c (review budget 300 -> 600 s). Because engine src changed, PASS must be re-earned: two fresh clean runs, run-011 and run-012.
Plan: after the DOG-012 push lands, remove worktree ../howlplane-dog012, then `git -C howlplane checkout dogfood/DOG-012-review-budget` (the live engine is the editable install of howlplane/src). Then in runs/run-011: `howl factory prepare --repo <target> --yes`; `howl orchestrate "$(cat ../run-001/00-mission.txt)" --repo /run/media/system/tallgeese/dev/dogfood-missions/run-011/household-tasks`; verify per Phase 19; repeat as run-012. Watch also for a live firing of the DOG-011 rework loop.
Afterwards return the howlplane checkout to main.

## Status update (run-011, 2026-10-03)
run-011 BLOCKED (not clean). Fixes for DOG-013 and DOG-014: howlplane dogfood/DOG-013-review-convergence 82414bf (stacked on DOG-012 9a9a59c); the howlplane checkout is on that branch (= live engine). Clean streak 0/2.
Next: run-012 from a fresh target (same steps as run-011, engine 82414bf), then run-013. Afterwards open PRs for DOG-012 and DOG-013 and return the checkout to main.

## Status update (run-012, 2026-10-03)
run-012 clean on 82414bf, but DOG-015 changed the engine (a1d8ce2, push in progress). Streak 0/2 on a1d8ce2. Next: run-013, run-014 with the same procedure. Then PRs: DOG-012 branch, DOG-013 branch (stacked); return checkout to main.
