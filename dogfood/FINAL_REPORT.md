# Howl Dogfood Campaign Final Report

## Result
DOGFOOD RESULT: PASS

## User Mission
Design and build a small command-line application for managing recurring household tasks (add, list, complete, record completion time, identify tasks due again, local persistence, automated tests, concise usage docs), starting only from that goal and using the public `howl` CLI. Mission text: runs/run-001/00-mission.txt.

## Clean Runs
- run-005 (verification: runs/run-005/VERIFICATION.md)
- run-006 (verification: runs/run-006/VERIFICATION.md)
Both ran on identical engine source (howlplane 82d3cde src; a3c7d7e only changed tests), each from a fresh empty target repository, same command, no manual steps.

Not counted: run-003 reached COMPLETE and verified fine, but the DOG-003 defect was present and acceptance tolerated it by chance (retracted after run-004 failed). run-001, run-002, run-004 ended HANDOFF REQUIRED.

## Findings
Total: 3. Resolved: 3. Remaining nonblocking: observations below.

| ID | Summary | Fix |
| --- | --- | --- |
| DOG-001 | Review and acceptance verdict text discarded, so users could not see why a session stopped | 110091a |
| DOG-002 | Acceptance step never received the independent audit verdicts, so it rejected working, CLEAN-audited output (runs 001, 002) | b07a4e0 |
| DOG-003 | An exit-0 reviewer with no text was reported as findings and then fed to acceptance as an unresolved audit (run-004) | 82d3cde, a3c7d7e |

## Ecosystem Repositories Changed
Repo: howlplane. Branch: dogfood/DOG-001-persist-verdict-text (from main 83b117c). Commits: 110091a, b07a4e0, 82d3cde, a3c7d7e. Remote: origin (github.com/howlcipher/howlplane), all pushed. Merged: YES, PR #138 merged to main as aeac4b9 on 2026-10-03. No other Howl repository needed changes.
Campaign records: howlplane branch dogfood/campaign-household-tasks (worktree howlplane-dogfood-campaign).

## Major Improvements
- Review and acceptance verdicts persisted (redacted, 4000 chars) and printed in the report.
- Acceptance now judges the real audit evidence, including superseded reviewer findings.
- Provider faults (empty reviewer output) are distinguished from code findings.

## User Experience Improvements
`howl orchestrate` now explains why it stopped at acceptance instead of showing only a failure code.

## Reliability Improvements
Removed two nondeterministic false rejections at acceptance. Genuine reviewer timeouts (run-006) reroute and complete.

## Integration Improvements
None required across repositories; the defects were all in howlplane orchestration.

## Generated Artifacts
artifacts/run-001-household-tasks-at-handoff.tgz, run-003-..., run-005-..., run-006 target: dogfood-missions/run-006/household-tasks (uncommitted in its repo by design; Howl is instructed not to commit).

## Evidence
runs/run-NNN/ (stdout, stderr, exit code, timestamps, manifests for runs 001 and 004), findings/FINDINGS.md, journal/DOGFOOD_JOURNAL.md.

## Remaining Recommended Work
1. (Done) howlplane PR #138 merged.
2. Cursor review returned empty output in 5 of 6 runs and timed out in the sixth. This is a Cursor backend or provider problem, now correctly handled but worth its own investigation.
3. Claude is excluded as planner with EXECUTION_PERMISSION_REQUIRED in unattended mode (seen run-001, recurring "Claude: unattended execution unavailable"). Possibly because this campaign ran inside a Claude Code session; not investigated.
4. `howl/README.md` does not mention `howl orchestrate`, the main user entry point (documentation gap, not filed as a blocking finding).
5. If acceptance rejects, only the session orchestrator can accept, and it is then excluded, so recovery needs a new session. Consider a documented recovery path.
6. The pre-push hook takes about 10 minutes; consider documenting that.

## Reproduction
1. `mkdir ws && cd ws && git init -b main`, add an initial commit.
2. `howl factory prepare --repo . --yes`
3. `howl orchestrate "<mission text from runs/run-001/00-mission.txt>" --repo .`
4. Expect Status COMPLETE; then run the README commands of the generated app and its test script.
Requires howlplane main at aeac4b9 or later; the installed engine is an editable install of the howlplane checkout.


---

# Addendum: follow-up campaign (after the first PASS)

## Result
PASS again, on the changed code. Clean runs: **run-009 and run-010**, consecutive, on identical engine source (howlplane `dogfood/rework-and-default-verify` @aedf82f), each from a fresh empty repository with the same mission and no recovery steps. Verification: runs/run-009 and runs/run-010 `VERIFICATION.md`.

Not counted: run-007 (needed a documented `resume --verify`, and a first takeover design let a rejection be shopped to a second agent: a false success caught live and fixed), run-008 (honest HANDOFF REQUIRED on a real Cursor finding; nothing could act on it yet).

## What was done for the four recommended follow-ups
1. **Cursor empty reviews (DOG-006):** root cause was `--mode plan` printing no text for a review in print mode; review and acceptance now use `--mode ask`. Cursor then produced real findings (run-007, run-008) and a clean review itself (run-010).
2. **Claude excluded as planner (DOG-004, DOG-005):** a read-only-role permission denial no longer poisons the cross-session readiness cache; Claude's read-only roles are told which tools they hold; an unusable explicit orchestrator is a clean error; `agents doctor` names the command that actually clears interactive-only.
3. **No recovery after rejection (DOG-007, DOG-010, DOG-011):** verdict exclusions expire when the repository changes; the rejection report gives next steps; if the orchestrator is disqualified for a non-work reason another agent can accept, but a rejection from any agent is final; real review findings now go back to the implementer (bounded, 2 rounds) instead of being shopped to another reviewer.
4. **howl README (DOG-008):** documents `howl orchestrate`, `agents doctor`, `factory prepare`.
Also DOG-009: without `--verify`, the project's discovered test command is used and reported as derived.

## Findings (follow-up)
DOG-004 to DOG-011. All resolved in code; DOG-011's rework loop is covered by tests but **has not fired in a live run** (reviews in runs 009 and 010 were clean). DOG-012 candidate (not fixed): Cursor's `ask`-mode review takes 213 to over 300 s against a 300 s default budget (timed out in run-009 and was replaced); `--execution-budget review=600` is the operator knob.

## Repositories and PRs (none merged by me)
- howlplane PR #139: `dogfood/followups-reviewer-and-recovery` (DOG-004 to 007, 010).
- howlplane `dogfood/rework-and-default-verify`: DOG-009, DOG-011, stacked on #139 (PR opened against #139's branch).
- howl PR #14: README (DOG-008).
- Campaign records: `dogfood/campaign-household-tasks`.

## Process lessons
- A change that widens who may approve needs a test where the new approver disagrees (DOG-010).
- Run `slopslint check --classify --enforce` before committing tests; it blocked pushes twice.
- The howlplane checkout is the live engine (editable install): its checked-out branch is what `howl orchestrate` runs.
