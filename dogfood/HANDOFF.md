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

## Warnings
howlplane has a pre-push hook that runs the full suite (`make test lint build docs`, several minutes). Pushes are slow, not hung. Campaign branch commit bc1ad5d push was in flight at 21:15.
Many other agents' worktrees exist; do not modify them. Do not use local LLM providers.
