# Findings

## DOG-001 — Review/acceptance verdict text is discarded; user cannot see why a session stopped

Status: RESOLVED (verified run-002: Codex acceptance rejection reason and Cursor review output now readable in report)
Severity: High (false-success and UX; blocks diagnosis of every audit/acceptance stop)
Discovered in run: run-001
Owning component: HowlPlane orchestration
Repository: howlplane (src/howlplane/control_plane/orchestration.py)

### User action
`howl orchestrate "<household mission>" --repo <target>` (see runs/run-001/00-mission.txt)

### Expected
When an independent reviewer reports findings, or the orchestrator rejects acceptance, the report and `inspect` show the reviewer's findings or rejection reason, so the user knows what to fix or resume.

### Actual
Session ended `HANDOFF REQUIRED` at stage acceptance. Cursor review failed `AUDIT_FINDINGS_OR_UNCONFIRMED`, was replaced by AGY (CLEAN), then Codex acceptance failed `ACCEPTANCE_REJECTED_OR_UNCONFIRMED`. The report says only "Independent audit: CLEAN" and lists exclusions. Attempt records keep `output_sha256` of stdout and `error` = first 200 chars of stderr (only the codex banner). No findings or rejection reason exist anywhere the user can read.

### Evidence
runs/run-001/02-orchestrate.{stderr,stdout}, 03-inspect.json, 04-session-manifest-at-handoff.json. Code: orchestration.py ~L1118 (`[:200]` of stderr/error_message, stdout hashed only), ~L1150-1157 (verdict suffix check).

### User impact
User cannot tell whether the app is wrong or the acceptance was spurious. Cursor's findings were silently superseded by a later CLEAN verdict from a different reviewer, so "Independent audit: CLEAN" may hide unresolved findings.

### Root cause
Hard-failed review/acceptance attempts persist no verdict text. (Sub-question, DOG-002 candidate: should a reviewer's findings be overridden by another reviewer's CLEAN without reconciliation?)

### Resolution
Review and acceptance attempts now store a redacted `verdict_excerpt` (last 4000 chars of stdout). Report prints it for AUDIT_FINDINGS_OR_UNCONFIRMED and ACCEPTANCE_REJECTED_OR_UNCONFIRMED attempts. Docs: documentation/ORCHESTRATE.md.

Not yet addressed (DOG-002 candidate): a reviewer's findings are treated as a worker failure and a different reviewer's CLEAN supersedes them. Decide after seeing real verdict text.

### Tests
New parametrized test_non_clean_verdicts_are_visible_in_report; tests/test_orchestration.py 21 passed; `-k 'orchestrat or closed_loop or handoff or factory'` 549 passed.

### Commit
Repository: howlplane
Branch: dogfood/DOG-001-persist-verdict-text (from main 83b117c)
SHA: 110091a
Remote: origin
Push status: PUSHED to origin (full pre-push suite passed)

### Regression verification
run-002 report printed `Verdict from Codex (acceptance, ...)` with the full rejection text.

### Notes
Generated app itself (independently checked after handoff): `python3 -m household_tasks --help` works; `bash scripts/test.sh` passes 11 tests incl. cross-process persistence. Files are uncommitted in the target working tree (README.md modified, others untracked); the session did not commit them.


## DOG-002 — Acceptance worker is never given the independent audit verdicts

Status: RESOLVED (verified run-003: Codex acceptance ACCEPTED, session COMPLETE)
Severity: High (workflow blocker; false rejection of working output; hits every run so far)
Discovered in run: run-001 (reason visible only after DOG-001 fix, confirmed run-002)
Owning component: HowlPlane orchestration
Repository: howlplane (orchestration.py `execute_assignment`)

### User action
`howl orchestrate "<household mission>" --repo <target>` (run-002)

### Expected
Working app + CLEAN independent audit (AGY) leads to acceptance, COMPLETE.

### Actual
Codex acceptance: "The application works, but acceptance is blocked by missing independent-audit evidence ... no independent findings or audit verdict." Verdict ACCEPTANCE_STATUS: REJECTED. Session HANDOFF REQUIRED; codex then excluded from acceptance and the other agents are "not the session orchestrator", so there is no recovery path without a new session.

### Evidence
runs/run-002/02-orchestrate.stdout (Verdict from Codex block), runs/run-002 manifest in ~/.local/state/howlplane/orchestrate/599376b8*.json.

### Root cause
The acceptance prompt says "inspect implementation, tests, and independent audit" but supplies none of it; reviewer verdicts live only in the manifest.

### Resolution
New `audit_evidence_for_acceptance()` appends every recorded review verdict (including superseded NOT CLEAN ones) to the acceptance prompt. Test: test_acceptance_prompt_includes_independent_audit_verdicts. Docs: ORCHESTRATE.md.

### Commit
Repository: howlplane; Branch: dogfood/DOG-001-persist-verdict-text; SHA: b07a4e0; Push status: PUSHED (origin/dogfood/DOG-001-persist-verdict-text, 110091a..b07a4e0)

### Regression verification
run-003 exit 0, Status COMPLETE, acceptance completed in ~50s.

### Notes
This also closes the earlier concern that a later CLEAN review hides an earlier reviewer's findings: acceptance now sees both.

## DOG-003 — Empty reviewer output is reported as "findings"

Status: RESOLVED (verified run-005: Cursor empty review classified AUDIT_NO_VERDICT, session COMPLETE)
Severity: High after run-004 (escalated from Medium: blocked completion)
Discovered in run: run-002
Owning component: HowlPlane orchestration
Repository: howlplane

### Actual
Cursor review exited 0 with empty stdout (verdict_excerpt blank, ~4 minutes). HowlPlane recorded `AUDIT_FINDINGS_OR_UNCONFIRMED` and printed "FAIL ... review failed", which reads as defects found. Also the Claude planner is skipped with EXECUTION_PERMISSION_REQUIRED in run-001 ("unattended execution unavailable").

### Expected
A distinct, explicit failure such as "reviewer returned no verdict", so the user knows it is a provider problem, not code findings.

### Notes
Fix candidate: classify empty/verdictless review stdout separately (touches the failure enum at orchestration.py ~L65 and its tests).


### DOG-003 update (run-004)
Run-003 hit the same empty Cursor review but Codex acceptance tolerated it, so run-003 was clean by luck. Run-004 (same code b07a4e0): Cursor empty review, AGY CLEAN, then Codex acceptance REJECTED: "Cursor's independent audit returned no text and is marked unconfirmed". Cause: DOG-002 now hands acceptance every review attempt, and the empty one was labelled NOT CLEAN (AUDIT_FINDINGS_OR_UNCONFIRMED). Evidence: runs/run-004/.
Resolution: empty exit-0 review stdout is `AUDIT_NO_VERDICT` (in ROLE_FAILURES, so the reviewer is excluded from the review role); acceptance evidence labels it "NO VERDICT (provider returned no text; not a finding)". Tests: test_empty_review_is_no_verdict_not_findings, test_acceptance_evidence_marks_no_verdict_reviewer_as_provider_fault; subset 552 passed.
Commit: howlplane dogfood/DOG-001-persist-verdict-text 82d3cde. Push: in progress.
Regression: run-005 (pending).
Open question for later (not fixed): Cursor reviewer returned empty output in 4 of 4 runs after 3-4 minutes. That is a Cursor backend/provider problem worth its own finding if it persists.

### DOG-003 follow-up: pre-push gate failure (self-inflicted, fixed)
First push of 82d3cde failed the repo pre-push suite: `slopslint check --enforce` reported python_tests had 3 duplicate clones (ceiling 2) because my two new tests shared near-identical worker stubs. Fixed in a3c7d7e by sharing a `scripted_execute` helper (clones back to 2; tests only). Lesson recorded: run `slopslint check --classify --enforce` before pushing test changes.


# Follow-up campaign (after PASS and PR #138 merge): findings DOG-004 to DOG-007

Branch howlplane dogfood/followups-reviewer-and-recovery (from merged main aeac4b9); howl dogfood/readme-orchestrate.

