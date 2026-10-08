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


---

# Addendum 2: resume and second follow-up campaign (2026-10-03)

## Result
DOGFOOD RESULT: PASS. Clean runs **run-014 and run-015**, consecutive, on identical engine source (howlplane `dogfood/DOG-013-review-convergence` @4a5d697), each from a fresh empty repository, same mission, no recovery steps. Verification: runs/run-014, runs/run-015 `VERIFICATION.md`.
Not counted: run-011 (BLOCKED), run-012 (clean, but the engine changed afterwards), run-013 (HANDOFF REQUIRED).

## Findings
Total this campaign: 6 (DOG-012..017). Resolved in code: 6. All pushed; none merged.
| ID | Summary | Commit / PR |
| --- | --- | --- |
| DOG-012 | 300 s review budget killed half of Cursor's real reviews | 9a9a59c / #142 |
| DOG-013 | Review rework could not converge ("CLEAN only if no issue") | 82414bf / #143 |
| DOG-014 | Reviewers not shown HowlPlane's own verification results | 82414bf / #143 |
| DOG-015 | CLEAN review notes invisible to the user | a1d8ce2 / #143 |
| DOG-016 | A fixable acceptance rejection ended the session | 4a5d697 / #143 |
| DOG-017 | `howlplane route` INTERNAL_ERROR on objectives mentioning tests | 27a5080 / #144 |
Also: host fault (not Howl) — ~16 packages missing from ~/.local site-packages broke the pre-push hook; restored with pip.

## What the runs proved live
- DOG-011's rework loop fired for the first time (run-011) and works mechanically; with DOG-013 it converges.
- A 307 s review completed under the new budget (run-011).
- Reviewers cite the harness evidence (run-013); CLEAN notes are printed (runs 013-015).

## Remaining recommended work
1. Merge #142, then #143 (stacked), and #144; until then the installed engine (editable install of the howlplane checkout) runs whatever branch is checked out. The checkout was returned to `main` at the end of this campaign.
2. Claude remains "interactive only" from an earlier real implementation denial; `howl agents doctor --live --repo <path>` re-verifies it. All runs used Codex/Cursor.
3. Run-to-run variance of the generated app is large (Python package vs single file; SQLite location; exit-code schemes). All met the mission; acceptable, but worth knowing.

