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

## Status update (run-013, 2026-10-03)
run-013 HANDOFF REQUIRED -> DOG-016 fixed in 4a5d697 (same branch dogfood/DOG-013-review-convergence; live engine). Streak 0/2. Next: run-014, run-015 on 4a5d697.

## Status update (FINAL, campaign 2, 2026-10-03)
Status: PASS (run-014 + run-015 on 4a5d697). See FINAL_REPORT.md Addendum 2.
Open PRs (not merged, the user's call): howlplane #142 (DOG-012), #143 (DOG-013..016, base #142's branch), #144 (DOG-017).
Repository state: howlplane checkout returned to main (a0ada6a); repair worktrees removed. Campaign records: branch dogfood/resume-check-20261003 (worktree howlplane-dogfood-campaign), pushed.
Resume instructions: nothing pending. To re-verify after merges: two fresh runs per FINAL_REPORT reproduction; record as run-016+.
Active finding: NONE.

## Status update (resume check 2, 2026-10-03)
Status: PASS, valid on main. howlplane main 4c4e69b has the same engine source as 4a5d697 (runs 014/015). The live engine checkout is on main at 4c4e69b.
Open: howlplane PR #144 (DOG-017), unmerged, the user's call. Nothing else pending; no active finding; no unpushed work.
Records branch: dogfood/resume-check-20261003-2 (worktree howlplane-dogfood-campaign).
Resume: if howlplane src changes after 4c4e69b (e.g. #144 merges), run two fresh missions as run-016 and run-017 per FINAL_REPORT.md reproduction before re-asserting PASS.
Warning: `pip check` shows litellm/mcp pin conflicts (jsonschema, pydantic); harmless today, but check it first if the pre-push hook fails.

## Status update (DOG-017 merged, 2026-10-04)
Status: VERIFYING. PR #144 (DOG-017) merged as a7f3bc4 (2026-10-04T01:57Z). Engine src changed vs 4a5d697 (control_plane/cli.py, route task-class inference), so PASS must be re-earned on a7f3bc4.
Next: fast-forward the howlplane checkout to a7f3bc4 (live engine), then run-016 and run-017 from fresh empty repos under dogfood-missions/, per FINAL_REPORT.md reproduction. Clean streak on a7f3bc4: 0/2.

## Status update (run-016, 2026-10-04)
run-016 CLEAN RUN 1/2 on a7f3bc4. run-017 in progress (target dogfood-missions/run-017/household-tasks, same procedure).

## Status update (FINAL, re-verification, 2026-10-04)
Status: PASS on howlplane main a7f3bc4 (run-016 + run-017). See FINAL_REPORT.md Addendum 3.
Open: records PR #147 (branch dogfood/resume-check-20261003-2), the user's to merge. No active finding, no open code PRs.
Live engine checkout: howlplane on main at a7f3bc4.
Resume: nothing pending. If howlplane src changes after a7f3bc4, run two fresh missions (run-018, run-019) per FINAL_REPORT reproduction before re-asserting PASS.

## Status update (DOG-018, 2026-10-04)
PASS for the household mission still stands on main a7f3bc4 (runs 016/017, Codex/Cursor). Exploratory run-018 (Claude READY) found DOG-018.
Live engine checkout: howlplane on branch dogfood/DOG-018-greenfield-denial (e821971) — this IS what `howl orchestrate` runs now.
Next: (1) after the push lands, re-verify Claude (`howl agents doctor --live --agent claude_code --repo <repo>`), run run-019 on a fresh repo and confirm the PERMISSION line plus Claude still READY afterwards; (2) user decision on DOG-018 part 2; (3) PR for the branch; return the checkout to main if the PR is not merged.

## Status update (FINAL, campaign 3, 2026-10-04)
Status: PASS with Claude in the agent pool: run-024 + run-025 on howlplane dogfood/DOG-018-greenfield-denial 7a516c0. See FINAL_REPORT.md Addendum 4.
Open PRs (the user's to merge): howlplane #148 (engine fixes DOG-018..021, 023; branch dogfood/DOG-018-greenfield-denial @7a516c0), howlplane #147 (campaign records; branch dogfood/resume-check-20261003-2).
Live engine: the howlplane checkout was returned to main (a7f3bc4), which does NOT have the #148 fixes. Until #148 merges, a greenfield session with Claude READY will hit DOG-018 again (Claude marked interactive-only; recover with `howl agents doctor --live --agent claude_code --repo <repo>`).
Open finding: DOG-022 (nonblocking report UX).
Resume: after #148 merges, `git -C howlplane pull` on main; src then equals 7a516c0 plus any later merges. If src differs from 7a516c0, run two fresh missions (run-026, run-027) per FINAL_REPORT reproduction before re-asserting PASS.
Warnings: pre-push hook runs the full suite (~11 min; slower while a mission runs); SlopsLint python_tests ceiling is 2 clones, so new tests must share helpers.

Note (2026-10-04): the first push of records commit ad9264c failed the pre-push full suite; the immediate retry passed (2280 passed). The failing test was not captured, so an unidentified flaky test exists; capture the log (git push > log 2>&1) if it recurs.

## Status update (post-merge, 2026-10-04)
Status: PASS, valid on main. PRs #147 (790eee7) and #148 (4e31e53) merged. `git diff 7a516c0 4e31e53 -- src pyproject.toml` is empty, so main's engine is byte-identical to the engine of clean runs 024/025; no new runs needed.
Live engine: howlplane checkout on main 4e31e53. `howl agents doctor`: all five agents READY (Claude included, no recovery needed).
Other repos: howl and howlproof fast-forwarded (upstream changes are docs/Pages/CI only); howlforge, howlcreate, howldream up to date.
Open: DOG-022 (nonblocking report UX). Unidentified flaky pre-push test (see note above).
Resume: nothing pending. If howlplane src changes after 4e31e53, run two fresh missions (run-026, run-027) per FINAL_REPORT reproduction.
DOG-024 (flaky pre-push test identified: test-suite leak into the real factory worktree root) fixed test-only in 48aa794, PR open. Engine source unchanged: PASS on main 4e31e53 still holds. The 73 leaked worktrees in ~/.local/share/howlplane/worktrees (gitdir under /tmp/pytest-of-*) were left for the user to remove.
