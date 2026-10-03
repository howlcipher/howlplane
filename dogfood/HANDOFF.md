# Howl Dogfood Handoff

## Mission
Prove a normal user can ask Howl for a household recurring-task CLI app (add, list, complete with timestamp, due-again detection, local persistence, tests, usage docs) via the public CLI and get a working result. Two consecutive clean runs required.

## Current Status
RUNNING (initialized, run-001 not yet started)

## Current Run
run-001 (pending)

## Current Phase
Preparing the mission target repository and checking agent readiness.

## Last User Command
none

## Last Result
n/a

## Active Finding
NONE

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
Many other agents' worktrees exist; do not modify them. Do not use local LLM providers.