## DOG-004 — A read-only-role permission denial marks Claude interactive-only across sessions
Status: RESOLVED — merged (#139), proven by clean runs 009/010; Claude recovery via `howl agents doctor --live` re-verified 2026-10-04. (Previously: FIX COMMITTED (338478b), awaiting regression run. Severity: High (silently removes an agent; poisons the cross-session cache).)
Evidence: run-001 planning by Claude was denied `cat README.md; howl --help | head`; HowlPlane then wrote unattended=false to the readiness cache. Later `howl agents doctor` showed Claude "NEEDS ACTION interactive only" and `howl orchestrate --orchestrator claude_code` ended in INTERNAL_ERROR (ValueError traceback, "likely a HowlPlane bug").
Root cause: the planning profile has no Bash by design, the model tried a shell, and the denial was recorded as evidence about unattended *mutation*. Also an unusable explicit orchestrator raised a bare ValueError.
Fix: denial scopes to mutating roles only (implementation/remediation); Claude read-only roles are told to use Read/Grep/Glob; explicit unavailable orchestrator raises OperatorFailure ORCHESTRATOR_UNAVAILABLE with the recovery command. Three tests that encoded the old contract were updated (observed-incident fixtures now reserve Claude; planning denial leaves capability unknown) and two tests pin the new contract (planning/review do not, implementation does mark interactive-only).
Live check: after clearing the stale cache, a Claude PLAN ONLY session completed in 15 s.

## DOG-005 — `agents doctor` advice for interactive-only does not recover it
Status: RESOLVED — merged (#139); documented recovery followed successfully 2026-10-04 (Claude NEEDS ACTION -> READY). (Previously: FIX COMMITTED (338478b). Severity: Medium.)
Evidence: doctor's Next says `agents doctor --refresh`, and neither `--refresh` nor `--live` (no repo) cleared "interactive only" even with a passing smoke, because a session verdict outranks a smoke. Only `--live --repo <path>` (workspace smoke) cleared it.
Fix: doctor now names that command for interactive-only workers; docs explain the precedence.

## DOG-006 — Cursor reviews return no text (root cause of repeated AUDIT_NO_VERDICT)
Status: RESOLVED — merged (#139), proven by clean runs 009/010 (Cursor reviews return text). (Previously: FIX COMMITTED (338478b), awaiting regression run. Severity: High (reviewer wasted ~4 min per run, 5 of 6 runs).)
Evidence (run outside HowlPlane, same review prompt, run-006 target): `agent -p --mode plan --output-format text` rc 0 after 4m08s, stdout 1 byte; `--mode plan --output-format json` timed out at 290 s with no output; `--mode ask` rc 0 in 213 s with a full review, including a legitimate finding (foreign-key integrity unenforced) and `AUDIT_STATUS: FINDINGS`. The planning role in plan mode works (19 s, real output).
Fix: Cursor review and acceptance use `--mode ask`; planning keeps `--mode plan`. Note: with real reviews Cursor will now sometimes report genuine findings, which route through the normal findings path.

## DOG-007 — No recovery after an acceptance rejection
Status: RESOLVED — merged (#139), proven by clean runs 009/010. (Previously: FIX COMMITTED (338478b). Severity: High.)
Evidence: `howl orchestrate resume` on run-001's rejected session re-printed the same HANDOFF with no worker and no guidance. The rejecting orchestrator stays excluded for acceptance for the session even after the repository changes.
Fix: verdict-based review/acceptance exclusions (AUDIT_FINDINGS_OR_UNCONFIRMED, AUDIT_NO_VERDICT, ACCEPTANCE_REJECTED_OR_UNCONFIRMED) expire when the repository changes; stale audit/acceptance results are cleared; the rejection report prints exact next steps (repair then `resume --verify`, or `discard`). Test: test_rejected_acceptance_recovers_after_the_repository_is_repaired.
Not done: automatic rework (feeding the rejection back to the implementer). That is a larger design change, recorded as a recommendation.

## DOG-008 — howl README omits `howl orchestrate`
Status: RESOLVED — howl PR #14 merged (77482cb). (Previously: FIX PUSHED (howl 95b78ac on dogfood/readme-orchestrate; PR pending). Severity: Medium (docs).)
Fix: README section on agents doctor / factory prepare / orchestrate with a first-run example, consistent with docs/SCOPE.md.

## DOG-009 — A reroute after a partly written attempt demands a --verify command the user cannot know yet
Status: RESOLVED — derived default verification merged (#140); every later run prints "Verification command: ... (derived ...)" (runs 009-017). (Previously: OPEN (observed run-007; recovery followed as documented; not yet repaired). Severity: Medium (workflow needs an extra manual step, but the handoff states it).)
Evidence: run-007. Claude (now a working planner thanks to DOG-004) planned, then its implementation wrote files and was denied a shell command (EXECUTION_PERMISSION_REQUIRED; it also tried `cd` into the howlplane repo, outside the workspace). Codex continued from the partial files and completed. The session still ended HANDOFF REQUIRED: "Partial or recovered changes require an explicit --verify command", because a failed attempt that changed the repo sets needs_validation (existing safeguard).
Impact: the mission goal contains no verification command and the app does not exist yet, so a user cannot supply one up front. After the fact it is discoverable (scripts/test.sh).
Recovery used (documented): `howl orchestrate resume --repo <target> --verify bash scripts/test.sh` (runs/run-007/05-*).
Fix candidates (design decision, not made): derive a default verification from the discovered project (ProjectAdapter already does this for Claude's allowlist), or let the planner propose it.
Note: this was always possible; earlier runs never hit it because no attempt failed after writing files.

## DOG-010 — Acceptance deadlocks when the orchestrator is disqualified (and my first fix created a false success)
Status: RESOLVED — merged (#139), proven by clean runs 009/010. (Previously: FIX COMMITTED (see HANDOFF for SHAs), awaiting a fresh clean run. Severity: High.)
Evidence, run-007 (runs/run-007/): Claude, now a working planner (DOG-004), became session orchestrator; its implementation was denied permission and it was marked interactive-only; only the orchestrator may accept, so the first pass ended HANDOFF REQUIRED with every agent "not the session orchestrator".
First fix (commit "let acceptance move off a disqualified orchestrator") let the next eligible agent take over. Live use of it on run-007 then produced a FALSE SUCCESS: Codex took over and REJECTED (genuine concerns: workflow evidence not shown, concurrent writers can overwrite, future completion dates), the session rerouted, Cursor ACCEPTED, and the session finished COMPLETE WITH WARNINGS. My "judged" guard only covered the original orchestrator. Run-007 is therefore NOT a clean run.
Correct fix: takeover is allowed only for non-verdict disqualification; a verdict (ACCEPTANCE_REJECTED_OR_UNCONFIRMED and the other verdict failures) from ANY agent makes acceptance final until the repository changes. Tests: takeover completes; rejection by the taker is final (fails against the flawed logic, verified); rejection by the original orchestrator is final.
Lesson: a fix that widens who may approve needs a test where the new approver disagrees. The live regression caught what the unit tests missed.

## DOG-011 — Real review findings cannot be acted on: `orchestrate` has no rework step
Status: RESOLVED — bounded rework loop merged (#140); fired live in run-011 and run-016, converges since DOG-013. (Previously: OPEN, needs a design decision (options presented to the user). Severity: High (blocks unattended completion whenever a reviewer finds a real issue).)
Evidence, run-008 (fresh run, corrected code): Codex planned and implemented. Cursor (now in `--mode ask`, DOG-006) returned a genuine review with a real Major finding (README claims JSON errors for argparse failures; false) and three Minor ones. The session rerouted to AGY (CLEAN), then Codex acceptance rejected, correctly and without false success. Session ended HANDOFF REQUIRED with exact next steps (DOG-007), but nothing in the ecosystem fixes the finding.
Why it surfaced now: before DOG-006 the Cursor review was always empty, so findings never existed; after it they are real, and before DOG-002 acceptance ignored them.
Behavior today: findings route to "another reviewer"; the implementer is never asked to fix them. The only recovery is a human editing the repository, then `resume --verify`.
Options (see the question put to the user): bounded rework loop (feed findings to the implementer, re-review, cap N rounds); advisory findings with acceptance deciding; or keep the human-in-the-loop handoff.
Run-008 is not a clean run (HANDOFF REQUIRED). Streak: 0.

## Follow-up status (after DOG-009/011 implementation)
DOG-009 and DOG-011 implemented on howlplane dogfood/rework-and-default-verify (user chose: bounded rework loop, project-derived verify). Run-009 verified DOG-009 live (derived verify ran and passed). DOG-011's loop is covered by tests but has not fired in a live run yet (run-009's reviewer returned CLEAN).
Observation (not fixed, DOG-012 candidate): Cursor review in --mode ask takes 213-300+ s (runs 006, 008, 009), right at the 300 s default review budget, so it times out in some runs and is replaced. Raising the review budget or using a faster Cursor model is an operator choice (`--execution-budget review=600`); no code change made.

## DOG-012 — Default review budget cuts off half of Cursor's real reviews

Status: RESOLVED — merged (#142), proven by clean runs 014-017. (Previously: FIX COMMITTED (howlplane dogfood/DOG-012-review-budget 9a9a59c), awaiting regression runs 011 and 012)
Severity: Medium (reliability: the reviewer most likely to find real defects is silently replaced; the session still completes)
Discovered in run: run-006, confirmed run-009
Owning component: HowlPlane orchestration (`DEFAULT_EXECUTION_BUDGETS`)
Repository: howlplane

### User action
`howl orchestrate "<mission>" --repo <target>` with default budgets.

### Expected
A normally sized whole-change review finishes within the default review budget.

### Actual
Review durations from runs 001-010 stderr: Cursor 90-300+ s (198, 221, 300 timeout and 300 timeout since `--mode ask`; 213 s measured directly), AGY 97-241 s. Default review budget was 300 s, so Cursor hit EXECUTION_BUDGET_EXCEEDED in runs 006 and 009 and AGY replaced it. Acceptance took 44-61 s.

### Root cause
Review default (300 s) was set without measurement; implementation was already raised to 600 s on dogfood evidence. A review covers the whole change and cannot be decomposed.

### Resolution
Review default 600 s; planning and acceptance stay 300 s. Docs: documentation/ORCHESTRATE.md. Operator override unchanged (`--execution-budget review=N`).
Options weighed: per-agent Cursor budget (special-cases one vendor, rejected); leave as is (operator knob, but users cannot know to use it); role default 600 (chosen; worst cost is a hung reviewer burning 5 more minutes before failover).

### Tests
tests/test_orchestration_execution_budget.py split: default-budget contract (review 600) and override contract; 15 passed. Orchestration subset (-k orchestrat/budget/review/cursor/agy/handoff/failover/readiness) 467 passed. slopslint enforce OK.

### Commit
Repository: howlplane; Branch: dogfood/DOG-012-review-budget; SHA: 9a9a59c; Push: in progress (see HANDOFF)

## DOG-013 — Review rework cannot converge: any minor note blocks the audit

Status: RESOLVED — merged (#146), proven by clean runs 014-017 (run-016 rework converged). (Previously: FIX COMMITTED (howlplane dogfood/DOG-013-review-convergence 82414bf, stacked on DOG-012), awaiting regression runs)
Severity: High (complete workflow blocker whenever the reviewer is thorough; first live firing of DOG-011's loop ended BLOCKED)
Discovered in run: run-011
Owning component: HowlPlane orchestration (review prompt, `execute_assignment`)
Repository: howlplane

### User action
`howl orchestrate "<mission>" --repo dogfood-missions/run-011/household-tasks`

### Expected
Rework fixes real defects; once none remain the audit is CLEAN and acceptance runs.

### Actual
Exit 2, Status BLOCKED, "AUDIT BLOCKED: review findings remain after 2 rework round(s)". Each round fixed the previous findings (tests 12 -> 14 -> 19), and each new Cursor review raised different, mostly Low or Minor items (exit-code wording, user_version=0 SQLite files, partial stdout on failure) plus one Medium real issue (documentation/demo.log shows an unquoted command that cannot succeed).

### Evidence
runs/run-011/02-orchestrate.{stdout,stderr} (three Cursor verdicts), exit in 02-exit.txt.

### Root cause
Review prompt: "End with AUDIT_STATUS: CLEAN only if you found no issue", plus "falsify correctness". A falsifying reviewer always finds a new note, so with a bounded loop the audit can never become CLEAN.

### Resolution
Reviewers classify findings BLOCKING (incorrect behavior, unmet requirement, required behavior without working tests, failing test, false docs or evidence) or NON-BLOCKING; only BLOCKING yields FINDINGS. Non-blocking notes remain in the verdict text that acceptance already receives (DOG-002), so nothing is hidden. Docs: ORCHESTRATE.md.
Options weighed: more rework rounds (does not converge); let acceptance judge after the cap (the "advisory findings" option the user did not choose earlier); severity contract (chosen).

### Tests
test_review_prompt_carries_harness_verification_and_a_blocking_only_verdict (fails on old code, verified). tests/test_orchestration.py 35 passed; orchestration subset 469 passed; slopslint enforce OK.

## DOG-014 — Reviewers are not shown HowlPlane's own verification results

Status: RESOLVED — merged (#146), proven by clean runs 014-017. (Previously: FIX COMMITTED (same commit 82414bf))
Severity: Medium (a shell-less reviewer reports the test gate as unproven; in run-011 round 1 it was a numbered finding)
Discovered in run: run-011
Owning component: HowlPlane orchestration
### Actual
Every Cursor review (ask mode has no shell) said "the suite was not re-run here … pass status is not established", although HowlPlane had just run `bash scripts/test.sh` (exit 0) itself.
### Resolution
Review prompt carries the latest harness result per check (`git diff --check`, verification command) with exit code and output tail. `doc["tests"]` is cleared when the repository changes outside the session, so the evidence is for the current tree.

## Observation (not filed): implementer's `howlplane route` attempt failed
Cursor noted `.howl_state/.../HP-20261003-185406-bced.txt`: Codex tried `howlplane route` with task_class 'test' and it was rejected as invalid. The mission asks for approach selection "through the normal Howl workflow", and the orchestrate session already is that workflow. Low priority; recheck if it recurs.

## DOG-015 — CLEAN review notes are invisible to the user
Status: RESOLVED — merged (#146), proven by clean runs 014-017. (Previously: FIX COMMITTED (howlplane dogfood/DOG-013-review-convergence a1d8ce2))
Severity: Medium (false-success defense: after DOG-013 a reviewer may mark items NON-BLOCKING; the user must be able to see them)
Discovered in run: run-012
### Actual
run-012 report showed only "Independent audit: CLEAN". The report printed verdicts only for non-clean attempts, and the session manifest is removed when a session completes (unless `--retain-report`), so the CLEAN reviewer's text was gone.
### Resolution
Report prints `Review notes from <agent> (review, CLEAN):` with the stored excerpt. Test: test_clean_review_notes_are_shown_in_the_report (failed before the fix). 327 related tests passed; slopslint OK.

## DOG-016 — A fixable acceptance rejection ends the session; nothing acts on it
Status: RESOLVED — merged (#146), proven by clean runs 014-017. (Previously: FIX COMMITTED (howlplane dogfood/DOG-013-review-convergence 4a5d697))
Severity: High (workflow blocker; acceptance judged the app functionally complete)
Discovered in run: run-013
Owning component: HowlPlane orchestration (acceptance stage)
### Actual
Codex acceptance: "The application meets the functional goal, but required workflow evidence is incomplete … blocked by missing validated Test Impact Assessment evidence", citing the user's global rules (howlplane/.agents/prompts/ship_check.md, reachable by every agent via the global install). Session HANDOFF REQUIRED; Codex excluded; others "not the session orchestrator".
### Root cause
Review findings got bounded rework (DOG-011) but acceptance rejections did not, so any requirement the acceptor applies that the implementer missed is terminal. The rule itself is legitimate user policy, so suppressing it would be wrong.
### Resolution
A reasoned rejection goes back to implementation on the shared 2-round budget; the same orchestrator re-judges with its earlier reasons in its prompt; never passed to another acceptor; after the cap the existing handoff path applies. Tests: two new (rework then accept; cap then handoff), DOG-010 test now asserts "only the same agent ever accepts" (was "exactly one call"). Orchestration subset 513 passed; slopslint OK. Docs: ORCHESTRATE.md.
Options weighed: tell the implementer about every global rule (unbounded, environment-specific); suppress global rules for acceptance (overrides user policy); rework the rejection (chosen; general, consistent with DOG-011).

## DOG-017 — `howlplane route` crashes on any objective that mentions tests
Status: RESOLVED — merged (#144, a7f3bc4); engine re-proven by clean runs 016/017. (Previously: FIX PUSHED (howlplane dogfood/DOG-017-route-task-class 27a5080, PR #144); separate from the PASS engine)
Severity: Medium (public CLI INTERNAL_ERROR on ordinary input; agents reworded objectives to work around it)
Discovered in run: run-011 (Cursor note), confirmed run-015 (.howl_state/howlplane/diagnostics/HP-20261003-200408-6096.txt in the target)
Owning component: HowlPlane CLI (`infer_task_metadata` in cli.py)
### User action
`howlplane route "Build a CLI with automated tests" --repo <repo> --json`
### Actual
`INTERNAL_ERROR … task_class 'test' invalid … likely a HowlPlane bug`.
### Root cause
Inference emitted "test" and "documentation"; TaskSpec accepts "test_improvement" and "docs". A unit test pinned "documentation".
### Resolution
Inference uses the valid names; contract test checks every inferred class validates (3 cases fail before the fix). Public CLI after the fix: SELECTED, class test_improvement. Related tests 257 passed; full pre-push suite passed.

## DOG-018 — Claude is marked interactive-only after a greenfield test-run denial, right after the doctor called it READY

Status: RESOLVED — merged to main via PR #148 (4e31e53); proven by clean runs 024/025. (Previously: FIXED, pushed (howlplane dogfood/DOG-018-greenfield-denial: part 1 1ce49aa, part 2 2ee6c5b per the user's choice "grant planned command"; prompt delimiting follow-up DOG-020). Verified live: run-019 (Claude planned and implemented 3 rounds, 0 denials) and run-020 (PERMISSION line, Claude still READY afterwards without --live).)
Severity: High (silently removes Claude from every later session; the documented recovery loops: `doctor --live` -> READY -> next greenfield session -> interactive-only).
Discovered in run: run-018
Owning component: HowlPlane orchestrate (capability evidence) + execution profile (grant derivation)
Repository: howlplane

### User action
`howl agents doctor --live --agent claude_code --repo <repo>` (READY), then `howl orchestrate "<mission>" --repo <fresh empty repo>`.

### Expected
Claude, reported READY with "Edits allowed: YES", implements unattended, or HowlPlane explains precisely why it cannot.

### Actual
`[22:57:45] FAIL Claude implementation failed: EXECUTION_PERMISSION_REQUIRED`, reroute to Codex, session COMPLETE WITH WARNINGS. Afterwards `howl agents doctor` shows Claude NEEDS ACTION "interactive only" again. The report does not say which tool was denied.

### Evidence
runs/run-018/02-orchestrate.{stdout,stderr}; agent_readiness.json `unattended: false, source: orchestrate session, 02:57:45Z`.
Rendered profile for a greenfield repo, implementation: `--allowedTools Read Glob Grep Edit Write Bash(git status|diff|log|show:*) --permission-mode acceptEdits` (no project command). For the populated repo after implementation it adds `Bash(bash scripts/test.sh)`.
Direct reproduction with that profile: Claude wrote add.py and test_add.py, then `permission_denials` = two `python3 -m unittest` attempts; exit 0, is_error false.

### User impact
Claude can never complete greenfield implementation, and each attempt also poisons its cross-session readiness.

### Root cause
The execution profile derives Bash grants from discovered project commands; a new repository has none until the implementer writes them, so the implementer cannot run its own tests. The orchestrator treated that denial (with edits already made) as proof the agent cannot run unattended.

### Resolution
Part 1 (e821971): a mutating-role denial after a repository delta whose only refused tool is a named Bash command is a grant gap: role excluded for this session, no interactive-only verdict (session or readiness cache), a PERMISSION progress line names the commands and points to `extra_allowed_bash`. Denials without edits are unchanged. Docs: ORCHESTRATE.md, AI_RESOURCE_POOL.md.
Part 2 (open, user decision): whether to grant greenfield implementers a bounded test-runner set so Claude can finish the work instead of being rerouted.

### Tests
New: tests/test_orchestration_capability_recovery.py (grant-gap classification table, record_failure contract, full-session reroute keeps readiness, no-edit denial still marks interactive-only). 7 fail on the old source; 33/33 pass with the fix. Subsystem: 318 passed (orchestration, readiness, agent execution, workspace trust, cursor). Full suite: pre-push hook.

### Commit
Repository: howlplane
Branch: dogfood/DOG-018-greenfield-denial
SHA: e821971
Remote: origin
Push status: PUSHED (amended to 1ce49aa after a SlopsLint duplication failure in the first pre-push run)

### Regression verification
Pending: a public-CLI run with Claude READY on a fresh repo should show the PERMISSION line and leave Claude READY afterwards.

## DOG-019 — Harness verification ignores the plan's test command, so reviewers get no test evidence in new repositories

Status: RESOLVED — merged to main via PR #148 (4e31e53); proven by clean runs 024/025. (Previously: FIXED, pushed (howlplane dogfood/DOG-018-greenfield-denial 245c705). Verified live in run-020: "Verification command: python3 -m unittest discover -s tests -t . (named by the plan (VERIFY_COMMAND))", and the reviewer cited HowlPlane's 14 passing tests.)
Severity: Medium-High (reviews must re-derive test results; sandboxed reviewers cannot, so findings pile up and rework rounds run out)
Discovered in run: run-019
Owning component: HowlPlane orchestrate (default verification, DOG-009)
Repository: howlplane

### User action
`howl orchestrate "<mission>" --repo <fresh repo>` (no --verify).

### Expected
HowlPlane runs the project's tests itself and shows the result to reviewers (DOG-014), as in runs 009-017 ("Verification command: bash scripts/test.sh (derived ...)").

### Actual
No "Verification command" line; `tests` holds only `git diff --check` x3. The generated app used `python3 -m unittest discover -s tests` with no scripts/test.sh, so nothing was derivable, although the plan had named exactly that command (planned_verify_command). Codex review: "9 errored during setup because the sandbox prohibits temporary-file creation. Persistence remains unverified."

### Root cause
`derive_verify_command` only uses discovered project commands; the plan's VERIFY_COMMAND (added for DOG-018) is used for the implementer's grant but not as a verification fallback.

### Resolution / Tests / Commit
Without --verify and without a discovered command, verify with the plan's VERIFY_COMMAND (deny floor applied) and report its source; discovered commands take precedence. Tests: tests/test_orchestration.py::test_plan_test_command_verifies_when_nothing_is_discoverable (3 cases; DOG-009 test folded in). Commit 245c705, branch dogfood/DOG-018-greenfield-denial, pushed.


## DOG-020 — Planned test command ran as `-t ..`; refused commands cut off in the progress line

Status: RESOLVED — merged to main via PR #148 (4e31e53); proven by clean runs 024/025. (Previously: FIXED (howlplane f83882d, pushed). Verified live: runs 021-023 had 0 permission denials with Claude implementing.)
Severity: High for Claude implementation (defeats DOG-018 part 2), Low for the truncation (UX)
Discovered in run: run-020
Owning component: HowlPlane orchestrate (implementer prompt, progress output). Introduced by the DOG-018 part 2 commit 2ee6c5b.
Repository: howlplane

### Actual
Run-020: Claude implementation refused, rerouted to Codex (session COMPLETE WITH WARNINGS). Progress: `PERMISSION Claude changed files but was refused commands HowlPlane did not grant: git status --short; ls -la; ls tests ..; python3 -m unittest discover -s tests -t ... Ex…` (cut at 160 chars).

### Root cause
Prompt "The plan's test command is: python3 -m unittest discover -s tests -t .. Make it pass": the command's `.` and the sentence's full stop merged; Claude ran `-t ..` (exact reproduction with HowlPlane's prompt builder and the real CLI: denied_commands ["python3 -m unittest discover -s tests -t .."], Claude's report asked the user to run `-t ..`). Claude also chained read-only probes. Progress lines are capped at 160 chars and the commands came after the explanation.

### Resolution
Backtick-delimited command; Claude mutating roles told to inspect with Read/Glob/Grep and run one command per call; one PERMISSION line per refused command, then the explanation. No new grants.

### Tests
Handoff test parametrized with the `-t .` command (fails before the fix); greenfield reroute test asserts the per-command line. 422 subsystem tests passed, SlopsLint at ceiling. Exact reproduction after the fix: success, 0 denials, 14 generated tests pass.

## DOG-021 — Rework fixes only the cited instance, so reviews never converge when Claude implements

Status: RESOLVED — merged to main via PR #148 (4e31e53); proven by clean runs 024/025. (Previously: FIXED (howlplane 22806e8, pushed); prompt-level fix verified in part (run-022 converged on the class fixes, run-023 did not, see DOG-023))
Severity: High (2/2 Claude-implemented runs BLOCKED: run-019, run-021; Codex-implemented runs complete). Since DOG-018 Claude is eligible again and AUTO picks it, so a user with Claude available now gets worse outcomes.
Discovered in run: run-019, confirmed run-021
Owning component: HowlPlane orchestrate (implementation and rework instructions)
Repository: howlplane

### Evidence
run-021 review findings by round: (1) numeric names collide with IDs; README promises status 1 but argparse exits 2; README example sequence fails. (2) `--file .` -> uncaught IsADirectoryError, README promises `error: ...`. (3) `{"tasks": null}` -> TypeError, `{}` task -> KeyError, same README promise. run-019: (1) corrupt records accepted, PermissionError uncaught; (2) README exit code; (3) PermissionError on stat treated as empty store. Each round fixed exactly the cited cases. Rework rounds 12-30 s.

### Root cause
The rework prompt says "Fix every valid finding", which Claude reads literally: each finding is patched as one instance. The underlying claim (all errors produce `error:` + status 1) stays false for other inputs, which the next review finds.

### Resolution
Pending: rework treats each finding as an instance of a defect class (fix the root cause, check the same claim's other inputs and error paths, add tests); implementers check that every documented promise holds.

## DOG-022 — A COMPLETE report shows superseded FINDINGS verdicts as if open

Status: FIXED (37bb7b7, pushed); verified live in run-027 (earlier verdicts labelled, final one not); clean runs 028/029 on 5a806e3
Discovered in run: run-022
Owning component: HowlPlane orchestrate report
### Actual
After 2 converged rework rounds the report prints `Verdict from Codex (review, AUDIT_FINDINGS_OR_UNCONFIRMED): ... blocking finding remains` twice, then `Review notes from Codex (review, CLEAN)`. Nothing says the first two were resolved by later rounds. The acceptance prompt already labels earlier rounds; the report does not.
### Proposed
Label each earlier-round verdict "round N, addressed by rework" (or print only the final verdict plus a one-line round summary).

## DOG-023 — Claude-implemented sessions rarely converge under Codex's falsifying review

Status: RESOLVED — merged to main via PR #148 (4e31e53); proven by clean runs 024/025. (Previously: FIX COMMITTED (howlplane dogfood/DOG-018-greenfield-denial 7a516c0, push in progress), per the user's decision "Prefer Codex implementer"; regression runs run-024/run-025 pending)
Severity: High (1 of 4 Claude-implemented sessions COMPLETE: run-019, 021, 023 BLOCKED; 022 COMPLETE)
Discovered in run: run-023 (pattern across 019, 021, 022, 023)
Owning component: HowlPlane orchestrate (AUTO routing)
Repository: howlplane

### Evidence
run-023 findings by round: unvalidated stored field values -> `{"tasks": {}}` accepted -> `date.fromisoformat` accepting `20261004`/`2026-W40-7`. Each round fixed the cited class; the next review found a narrower instance. Codex->Cursor sessions: 6/6 COMPLETE (009, 010, 014-017). Implementer and reviewer pairing are confounded.

### Options presented to the user
Later-round review focus; more rework rounds; prefer Codex implementer; accept as-is. Chosen: prefer Codex implementer.

### Resolution
`candidates()`: for mutating roles, when requested_orchestrator is AUTO and strategy is not ECONOMY, Codex (IMPLEMENTATION_PREFERENCE) is moved to the front; other agents remain fallbacks; an explicit orchestrator still implements first. Docs: ORCHESTRATE.md.

### Tests
test_auto_prefers_codex_for_implementation_only_when_no_orchestrator_was_chosen (5 cases; 2 fail before the fix), test_preferred_implementer_falls_back_when_unavailable; DOG-010 takeover fixture pins the old preference. 429 subsystem tests passed; lint and SlopsLint clean.

## DOG-024 — Test suite writes factory worktrees into the real data home; intermittent pre-push failure

Status: RESOLVED — merged via PR #149 (f864cb2). The 73 leaked worktrees were removed on 2026-10-04 with the user's go-ahead, after checking that each held only target/.git + README.md pointing into /tmp/pytest-of-*; the 27 real worktrees were kept.
Severity: Medium (pollutes the operator's real ~/.local/share/howlplane/worktrees; flaky pre-push gate blocked pushes twice)
Discovered in: records pushes ad9264c (first attempt) and 9976f67 (pre-push gate)
Owning component: howlplane test suite (tests/conftest.py)

### Evidence
`FAILED tests/test_factory_canary_isolation.py::test_explicit_state_dir_wins_in_bounded_run` — `CampaignError: Factory target exists but is not a healthy Git worktree: ~/.local/share/howlplane/worktrees/edae7d.../target` (gitdir points into a deleted /tmp/pytest-of-howlcipher/... repo). 73 of 100 entries in the real worktrees root have a gitdir under /tmp/pytest-of-*.

### Root cause
Tests that prepare a factory campaign without `set_xdg_paths` resolve the real XDG data home; the worktree name is a hash of the temp repo path, so pytest temp-path reuse meets a stale target. The product's refusal to reuse an unhealthy target is intentional.

### Resolution
tests/conftest.py: autouse `_isolate_xdg_homes` (per-test XDG_DATA_HOME/XDG_STATE_HOME) and session guard `_fail_on_factory_worktrees_in_the_real_data_home`. Test-only; engine source unchanged, so the PASS is unaffected.

### Tests
Guard proven: with isolation disabled it fails naming the leaked worktree (that one worktree, created by the check, was removed). Full suite: 2313 passed; real worktree count unchanged.

### Notes
The 73 pre-existing leaked worktrees in ~/.local/share/howlplane/worktrees were left in place; removal is the user's call.

## DOG-025 — Workers re-run the Howl workflow themselves and copy HowlPlane session state into the user's repo

Status: FIXED (5a806e3, pushed); verified live in runs 028/029 (no HowlPlane artifacts in the trees, normal Codex durations 4m15s/4m36s, reviews no longer ask for workflow evidence)
Severity: High (internal session state incl. the lease fence token written into the deliverable; implementation budget overrun -> reroute -> BLOCKED in run-027; reviewers repeatedly flag missing workflow evidence)
Discovered in run: run-027 (symptoms in 019, 021, 022, 023 review notes)
Owning component: HowlPlane orchestrate (worker instructions; plan handoff)

### Evidence
run-027 tree: documentation/howl_route.txt (a `howlplane route` decision: "Selected Agent: Antigravity CLI (agy)") and documentation/howl_workflow.json (the session manifest: planning attempts, "fence": "685aa1e4...", workspace trust policy). Codex implementation: EXECUTION_BUDGET_EXCEEDED at 600 s vs 134-430 s in 25 earlier Codex implementations. Review notes in runs 019/021/022/023: "No Howl workflow evidence ... selection compliance could not be verified".

### Root cause
The goal asks for the approach to be chosen "through the normal Howl workflow", but workers are never told they are running inside that workflow, and the planning stage's output (the approach decision) is discarded: implementation and review never see it. Workers therefore try to reproduce the workflow themselves, reaching into HowlPlane's CLI and state.

### Resolution
Pending: store the plan; give it to implementation and review/acceptance as the workflow's decision record; tell every role it runs inside a HowlPlane session and must not invoke howl/howlplane or read HowlPlane state.

## DOG-026 — A failed verification ends the session instead of going back to the implementer; the failing output is cut off
Status: RESOLVED (verified live)
Severity: P1 (workflow blocker with no actionable evidence)
Discovered in: run-032 (adaptive tranche 1, Mission C, existing Node.js project invoicegen)
Owning component: HowlPlane orchestration (`src/howlplane/control_plane/orchestration.py`)
Repository: howlplane
### User action
`howl orchestrate "<config refactor request>" --repo dogfood-missions/run-032/invoicegen` (after `howl factory prepare --yes`).
### Expected
When HowlPlane's own verification (`npm test`, derived) fails after implementation, the failure goes back to the implementer like a review finding, within the bounded rework budget; if it still fails, the report says what failed and what to do.
### Actual
Codex finished; `npm test` failed 1 of 59 (`CLI rejects invalid outDir before any output`: file `outDir: 42` masked by `INVOICE_OUT_DIR`). The session stopped at HANDOFF REQUIRED on the first verification attempt with no rework. The report's Tests entry held only the last 1000 characters (TAP summary), not the failing case; `Failures: []`; `Independent audit: not requested or incomplete`. `howl orchestrate inspect` showed only `Stage: implementation`. `resume` would rerun implementation with no mention of the failure.
### Evidence
runs/run-032/stdout.log, stderr.log, exit.txt (2). Code: orchestration.py (41a1621) lines 1584-1592 (`[-1000:]`, immediate handoff); 1315/1383 the same unguarded `subprocess.run` (OSError/TimeoutExpired would crash the session).
### User impact
The user must rerun the tests, find the failure, and fix it or re-run Howl blind, for a defect Howl had already detected. Uncommitted user WIP was preserved (checked by sha256), so no data loss.
### Root cause
Defect class: evidence HowlPlane itself produces is not routed back to the implementer. Rework (DOG-011/013/016) was wired only for review and acceptance verdicts; verification was a terminal gate. Output capture was tail-only, and verification execution errors were unhandled at all four call sites.
### Resolution
Failed verification -> `begin_rework` with source `verification` (command, exit code, failure-first output), sharing MAX_REWORK_ROUNDS (not enlarged). `test_output_excerpt` keeps failure lines with context plus the tail. `run_verification` centralizes all four call sites; OSError -> reported "could not run" (no rework), timeout -> exit 124 (reworkable). Report/inspect print `Blocked by:`, the failing output, and an accurate Next step.
### Tests
New tests/test_orchestration_verification_rework.py (5 tests): rework converges; exhaustion handoff with reason/output/inspect; resume unchanged stops again; resume after user fix completes without another implementation; unrunnable and timed-out commands; excerpt extraction. Orchestration modules: 108 passed. make lint clean. Full suite: pre-push hook.
### Commit
Repository: howlplane. Branch: dogfood/DOG-026-verification-rework. SHA: 744bc4a. Push status: PUSHED (pre-push full suite 2321 passed). PR: not yet opened.
### Regression verification
- run-033 (same mission, identical start, 744bc4a): COMPLETE, verified CLEAN; verification passed first time, so it proves no regression but not the fix.
- Live proof: `howl orchestrate resume --repo dogfood-missions/run-032/invoicegen` on 744bc4a (the user-upgrades-then-resumes path; runs/run-032/03-resume.*): `Configured validation failed: \`npm test\` exited 1` -> `REWORK Round 1 of 2: sending the failed verification back to implementation` -> Codex fixed it -> `npm test` passed (62/62) -> review CLEAN -> ACCEPTED -> COMPLETE, `Rework rounds: 1 of 2`, exit 0. User WIP sha256 unchanged.
### Notes
Caught by testing my own report text: an earlier draft claimed resume-without-changes would get another implementation attempt; it does not (budget spent), and the text now says so.

## DOG-027 — Reviewers cannot see the diff they are asked to inspect, so compatibility requirements go unverified
Status: RESOLVED (verified live, run-034)
Severity: P2 (independent review weaker than claimed; no false success observed)
Discovered in: run-032 resume review notes (also visible in run-030: "I read the diff and tests with Read only")
Owning component: HowlPlane orchestration (review/acceptance prompt evidence)
Repository: howlplane
### User action
Normal `howl orchestrate` on existing repositories with explicit compatibility requirements (runs 030-033).
### Expected
The independent reviewer can see what the implementation changed relative to the original and judge "existing tests unchanged" and "output byte-identical" claims.
### Actual
Claude review notes (run-032 resume): "no shell was available in this role", "I could not diff against HEAD to confirm that the original CLI tests and render tests are unmodified", "I could not confirm that the original source gave the same defaults and byte-identical env-only output", marked NON-BLOCKING. Review roles get Read/Glob/Grep only (provider_execution_profile.py: read-only git granted to mutating roles only; pinned by test_review_roles_stay_read_only), while the prompt says "Independently inspect the current diff".
### User impact
Under AUTO routing Claude reviews every normal run, so every audit of an existing codebase is diff-blind; compatibility regressions could pass review.
### Root cause
Evidence the reviewer needs exists only as git state, which the read-only contract (rightly) keeps away from review roles; HowlPlane did not supply it, unlike verification output (DOG-014).
### Resolution
Design choice (pros/cons recorded in the session): HowlPlane computes the diff itself rather than widening reviewer grants (keeps the read-only contract; avoids reviewers running git, which can execute repo-configured programs, see DOG-028). `session_diff_evidence` adds `git diff HEAD` (bounded, 12000 chars) plus new-file list to review and acceptance prompts. `base_dirty` recorded at session start so the user's pre-existing uncommitted changes are named and excluded; files mixing both are labelled.
### Tests
tests/test_orchestration_review_diff_evidence.py (5 tests, real prompt building through a recording backend). test_provider_permissions unchanged and passing.
### Commit
howlplane dogfood/DOG-027-review-diff-evidence 175b57a (stacked on DOG-026 744bc4a). Push: PUSHED (pre-push full suite 2327 passed).
### Regression verification
run-034 (175b57a): reviewer wrote "shopcalc/pricing.py is not in the diff. Only README.md, TESTING_NOTES.md, tests/__init__.py and tests/test_pricing.py changed or were added", a scope check impossible before. Runs 035-037 on the same engine clean.

## DOG-028 — HowlPlane's own git calls run programs named in repository config (sandbox escape path)
Status: RESOLVED (verified live, run-038 targeted experiment)
Severity: P1 (security: trust-boundary violation)
Discovered in: building DOG-027's regression test (a planted textconv driver fired during an ordinary session)
Owning component: HowlPlane git launcher (git_env.py) and orchestration evidence
Repository: howlplane
### User action
Any `howl orchestrate` session (checkpoint fingerprint runs `git status` and `git diff` at every checkpoint).
### Expected
HowlPlane reads the repository as data; agents' tool sandboxes are the only place agent-influenced code runs.
### Actual
git 2.53 executes `core.fsmonitor` on `git status`/index refresh, and `diff.<driver>.textconv` on `git diff --binary --no-ext-diff` (HowlPlane's fingerprint). Reproduced: a `.git/config` with these set made HowlPlane create a marker file during a session. `git_baseline.py` and `human_boundary.py` also ran unguarded `git diff HEAD` (diff.external too).
### User impact
An implementer agent (Edit/Write tools, or Codex workspace-write) that writes `.git/config`, for example under prompt injection from repository content, gets HowlPlane to run an arbitrary program with the user's full privileges outside the agent sandbox.
### Root cause
Defect class: git invocations treated as read-only although git config can name executables. The sanitized git env (2026-08-26) scrubbed only repository-selection variables.
### Resolution
`git_env.guarded_git_args`: `-c core.fsmonitor=false` on every call; `--no-ext-diff --no-textconv` on diff/log/show. Applied in `run_git_in_repo` (all canonical callers), orchestration's git calls, and the factory campaign helper. User-supplied `--verify` commands still run as given.
### Tests
test_git_env_isolation::test_repository_configured_programs_never_run_from_howlplane_git_calls (status/diff/log/show + evidence; proves unguarded git would run it); full-session test in test_orchestration_review_diff_evidence.
### Commit
Same commit as DOG-027 (175b57a), shared files. PUSHED.
### Regression verification
run-038 TARGETED DOGFOOD EXPERIMENT: core.fsmonitor logging hook in the target repo; public-CLI session made 4 hook calls, all from Codex's sandboxed git, 0 from HowlPlane (control: plain git status fires it).
### Notes
Not addressed (out of scope, recorded): git hooks in `.git/hooks` / `core.hooksPath` still run on HowlPlane's own commits in git_integration; disabling them would change semantics for users who rely on their hooks. Candidate for a separate design decision.

## DOG-029 — Secret redaction rewrites the user's goal and review notes ("token once" -> "token <redacted>")
Status: RESOLVED (verified live, run-040)
Severity: P1 (requirements altered before any agent reads them; risk of missed requirements and false completion)
Discovered in: run-039 (adaptive tranche 2, multi-user/API-token change to the bookmarks service)
Owning component: HowlPlane orchestration redaction (orchestration.SECRET) vs canonical presentation/redact.py
Repository: howlplane
### User action
`howl orchestrate "<goal about API tokens and Bearer auth>" --repo dogfood-missions/run-039/bookmarks`.
### Expected
Agents receive the goal as written; only real credentials are masked in stored and reported text.
### Actual
The report's Goal line (= doc["goal"], used in every worker prompt) read: "prints a newly generated API token <redacted>, ... issue a new token <redacted> an existing user. Store only a hash of each token, never the token <redacted>", "requires `Authorization: Bearer <redacted> A missing, malformed, unknown or revoked token <redacted> 401". Words lost: once, for, itself, `<token>`, returns. The review notes were mangled the same way ("Token <redacted> only the SHA-256 hex digest is stored").
### Evidence
runs/run-039/stdout.log (8 `<redacted>` occurrences, none of them secrets). Pattern: `(token|password|secret|api[_-]?key)[=: ]+(\S+)`, where a bare space counts as a separator.
### User impact
Any goal about authentication, tokens, passwords or secrets reaches agents altered. Here Codex inferred the intent (the deliverable met every requirement), but "revoked token returns 401" lost its verb. Review findings sent to rework pass through the same filter.
### Root cause
Defect class: two redaction implementations diverged. Orchestration's private pattern used keyword + space. The canonical primitive (which claims to be the only one) would also have masked documentation placeholders (`Authorization: Bearer <token>`). Redaction treated any word near a keyword as a credential instead of requiring the value to look like one.
### Resolution
orchestration.redact delegates to presentation.redact.redact_operator_text. The canonical patterns require credential-shaped values: name=value (any non-placeholder value), name: value (credential charset, 6+ characters, contains a digit), Bearer/Authorization (8+ characters, contains a digit). Placeholders (<x>, $X, {x}) are never masked. Quoted JSON keys stay unmatched, so HowlPlane's lease token survives manifest redaction. Trade-off recorded: "password: changeme" is no longer masked.
### Tests
tests/test_redaction_preserves_prose.py (prose and placeholders survive; 8 credential forms masked; orchestration == canonical; goal stored and saved verbatim). 331 passed in the affected modules; make lint clean.
### Commit
howlplane dogfood/DOG-029-redaction-prose 8e5b2ab (based on main 068e3d7, not stacked). Push: PUSHED (pre-push 2338 passed). PR #157 (base main).
### Regression verification
run-040 (same mission, identical start, local integration a911cc9): the live session manifest goal was byte-identical to the mission text (no redaction markers); the report showed 1 [REDACTED], which was DOG-030 (below), not the goal.

## DOG-030 — Reviewers were shown a redacted diff: text that is not in the repository
Status: RESOLVED (merged via #156; verified live in run-043)
Severity: P2 (reviewers judge distorted evidence; risk of false findings and needless rework)
Discovered in: run-040 review notes ("The README example uses `API_TOKEN='[REDACTED]'` as a placeholder")
Owning component: HowlPlane orchestration, session_diff_evidence (DOG-027 code)
Repository: howlplane
### Actual
README line `API_TOKEN='paste-the-printed-token-here'` reached the reviewer as `API_TOKEN='[REDACTED]'`; the reviewer flagged the README for content it does not contain.
### Root cause
Same class as DOG-029: redaction applied to evidence passed between agents. The diff is never persisted and the reviewer can Read the files, so redacting it adds no confidentiality, only distortion. Introduced by my DOG-027 change.
### Resolution
The diff is no longer redacted. Residual risk recorded: plan and verdict excerpts are stored redacted in the manifest and re-fed to agents; with DOG-029's narrower patterns, only literal name=value lines can be altered.
### Tests
test_orchestration_review_diff_evidence::test_reviewers_see_the_repository_text_not_a_redacted_version (fails on the old line).
### Commit
howlplane dogfood/DOG-027-review-diff-evidence (PR #156), 540ca27 on top of 175b57a, PUSHED (pre-push 2328 passed).
### Regression verification
run-043 (main bb7ba21, a change full of `PINGBOT_API_TOKEN=...` lines): the reviewer quoted the diff's token-shaped literals as written (`abc123==`, `K = a=b = c`) and raised no finding about placeholder or masked text. DOG-029 also held again: goal byte-identical, no markers in the report.

## DOG-031 — `howl doctor` reports HEALTHY while a developer-profile runtime lacks a dependency its checkout declares
Status: RESOLVED (verified through the public CLI on the real installation)
Severity: P1 (false health; HowlWriter's native path and the creative pipeline unusable with no signal)
Discovered in: run-042 setup (creative pipeline preflight)
Owning component: Howl installer (health checks, doctor --fix, developer source installs)
Repository: howl
### User action
`howl status`, `howl doctor`, then `howlwriter native write --request ... --command-config remote.json`.
### Expected
If an installed component cannot run its documented commands because a declared dependency is missing, doctor says so and `--fix` repairs it.
### Actual
`howl doctor`: every component healthy, "Ecosystem Status: HEALTHY". `howlwriter native write`: ModuleNotFoundError: howl_provider_core. The developer-profile HowlWriter runtime is an editable install of the checkout; the checkout later added a hard, pinned dependency; the runtime never received it. The health check is `--version`, and pip's metadata (written at install time) makes `pip check` pass too.
Two adjacent defects in the repair path: doctor checked and reinstalled the raw manifest component, so `--fix` would have replaced a developer's editable checkout with the release wheel; and the source reinstall could not find the checkout unless HOWL_<NAME>_DIR or HOWL_DEV_WORKSPACE was set or doctor ran from a sibling directory.
### Root cause
Defect class: installed state was trusted after install-time resolution. Nothing tied a runtime to its source's current dependency declarations, and repair ignored the recorded install profile and the runtime's own record of its checkout.
### Resolution
Editable installs record a hash of the checkout's dependency declarations; the health check fails on change, or on an unrecorded editable runtime (PEP 610 direct_url.json), with "run `howl doctor --fix`". Doctor applies the recorded profile (`Component.ForProfile`, shared with planning). Source reinstall falls back to the checkout the runtime records.
### Tests
pyruntime (no-op without editable, unrecorded, drift per declaration file + resync, InstalledCheckout cases), health (drift fails a passing exec check), doctor (repair method follows profile). gofmt, vet, `go test -race ./...`, build clean.
Public CLI: fixed `howl doctor` -> FAIL howlwriter + howlplane-engine (unverified) -> `howl doctor --fix` -> reinstalled both -> HEALTHY; howl_provider_core importable; `howlwriter native write` produced a package (FACTUALLY_PRESERVED).
### Commit
howl dogfood/DOG-031-editable-dependency-drift 5ee87c1, PUSHED; PR howlcipher/howl#15. (The installed ~/.local/bin/howl is still the old binary until the PR merges and howl self-updates; the repair was run with a build of the branch.)
### Notes
Host observation (not a Howl defect): howlcreate's own developer venv (in its checkout, not installer-managed) had a stale provider-core lacking `classify_failure`; resynced with `uv pip install -e .`.

## DOG-032 — The documented creative pipeline is not reachable through the official installer
Status: RESOLVED (option B, chosen by the user; verified live)
Severity: P2 (promised integration unavailable without hand-installing into an installer-owned runtime)
Discovered in: run-042
Owning component: Howl installer manifest and HowlPlane creative pipeline (boundary between them)
Repository: howl, howlplane
### Actual
HowlPlane's README presents `howlplane creative run` (HowlDream -> HowlWriter -> HowlCreate) as native. The pipeline runs each stage as `<HowlPlane's python> -m <component>` and its preflight requires all four packages importable in HowlPlane's engine runtime. The installer manifest does not include howldream, howlcreate or howl-provider-core, and installs HowlWriter into a separate runtime. On a standard install, `howlplane doctor --creative` therefore fails 6 checks, and its remediation (`pip install 'git+...@main'`) means hand-installing into an installer-owned virtualenv. The README's "Plane runs each component through its own CLI" is inaccurate (it runs their modules under its own interpreter).
### User-mode workaround used in run-042
Installed editable howldream, howlwriter and howlcreate into the engine runtime (pip resolved the shared provider-core pin 5837d46); preflight PASS 18/18. Note: installing the local editable provider-core alongside conflicts with the consumers' pinned direct URL (operator error, not a defect; the preflight's pin check covers it).
### Options (for the user)
A) Installer owns composition: add the creative components to the manifest as an optional capability installed into the engine runtime.
B) HowlPlane owns composition: run each component's console script in its own installer-managed runtime (what the README describes), and make the preflight check CLIs and contracts by subprocess.
### Resolution (2026-10-07)
User chose option B. Stages run the `howldream`, `howlwriter` and `howlcreate` console scripts from PATH in their own environments, recording each resolved executable; a missing CLI fails the stage clearly and blocks the preflight. The preflight checks each component with `<cli> --version` (still catches stale component venvs) and validates the command profile by shape when provider-core is not in HowlPlane's interpreter. In-process package/symbol/pin/schema checks and `--repos-root` removed.
### Live verification
Workaround removed first (engine runtime package set restored to its pre-workaround state). Then `howlplane doctor --creative` PASS 9/9 and `howlplane creative run` (runs/c2-optionB) COMPLETED all 7 stages, lineage intact, each stage running /home/.../.local/bin/<component>. The same run showed DOG-033's "Review required" section live.
### Commit
howlplane cb74193, PR #160, merged 86eb74f.

## DOG-033 — A COMPLETED creative run hides copy that failed Writer's fidelity check
Status: RESOLVED (merged #158; verified live in run-042's option-B rerun)
Severity: P2 (part of the request silently not delivered; same class as DOG-015/022)
Discovered in: run-042
Owning component: HowlPlane creative pipeline report (creative_pipeline.py)
Repository: howlplane
### Actual
Writer marked the subhead FACTUAL_REVIEW_REQUIRED; Create kept the original placeholder for it (usable=false). The CLI printed "Creative run ...: COMPLETED" and seven COMPLETED stages, nothing else; creative-run.json recorded only the package-level factual_status.
### Resolution
writer_write records review_required (item, status, findings); materialize records withheld_items; the report prints a "Review required" section. Status and exit code unchanged. The guidance names the package to review and does not claim an untested amend/resume path.
### Tests
test_creative_pipeline: review section with findings and withheld items; no section for a fully verified run (26 creative tests pass; make lint clean).
### Commit
howlplane dogfood/DOG-033-creative-review-visibility 4d3a214 (first push rejected by the pre-push SlopsLint clone ceiling, my duplicated test helper; deduplicated). PR #158.

## DOG-034 — HowlPlane's own commits run git hooks that agents could plant mid-task
Status: FIXED (merged #161, cafca95; policy proven by real-git tests, no live campaign run since)
Severity: P1 (security: same class as DOG-028)
Discovered in: follow-up to DOG-028 (tranche 1 open item), policy chosen with the user on 2026-10-07
Owning component: HowlPlane git integration (factory/marathon commits)
Repository: howlplane
### Actual
GitIntegrationExecutor.stage_and_commit runs `git commit`, which executes `.git/hooks/*` or the core.hooksPath directory. Agents can write both; reviewers never see them in the diff.
### Resolution
Keep the user's hooks; refuse to commit when the hook setup changed after the task branch was created. create_task_branch fingerprints core.hooksPath plus each hook's content and executable bit; stage_and_commit refuses before staging when it differs, naming the change. Limitation: a commit from a process that did not create the branch (resumed campaign) has no baseline.
### Tests
tests/test_git_hook_change_guard.py on a real repo with a bare origin: user hook still runs; new hook, changed hook and hooksPath redirect each block the commit with the planted hook unrun and HEAD unmoved. Git integration, marathon, crash recovery, acceptance canary modules unchanged (96 pass).
### Commit
howlplane dogfood/DOG-034-hook-change-guard 8bbceb0, PR #161, merged cafca95 (pre-push 2351 passed; CI green).

## DOG-035 — `orchestrate --verify` rejects any verification command that has options
Status: FIXED (merged #164, f39cf18; HowlPlane review session 889142d7 COMPLETE, audit CLEAN; post-merge public proof recorded in howl-cubs-dogfood)
Severity: P1 (documented first-run path unusable; no workaround is documented)
Discovered in: howl-cubs-dogfood mission R001 (github.com/howlcipher/howl-cubs-dogfood, workflow-evidence/R001/plane/CUBS-P-001-repro.txt), 2026-10-08
Owning component: HowlPlane orchestrate CLI parser; docs in howl README
Repository: howlplane (parser, ORCHESTRATE.md); howl (README example)
### Expected
`howl orchestrate "Add a --version flag" --repo . --verify python3 -m unittest` (howl README.md:160) starts a session verified by that command.
### Actual
Exit 2 before any session: `howlplane: error: unrecognized arguments: -m unittest`. `--verify` is `nargs="+"`, so argparse reads the command's own flags as orchestrate options. Same for `--verify python3 -m pytest -q`. Factory queue tasks already bypassed argv for this reason; the CLI did not, and no test parsed a dashed `--verify` through the CLI.
### Resolution
`verification_command()` parses a single CLI value using POSIX shell quoting for new sessions and resume, including `--verify "python3 -m pytest -q"` and the `--verify=...` form. Existing executable paths remain literal, including spaces; inner quotes preserve paths that do not exist yet. Multiple CLI values and Factory queue arrays remain literal argv, including queue retries. Empty CLI commands and malformed quoting report `INVALID_VERIFICATION_COMMAND`; execution uses no shell. Unquoted flags can collide with orchestrate options; unknown flags such as `-m` exit 2 with a quoting hint restricted to orchestrate invocations using `--verify`. ORCHESTRATE.md documents these rules. The external howl README example needs the quoted form; this checkout does not establish its update status.
### Tests
`tests/test_orchestration_verification_rework.py`: real verification and rework through both CLI value forms; quoted resume override; literal Factory argv; absolute and repository-relative executable paths containing spaces; inner quoting, dash-free argv, blank commands and malformed quotes; unknown-option errors with and without the quoting hint. New regressions reproduced path corruption, malformed-input handling gaps, and the unrelated-subcommand hint before correction. Existing rework tests retain their intended failure and recovery contracts.

## DOG-036 — The verification command's 300-second limit is fixed and undocumented
Status: FIXED (merged #164, f39cf18; HowlPlane review session 889142d7 COMPLETE, audit CLEAN; post-merge public proof recorded in howl-cubs-dogfood)
Severity: P2 (capability gap with a hidden limit; blocks acceptance for repositories with long required gates)
Discovered in: howl-cubs-dogfood mission R001, HowlPlane review session 1426222b on the DOG-035 fix, 2026-10-08
Owning component: HowlPlane orchestration verification
Repository: howlplane
### Actual
`VERIFY_TIMEOUT_SECONDS = 300` was not configurable or documented. HowlPlane's own full suite takes about 456 s; acceptance applied documentation/TESTING.md and rejected twice for missing full-gate evidence; implementers that ran the suite hit the 600 s execution budget and read-only roles may not run git-invoking tests, so no session configuration could produce the evidence.
### Resolution
`--verify-timeout SECONDS` (1 to 3600, refused outside the range) on a new session or `resume`, stored as `verify_timeout_seconds`; default unchanged; the overrun message names the option; ORCHESTRATE.md documents it.
### Tests
tests/test_orchestration_verification_rework.py: a 2 s command times out under a 1 s limit (exit 124, message names the option), then `resume --verify-timeout 10` re-runs the same check on the same tree and completes; out-of-range values refused; CLI parse and default.

## DOG-037 — AGY's read-only roles were not read-only; one write discarded the whole session
Status: FIXED (merged; see the status note at the end of this file)
Severity: P1 (permission boundary; loss of verified work)
Discovered in: howl-cubs-dogfood mission R001, S1 session b3e9d287 on cubs-edge-lab, 2026-10-08
Owning component: HowlPlane orchestration routing (AGY backend)
Repository: howlplane
### Actual
AGY was assigned review (`agy -p ... --mode plan`). It ran the project's probe CLI (523 network requests) and overwrote tracked research files. HowlPlane detected READ_ONLY_ROLE_MUTATED_REPOSITORY and ended the session BLOCKED, not resumable, discarding Codex's verified implementation. AGY's `plan` mode makes no read-only guarantee; the other CLIs enforce read-only through sandbox, tool allowlist or mode.
### Resolution
READ_ONLY_UNENFORCED = {agy}: AUTO routing skips AGY for planning, review and acceptance with a stated reason; an explicitly chosen AGY orchestrator still plans and accepts (explicit override) but never reviews. A repository change in any of those roles, including a failed or timed-out planning attempt, or a review finding or acceptance rejection that also writes, sets READ_ONLY_ROLE_MUTATED_REPOSITORY and ends the session BLOCKED and not resumable. It is not sent back for rework.
### Tests
tests/test_orchestration_role_containment.py: AGY never gets a read-only role in AUTO routing, including configured planning, review, and acceptance fallbacks, acceptance takeover, and resume of an older AUTO session that selected AGY; a session whose only non-implementer is AGY ends AUDIT BLOCKED with the reason; with Cursor available the audit runs and completes; an explicit AGY orchestrator plans and accepts but never reviews. A write during planning, review, or acceptance blocks the session and refuses resume, including a planning failure or timeout, and a review finding or acceptance rejection that also writes; a planning failure that does not write stays resumable. Two role-scoped-capacity tests re-expressed through `remediation`.

## DOG-038 — No way to let an orchestrate worker fetch public data
Status: FIXED (merged; see the status note at the end of this file)
Severity: P2 (capability gap for research and data-acquisition goals)
Discovered in: howl-cubs-dogfood mission R001, S1 session b3e9d287, 2026-10-08
Owning component: HowlPlane orchestration / Codex backend
Repository: howlplane
### Actual
Codex implementation runs `--sandbox workspace-write`, whose network is off: `statsapi.mlb.com` failed DNS inside the worker while the host resolved it. No option granted network, so a goal that must fetch public data could not run; the implementer honestly reported "live probe not run".
### Resolution
`--worker-network` (new session or resume) sets `worker_network` on the session; implementation and remediation tasks carry it, and Codex adds `-c sandbox_workspace_write.network_access=true`. Verified directly: the same Codex prompt fails DNS without the setting and resolves with it. Read-only roles never receive it.
### Tests
tests/test_orchestration_role_containment.py: flag parse and default off; only mutating roles receive the metadata, on and off, including after resume; Codex argv carries the setting only for networked mutating tasks, and AGY, Cursor, Claude, Gemini, and Devin argv do not gain network configuration.

## DOG-039 — A Codex usage limit was recorded as "not authenticated"
Status: FIXED (merged; see the status note at the end of this file)
Severity: P2 (wrong capacity evidence persisted across sessions; wrong recovery advice)
Discovered in: howl-cubs-dogfood mission R001, S1 rerun 6713a592 on cubs-edge-lab, 2026-10-08
Owning component: HowlPlane provider failure classification (synthesis/provider_pool.py)
Repository: howlplane
### Actual
Codex ended with `ERROR: You've hit your usage limit. Upgrade to Pro (...) or try again at 8:01 PM.` (codex_error_info usage_limit_exceeded). No anchored terminal pattern knew "usage limit." followed by vendor text; the transcript-wide fallback checks authentication markers before capacity markers and found "unauthorized" ten times in the user's AGENTS.md rules prose. HowlPlane recorded AUTHENTICATION_REQUIRED and the readiness cache said "Codex UNAVAILABLE not authenticated" while `codex login status` reported a login.
### Resolution
Anchored SESSION_LIMIT pattern for Codex's usage-limit stop line (trailing upsell and reset text allowed); fallback auth markers use "401 unauthorized" instead of the bare word. Real anchored auth stop lines ("Unauthorized", "error: not authenticated", "Login required") are unchanged.
### Tests
tests/test_usage_limit_classification.py: Codex usage limit after a transcript containing the rules prose classifies SESSION_LIMIT (both apostrophes); rules prose alone is not authentication; five real auth stop lines still classify as authentication; the readiness cache records an expiring SESSION_LIMIT limit and no auth=false. 4 of 9 fail without the fix (the 5 preservation tests pass both ways).

## DOG-042 — A declared IMPLEMENTATION_STATUS: INCOMPLETE was recorded as success
Status: FIX IN REVIEW (branch dogfood/R002-orchestration-fixes)
Severity: P2 (false success; wasted review and rework rounds)
Discovered in: howl-cubs-dogfood R001, session 43b8a9e6 on cubs-edge-lab, 2026-10-09
Owning component: HowlPlane orchestration
Repository: howlplane
### Actual
Codex ended three implementation attempts with "IMPLEMENTATION_STATUS: INCOMPLETE" and its reason (no authorization to fetch data). Each was recorded SUCCEEDED, verified and reviewed; review found the missing work each time, and the session ended AUDIT BLOCKED after both rework rounds.
### Resolution
A status line declaring INCOMPLETE in a mutating role makes the attempt REVOKED with IMPLEMENTATION_INCOMPLETE (a role failure): partial changes kept, the stated reason stored as the verdict excerpt and printed in the report, the next implementer continues; the session hands off when none remains. Implementers are told to use the status rather than present partial work as finished. Only a status line counts, not the phrase quoted in prose.
### Tests
tests/test_orchestration_declared_status.py: reroute and completion by the next implementer before any review; handoff with the printed reason when all declare INCOMPLETE; status-line recognition including the bold form Codex used and prose that only mentions the phrase. All fail without the fix.

## DOG-041 — Acceptance cited a superseded planner VERIFY_COMMAND as the session's command
Status: FIX IN REVIEW (branch dogfood/R002-orchestration-fixes)
Severity: P3 (misleading review context; contributed to rejections)
Discovered in: howl-cubs-dogfood R001, review session 1426222b, 2026-10-08
Owning component: HowlPlane orchestration prompts
Repository: howlplane
### Actual
The planner's VERIFY_COMMAND named a nonexistent tests/test_task_queue.py. The explicit --verify superseded it and was the command actually run, but the plan excerpt given to review and acceptance still contained the line; two acceptors reported "the supplied verification command names nonexistent tests/test_task_queue.py".
### Resolution
verification_command_note(): when an explicit --verify differs from the planned command, review and acceptance instructions state which command verifies the session and that the planned one was superseded and not run.
### Tests
tests/test_orchestration_declared_status.py: the note reaches both review and acceptance prompts; no note when there is no explicit command, no planned command, or they are equal. The positive case fails without the fix.

## DOG-044 — Acceptance did not receive HowlPlane verification evidence
Status: FIX IN REVIEW (branch dogfood/R004-dog044)
Severity: P2 (passing work was rejected for missing test evidence, consuming both rework rounds)
Discovered in: howl-cubs-dogfood R004, session 8814b377
Owning component: HowlPlane orchestration
Repository: howlplane
### Actual
The configured validation passed three times, but the read-only Claude acceptor rejected the work because the prompt did not show that HowlPlane had run the verification command. Both rework rounds were spent on a passing tree and the session ended HANDOFF REQUIRED.
### Root cause
`execute_assignment()` included `verification_evidence_for_review(doc)` in review instructions but acceptance received only audit verdicts and the session diff. The harness results in `doc["tests"]` were therefore invisible to acceptance.
### Resolution
Acceptance now receives the same latest-per-command harness evidence as review, labelled as HowlPlane evidence with command, exit code and output tail. It explicitly says when no verification command ran and distinguishes a diff check from test verification. Reconciliation clears results when the tree changes, and implementation verification precedes acceptance.
### Tests
`tests/test_orchestration_acceptance_verification_evidence.py`: passing and failing results, no verification, diff-check-only, latest result per command, review output parity, reconciliation freshness, and the R004 command path with explicit `--verify true`. The contract and R004 regression tests fail without the fix.

## DOG-043 — Correctable orchestrate refusals were reported as suspected HowlPlane bugs
Status: FIX IN REVIEW (branch dogfood/R002-orchestration-fixes)
Severity: P3 (misleading UX; wrong recovery advice)
Discovered in: howl-cubs-dogfood R001 (`--separate` on a worktree with an unfinished session), 2026-10-09
Owning component: HowlPlane orchestrate CLI / presentation
Repository: howlplane
### Actual
Documented refusals raised plain ValueError, which the presentation layer reports as "Something unexpected failed ... likely a HowlPlane bug or an unhandled condition (ValueError) ... INTERNAL_ERROR" with a saved traceback. Reproduced for `--separate` on the same worktree, `resume` with no unfinished session, and `--verify-timeout 0`; 22 such raise sites in orchestration.py.
### Resolution
OrchestrateRequestError (a ValueError subclass) for the 22 user-correctable refusals, with optional next step and command; explain() renders it as ORCHESTRATE_REQUEST_REFUSED ("nothing was changed"). Internal failures (git inspection, coordinator lease race) keep their existing handling.
### Tests
tests/test_orchestration_declared_status.py: three refusals through command() map to the operator error; `--separate` on the same worktree names `git worktree add`; internal errors are not relabelled. All fail without the fix.

## DOG-040 — A refused test command marked a proven agent interactive-only everywhere
Status: FIX IN REVIEW (branch dogfood/R002-orchestration-fixes)
Severity: P2 (false capability evidence persisted across repositories)
Discovered in: howl-cubs-dogfood R001, review session 1426222b (existing-WIP validation), 2026-10-08
Owning component: HowlPlane orchestration routing / readiness evidence
Repository: howlplane
### Actual
Claude, validating existing work (which correctly changes nothing), ran `python3 -m pytest <tests>`; the granted command was the dash-free `pytest <tests>`. The refusal was EXECUTION_PERMISSION_REQUIRED with no repository change, so DOG-018's grant-gap rule (which required edits) did not apply, and the readiness cache marked Claude interactive-only for every repository (`agents doctor`: NEEDS ACTION) until a live doctor recovery.
### Resolution
permission_grant_gap() no longer requires edits: a refusal whose only denied tool is Bash, with named commands, in a mutating role is a session-scoped grant gap (role excluded for the session, PERMISSION line, readiness cache untouched). Edit-tool refusals and unnamed denials still mark interactive-only. A session's "unattended verified" capability was considered as the condition instead and rejected: any successful assignment, including read-only planning, sets it.
### Tests
tests/test_orchestration_capability_recovery.py: grant-gap rules (implementation and remediation Bash-only; Edit/Write refusals and unnamed denials excluded; read-only roles excluded); end-to-end, a no-edit refused test command keeps Claude ready across sessions; an Edit-tool denial still marks interactive-only. The DOG-018 test that required edits was revised with this evidence. 7 tests fail without the fix.

### Follow-up review
A mixed denial envelope can contain a named Bash command and an unnamed Bash refusal. Both must be retained as evidence: because the unnamed refusal does not establish a missing grant, the mixed case is interactive-only, not a grant gap. The denial parser records the unnamed count and grant-gap classification rejects mixed evidence. The session-only grant-gap exclusion can still cost one attempt per session for an agent that consistently refuses unattended execution; this is documented in `documentation/ORCHESTRATE.md`.

## DOG-037, DOG-038, DOG-039 — status
Merged: #165 (28511b4) for DOG-037/038 and #166 (af9f40a) for DOG-039, each with a HowlPlane review session and post-merge public proof recorded in howl-cubs-dogfood R001. Status: FIXED.