## Reproduction
Same as above, with the howlplane checkout at 4a5d697 (or main after #142 and #143 merge):
`git init` an empty repo with one commit; `howl factory prepare --repo . --yes`; `howl orchestrate "<runs/run-001/00-mission.txt>" --repo .`; expect Status COMPLETE in about 10 minutes; run the generated README commands and test script.

# Addendum 3: re-verification after merges (2026-10-04)

## Result
DOGFOOD RESULT: PASS. Clean runs **run-016 and run-017**, consecutive, on howlplane main @a7f3bc4 (includes #142 DOG-012, #146 DOG-013..016, #144 DOG-017), each from a fresh empty repository, same mission, no recovery steps. Verification: runs/run-016, runs/run-017 `VERIFICATION.md`.
run-016 exercised the DOG-011/013 rework loop live (1 of 2 rounds, converged to CLEAN); run-017 passed review on the first round.

## Findings
None new. Non-blocking observation: a delivered working tree may contain gitignored demo databases left by the agent's own checks (run-016 `.demo/`); not reachable by a git clone.

## Repository state
All campaign PRs merged except the records PR #147 (open; merge blocked for the agent by the Claude Code permission guard, left to the user). Live engine checkout on main a7f3bc4.

## Reproduction
Unchanged from Addendum 2, with the howlplane checkout on main (a7f3bc4 or later with identical src).

# Addendum 4: Claude back in the agent pool (2026-10-04)

## Result
DOGFOOD RESULT: PASS. Clean runs **run-024 and run-025**, consecutive, on howlplane `dogfood/DOG-018-greenfield-denial` @7a516c0, fresh empty repositories, same mission, all five agents READY (Claude included), no recovery steps. Verification: runs/run-024, runs/run-025 `VERIFICATION.md`.
Not counted: run-018 (found DOG-018), 019 (BLOCKED), 020 (rerouted), 021 (BLOCKED), 022 (clean, but 023 on the same engine was not), 023 (BLOCKED).

## Findings (campaign 3)
| ID | Summary | Commit |
| --- | --- | --- |
| DOG-018 | Greenfield test-run denial marked Claude interactive-only; implementers could not run their tests | 1ce49aa (grant gap), 2ee6c5b (plan's VERIFY_COMMAND granted; user's choice) |
| DOG-019 | Verification ignored the plan's test command, so reviewers had no test evidence | 245c705 |
| DOG-020 | Planned command ran as `-t ..` (prompt punctuation); refused commands truncated in progress output | f83882d |
| DOG-021 | Rework fixed only cited instances | 22806e8 |
| DOG-023 | Claude-implemented sessions rarely converged under Codex review: AUTO now prefers Codex for implementation (user's choice) | 7a516c0 |
| DOG-022 | COMPLETE report shows superseded FINDINGS verdicts unlabelled | open, nonblocking |
All commits pushed on dogfood/DOG-018-greenfield-denial; PR to be merged by the user.

## What the runs proved live
- Claude's documented recovery (`howl agents doctor --live`) now sticks: it stayed READY through runs 019-025.
- Claude planned and implemented greenfield work with 0 permission denials (runs 021-023), within a bounded grant (plan's test command as an exact literal + read-only git).
- HowlPlane verifies with the plan's command when nothing is discoverable (runs 020-025), and reviewers cite it.
- The PERMISSION line names refused commands and Claude stays READY afterwards (run-020).

## Remaining recommended work
1. Merge the DOG-018 branch PR; until then the installed engine follows whatever the howlplane checkout has (returned to main after this campaign, so main lacks these fixes).
2. DOG-022 (report labels for superseded verdicts).
3. Review-convergence with Claude implementing remains weaker (1/4); revisit "later-round review focus" if Claude should implement by default again.

## Reproduction
As Addendum 2, with the howlplane checkout at 7a516c0 (or main after the PR merges); Claude may be READY.

## Post-merge (2026-10-04)
PR #148 merged as 4e31e53 and #147 as 790eee7; main engine source is identical to 7a516c0, so the Addendum 4 PASS applies to main.

# Addendum 5: post-merge follow-ups (2026-10-04)

## Result
DOGFOOD RESULT: PASS. Clean runs **run-028 and run-029** on howlplane `dogfood/DOG-022-report-superseded-verdicts` @5a806e3 (main f864cb2 + DOG-022 + DOG-025). Not counted: run-026 (clean, but run-027 on the same engine was not), run-027 (BLOCKED, found DOG-025).

## Findings
| ID | Summary | Commit |
| --- | --- | --- |
| DOG-022 | Report showed superseded FINDINGS verdicts as if open | 37bb7b7 |
| DOG-024 | Test suite leaked factory worktrees into the real data home; intermittent pre-push failure | 48aa794 (merged, #149) |
| DOG-025 | Workers re-ran the Howl workflow and copied the session manifest (lease token) into the user's repo; plan was discarded | 5a806e3 |
Housekeeping: 73 pytest-leaked worktrees removed from ~/.local/share/howlplane/worktrees (user approved); 27 real ones kept.

## Remaining recommended work
1. Merge the PR for dogfood/DOG-022-report-superseded-verdicts (carries DOG-022, DOG-025 and the campaign records stranded by #150 merging into #149's branch).
2. Claude-implemented review convergence (DOG-023) is routed around, not solved.

## Post-merge (campaign 4)
PR #151 merged as e74e460; main engine source is identical to 5a806e3, so the Addendum 5 PASS applies to main.

# Adaptive Dogfood Tranche 1 (2026-10-04)

## Result
ADAPTIVE DOGFOOD TRANCHE: PASS

## Engine
SHA: 175b57a (howlplane `dogfood/DOG-027-review-diff-evidence` = main 41a1621 + DOG-026 744bc4a + DOG-027/028 175b57a). PRs #153 (DOG-026) and #154 (DOG-027/028, stacked), not merged.

## Runs
| Run | Engine | Mission | Result |
| --- | --- | --- | --- |
| 030 | 41a1621 | Go, existing: logsum time window + JSON output | CLEAN |
| 031 | 41a1621 | Python, existing: HTTP/SQLite service, 3 symptom-only bugs + automatic DB upgrade | CLEAN |
| 032 | 41a1621 | Node, existing with uncommitted user WIP: config refactor | HANDOFF -> DOG-026 |
| 033 | 744bc4a | 032 rerun, identical start | CLEAN (DOG-026 path not triggered) |
| 032 resume | 744bc4a | `howl orchestrate resume` after engine upgrade | COMPLETE via 1 verification rework round (DOG-026 live) |
| 034 | 175b57a | Python, existing: characterization tests only, source frozen | CLEAN (15/15 mutants killed) |
| 035 | 175b57a | Rust, greenfield: ini2json | CLEAN (19/19 operator oracle cases) |
| 036 | 175b57a | Household baseline fixture | CLEAN 1/2 |
| 037 | 175b57a | Household baseline fixture | CLEAN 2/2 |
| 038 | 175b57a | TARGETED experiment: fsmonitor hook in target repo | 0 HowlPlane-invoked hooks (DOG-028 live) |

## Missions Exercised
Existing-code feature (Go), symptom-driven bug investigation with concurrency and legacy-data upgrade (Python HTTP), refactor and configuration layering on a dirty working tree (Node), test-only characterization with a frozen source (Python), greenfield parser and file transformation (Rust), and the baseline fixture.

## New Capability Coverage
Four languages (Go, Python, JavaScript, Rust); existing-code modification; bug repair; REST/HTTP; concurrency; on-disk data upgrade; refactor; configuration; uncommitted user WIP preserved byte-for-byte (twice); test-only work; file transformation; resume after an engine upgrade. Verification-command discovery worked for go test, npm test, cargo test and plan-named unittest.

## Findings
Total: 3 (DOG-026..028). P0: 0. P1: 2 (DOG-026 workflow blocker; DOG-028 security). P2: 1 (DOG-027). P3: 0. Observation: OBS-030-1, no public cleanup for factory-prepared worktrees (32 dirs, 32 MB).

## Resolved
- DOG-026: a failed verification now drives the bounded rework loop with failure-first evidence; cannot-run and timeout cases are reported, not raised; the report and inspect say what blocked. Verified live by resume.
- DOG-027: review and acceptance receive HowlPlane's own session diff, with the user's pre-existing WIP named and excluded. Verified live (run-034 reviewer scope check).
- DOG-028: HowlPlane git calls disable core.fsmonitor, diff.external and textconv. Verified live (run-038).

## Remaining Known Limitations
- DOG-023 (Claude-implemented convergence) is still routed around by AUTO preferring Codex for implementation; not re-tested this tranche.
- Git hooks (`.git/hooks`, core.hooksPath) still run on HowlPlane's own commits (git_integration); needs a design decision.
- Read-only reviewers still cannot run tests themselves; they rely on harness evidence (by design).
- OBS-030-1 factory worktree cleanup.

## Repositories Changed
howlplane only (engine branches above; campaign records on `dogfood/adaptive-tranche-1`).

## Commits / PRs
744bc4a (DOG-026, PR #153), 175b57a (DOG-027/028, PR #154, stacked). Records: dogfood/adaptive-tranche-1. All pushed; every engine push passed the full pre-push suite (2321, 2327).

## Regression Evidence
Baseline fixture clean twice on the final engine (036, 037); final two adaptive missions clean on the final engine (034, 035); no internal-state edits, bypasses or manual completion in any run.

## What Is Now Proven
On 175b57a, a user can hand Howl realistic requests in four languages, on existing or empty repositories, including symptom-only bug reports, refactors over uncommitted work, and frozen-source test tasks, and get independently verified results through `howl orchestrate`. A failing test after implementation is now recovered by Howl itself.

## What Is NOT Proven
Long or multi-hour tasks; tasks needing network services or external credentials; non-AUTO routing (Claude implementing); providers failing mid-session this tranche; other ecosystem components (HowlDream/Create/Writer/Proof/Relay) in a composed workflow; Windows/macOS hosts.

## Recommended Next Dogfood Frontier
1. Merge #153 and #154, then re-confirm on main (src should equal 175b57a).
2. A multi-step task large enough to hit execution budgets or need decomposition.
3. A provider failure injected mid-implementation (resume and reroute under the new rework paths).
4. An ecosystem handoff the docs actually promise (for example HowlWriter for a documentation-heavy deliverable).
5. Decide the git-hooks policy for HowlPlane's own commits.

# Adaptive Dogfood Tranche 2 (2026-10-05 to 2026-10-07)

## Result
STOPPED at the user's request after run-042. Not PASS: the convergence criterion (final two missions clean on the final engine) was not reached, because the engine changed with each fix and no clean pair followed DOG-030/033. Not BLOCKED: no external blocker.

## Engine
Live runs used local integration branches of howlplane: a911cc9 (main 068e3d7 + #156 175b57a + #157 8e5b2ab) for run-040, then c9ee1c4 (+ DOG-030 540ca27) for runs 041-042. Not pushed (integration only); every component commit is pushed on its own branch.

## Runs
| Run | Mission | Result |
| --- | --- | --- |
| 039 | Python, existing: multi-user, API tokens, auth, isolation, upgrade (large change) | Deliverable correct; NOT CLEAN -> DOG-029 |
| 040 | 039 rerun, identical start | Deliverable correct; DOG-029 verified live; NOT CLEAN -> DOG-030 |
| 041 | TARGETED: Go concurrency + Ctrl-C during implementation + resume | Interruption and resume work; deliverable correct |
| 042 | Ecosystem: `howlplane creative run` (Dream -> Writer -> Create) landing page | COMPLETED with intact lineage; NOT CLEAN -> DOG-031/032/033 |

## Findings
| ID | Sev | Summary | State |
| --- | --- | --- | --- |
| DOG-029 | P1 | Redaction rewrote the user's goal and review notes ("token once" -> "token <redacted>") | Fixed 8e5b2ab, PR #157, verified live (run-040) |
| DOG-030 | P2 | Reviewers shown a redacted diff (text not in the repo) | Fixed 540ca27 on PR #156 |
| DOG-031 | P1 | `howl doctor` HEALTHY while an editable runtime lacked a declared dependency; --fix ignored the install profile and could not find checkouts | Fixed howl 5ee87c1, PR howl#15, verified on the real install |
| DOG-032 | P2 | Creative pipeline not reachable through the official installer | OPEN: product decision (installer-owned vs HowlPlane-owned composition) |
| DOG-033 | P2 | COMPLETED creative run hides copy withheld for factual review | Fixed 2e8113a (branch dogfood/DOG-033-creative-review-visibility) |
Observations: P3 resume hint names `howlplane` while the user typed `howl`; P3 HowlWriter fidelity check over-conservative on "default 8 concurrent checks".

## What Is Now Proven
- Large cross-cutting changes (auth, isolation, migration) finish in one implementation pass and are delivered correctly.
- Ctrl-C mid-implementation checkpoints cleanly (no orphan workers, repo stable), `inspect` shows INTERRUPTED, and `resume` completes correctly.
- The Dream -> Writer -> Create handoff runs end to end with intact provenance, once installed by hand.

## What Is NOT Proven
- A clean pair on the final engine (DOG-030 and DOG-033 fixes are untested live).
- The creative pipeline on a standard (non-developer) install.
- Provider failure mid-session; multi-hour tasks.

## PRs
Merged at the user's request: howlplane #157 (DOG-029), #156 (DOG-027/028/030), howl #15 (DOG-031); #158 (DOG-033) and the records PR merged after CI. All engine commits verified on main by ancestry.

## Recommended Next Steps
1. Run two fresh missions on main (now carrying DOG-026..033) to earn a clean pair.
2. Decide DOG-032 (A: installer owns creative components; B: HowlPlane runs each component's own console script).
3. Decide the git-hooks policy for HowlPlane's own commits (from tranche 1).

## Addendum: Clean pair on main (2026-10-08)

### Result
CLEAN PAIR: run-043 and run-044, consecutive, both on howlplane main bb7ba21 (carries DOG-026..034), public CLI only (`howl factory prepare --yes`, `howl orchestrate`), HOWL_FORBID_LOCAL_INFERENCE=1, normal AUTO routing, no `--verify`. No new finding; no internal state touched; no output hand-fixed.

| Run | Mission | Result |
| --- | --- | --- |
| 043 | Existing Python CLI (pingbot): API token auth, config `=` truncation bug, secret masking, exit code 3 on 401/403; request full of credential wording; uncommitted user WIP present | COMPLETE 5.1 min, review CLEAN first pass; 21 tests, 26/26 independent probes; CLEAN |
| 044 | Existing TypeScript CLI (relnotes): read history from git (range, merges, breaking changes, errors), integration tests on real repos | COMPLETE 8.8 min, review CLEAN first pass; 16 tests, 29/29 independent probes; CLEAN |

### What is proven (on main bb7ba21)
- DOG-029: a goal containing `Authorization: Bearer <token>`, `PINGBOT_API_TOKEN=<your-token>==`, "the token itself" reaches the report verbatim (byte-identical, 0 redaction markers); review notes unaltered.
- DOG-027/030: reviewers judge HowlPlane's unredacted diff: run-043's reviewer quoted token-shaped literals as written and scoped the change by the diff, correctly excluding the user's pre-existing WIP; run-044's reviewer confirmed the untouched test file from the diff.
- Uncommitted user WIP (a modified tracked file the CLI imports plus an untracked file) survives byte-identical (sha256), with nothing committed or stashed and no Howl state left in the repo.
- New coverage: TypeScript (strict tsc, ESM) and an integration task (git as an external system); MISSION_COVERAGE.md now has every capability checked.
- Both deliverables met every requirement, including error paths, boundaries, shell and option injection safety (044), and README accuracy.

### What is NOT proven
- DOG-026's verification-rework path on main: verification passed first time in both runs (it was proven live on its branch in run-032's resume).
- DOG-034's hook guard in a live campaign: orchestrate does not commit; only real-git tests cover it.
- Statistical reliability: two runs, both first-pass CLEAN with the same routing (Claude plan/review/acceptance, Codex implementation). Other pools, provider failures mid-session, multi-hour tasks and the creative pipeline were not exercised.
- Reviewer depth: both reviews took about 25 s and relied on HowlPlane's test evidence ("I did not run anything myself"); my independent probes found nothing they missed, but that is evidence about these two runs only.

