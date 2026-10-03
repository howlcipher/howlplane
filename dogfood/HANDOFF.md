# Howl Dogfood Handoff

## Mission
Prove a normal user can ask Howl for a household recurring-task CLI app (add, list, complete with timestamp, due-again detection, local persistence, tests, usage docs) via the public CLI and get a working result. Two consecutive clean runs required.

## Current Status
RUNNING (run-001 ended HANDOFF REQUIRED; DOG-001 open)

## Current Run
run-001 (started 2026-10-02 21:14 local; evidence in runs/run-001/)

## Current Phase
Repair mode for DOG-001. Session 791d33f0 is resumable at stage acceptance.

## Last User Command
`howl orchestrate "<mission in runs/run-001/00-mission.txt>" --repo /run/media/system/tallgeese/dev/dogfood-missions/run-001/household-tasks` (after `howl factory prepare --repo ... --yes`, `howl agents doctor --repo ...`)

## Last Result
Exit 2, HANDOFF REQUIRED at acceptance; app works (11 tests pass) but acceptance rejection reason is invisible. Logs in runs/run-001/02-orchestrate.{stdout,stderr}; exit code in 02-exit.txt once finished.

## Active Finding
DOG-001 (verdict text discarded)

## Root Cause
n/a

## Current Repair
none

## Latest Commits
none yet

## Repositories Modified
howlplane: branch dogfood/campaign-household-tasks (campaign records only)

## Tests Completed
none

## Remaining Work
1. Create target repo /run/media/system/tallgeese/dev/dogfood-missions/run-001/household-tasks (git init).
2. `howl agents doctor --repo <target>`; `howl factory prepare --repo <target> --yes`.
3. `howl orchestrate "<mission text>" --repo <target> --verify <cmd>`.
4. Independently verify output (Phase 19).

## Resume Instructions
1. cd /run/media/system/tallgeese/dev/howlplane && git checkout dogfood/campaign-household-tasks
2. Read this file, findings/FINDINGS.md, journal.
3. `howl orchestrate inspect --json --repo <target>` to see any live session before starting anything new.

## Warnings
howlplane has a pre-push hook that runs the full suite (`make test lint build docs`, several minutes). Pushes are slow, not hung. Campaign branch commit bc1ad5d push was in flight at 21:15.
Many other agents' worktrees exist; do not modify them. Do not use local LLM providers.
