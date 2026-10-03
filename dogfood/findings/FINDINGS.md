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
Status: FIX COMMITTED (338478b), awaiting regression run. Severity: High (silently removes an agent; poisons the cross-session cache).
Evidence: run-001 planning by Claude was denied `cat README.md; howl --help | head`; HowlPlane then wrote unattended=false to the readiness cache. Later `howl agents doctor` showed Claude "NEEDS ACTION interactive only" and `howl orchestrate --orchestrator claude_code` ended in INTERNAL_ERROR (ValueError traceback, "likely a HowlPlane bug").
Root cause: the planning profile has no Bash by design, the model tried a shell, and the denial was recorded as evidence about unattended *mutation*. Also an unusable explicit orchestrator raised a bare ValueError.
Fix: denial scopes to mutating roles only (implementation/remediation); Claude read-only roles are told to use Read/Grep/Glob; explicit unavailable orchestrator raises OperatorFailure ORCHESTRATOR_UNAVAILABLE with the recovery command. Three tests that encoded the old contract were updated (observed-incident fixtures now reserve Claude; planning denial leaves capability unknown) and two tests pin the new contract (planning/review do not, implementation does mark interactive-only).
Live check: after clearing the stale cache, a Claude PLAN ONLY session completed in 15 s.

## DOG-005 — `agents doctor` advice for interactive-only does not recover it
Status: FIX COMMITTED (338478b). Severity: Medium.
Evidence: doctor's Next says `agents doctor --refresh`, and neither `--refresh` nor `--live` (no repo) cleared "interactive only" even with a passing smoke, because a session verdict outranks a smoke. Only `--live --repo <path>` (workspace smoke) cleared it.
Fix: doctor now names that command for interactive-only workers; docs explain the precedence.

## DOG-006 — Cursor reviews return no text (root cause of repeated AUDIT_NO_VERDICT)
Status: FIX COMMITTED (338478b), awaiting regression run. Severity: High (reviewer wasted ~4 min per run, 5 of 6 runs).
Evidence (run outside HowlPlane, same review prompt, run-006 target): `agent -p --mode plan --output-format text` rc 0 after 4m08s, stdout 1 byte; `--mode plan --output-format json` timed out at 290 s with no output; `--mode ask` rc 0 in 213 s with a full review, including a legitimate finding (foreign-key integrity unenforced) and `AUDIT_STATUS: FINDINGS`. The planning role in plan mode works (19 s, real output).
Fix: Cursor review and acceptance use `--mode ask`; planning keeps `--mode plan`. Note: with real reviews Cursor will now sometimes report genuine findings, which route through the normal findings path.

## DOG-007 — No recovery after an acceptance rejection
Status: FIX COMMITTED (338478b). Severity: High.
Evidence: `howl orchestrate resume` on run-001's rejected session re-printed the same HANDOFF with no worker and no guidance. The rejecting orchestrator stays excluded for acceptance for the session even after the repository changes.
Fix: verdict-based review/acceptance exclusions (AUDIT_FINDINGS_OR_UNCONFIRMED, AUDIT_NO_VERDICT, ACCEPTANCE_REJECTED_OR_UNCONFIRMED) expire when the repository changes; stale audit/acceptance results are cleared; the rejection report prints exact next steps (repair then `resume --verify`, or `discard`). Test: test_rejected_acceptance_recovers_after_the_repository_is_repaired.
Not done: automatic rework (feeding the rejection back to the implementer). That is a larger design change, recorded as a recommendation.

## DOG-008 — howl README omits `howl orchestrate`
Status: FIX PUSHED (howl 95b78ac on dogfood/readme-orchestrate; PR pending). Severity: Medium (docs).
Fix: README section on agents doctor / factory prepare / orchestrate with a first-run example, consistent with docs/SCOPE.md.

## DOG-009 — A reroute after a partly written attempt demands a --verify command the user cannot know yet
Status: OPEN (observed run-007; recovery followed as documented; not yet repaired). Severity: Medium (workflow needs an extra manual step, but the handoff states it).
Evidence: run-007. Claude (now a working planner thanks to DOG-004) planned, then its implementation wrote files and was denied a shell command (EXECUTION_PERMISSION_REQUIRED; it also tried `cd` into the howlplane repo, outside the workspace). Codex continued from the partial files and completed. The session still ended HANDOFF REQUIRED: "Partial or recovered changes require an explicit --verify command", because a failed attempt that changed the repo sets needs_validation (existing safeguard).
Impact: the mission goal contains no verification command and the app does not exist yet, so a user cannot supply one up front. After the fact it is discoverable (scripts/test.sh).
Recovery used (documented): `howl orchestrate resume --repo <target> --verify bash scripts/test.sh` (runs/run-007/05-*).
Fix candidates (design decision, not made): derive a default verification from the discovered project (ProjectAdapter already does this for Claude's allowlist), or let the planner propose it.
Note: this was always possible; earlier runs never hit it because no attempt failed after writing files.

## DOG-010 — Acceptance deadlocks when the orchestrator is disqualified (and my first fix created a false success)
Status: FIX COMMITTED (see HANDOFF for SHAs), awaiting a fresh clean run. Severity: High.
Evidence, run-007 (runs/run-007/): Claude, now a working planner (DOG-004), became session orchestrator; its implementation was denied permission and it was marked interactive-only; only the orchestrator may accept, so the first pass ended HANDOFF REQUIRED with every agent "not the session orchestrator".
First fix (commit "let acceptance move off a disqualified orchestrator") let the next eligible agent take over. Live use of it on run-007 then produced a FALSE SUCCESS: Codex took over and REJECTED (genuine concerns: workflow evidence not shown, concurrent writers can overwrite, future completion dates), the session rerouted, Cursor ACCEPTED, and the session finished COMPLETE WITH WARNINGS. My "judged" guard only covered the original orchestrator. Run-007 is therefore NOT a clean run.
Correct fix: takeover is allowed only for non-verdict disqualification; a verdict (ACCEPTANCE_REJECTED_OR_UNCONFIRMED and the other verdict failures) from ANY agent makes acceptance final until the repository changes. Tests: takeover completes; rejection by the taker is final (fails against the flawed logic, verified); rejection by the original orchestrator is final.
Lesson: a fix that widens who may approve needs a test where the new approver disagrees. The live regression caught what the unit tests missed.

## DOG-011 — Real review findings cannot be acted on: `orchestrate` has no rework step
Status: OPEN, needs a design decision (options presented to the user). Severity: High (blocks unattended completion whenever a reviewer finds a real issue).
Evidence, run-008 (fresh run, corrected code): Codex planned and implemented. Cursor (now in `--mode ask`, DOG-006) returned a genuine review with a real Major finding (README claims JSON errors for argparse failures; false) and three Minor ones. The session rerouted to AGY (CLEAN), then Codex acceptance rejected, correctly and without false success. Session ended HANDOFF REQUIRED with exact next steps (DOG-007), but nothing in the ecosystem fixes the finding.
Why it surfaced now: before DOG-006 the Cursor review was always empty, so findings never existed; after it they are real, and before DOG-002 acceptance ignored them.
Behavior today: findings route to "another reviewer"; the implementer is never asked to fix them. The only recovery is a human editing the repository, then `resume --verify`.
Options (see the question put to the user): bounded rework loop (feed findings to the implementer, re-review, cap N rounds); advisory findings with acceptance deciding; or keep the human-in-the-loop handoff.
Run-008 is not a clean run (HANDOFF REQUIRED). Streak: 0.

## Follow-up status (after DOG-009/011 implementation)
DOG-009 and DOG-011 implemented on howlplane dogfood/rework-and-default-verify (user chose: bounded rework loop, project-derived verify). Run-009 verified DOG-009 live (derived verify ran and passed). DOG-011's loop is covered by tests but has not fired in a live run yet (run-009's reviewer returned CLEAN).
Observation (not fixed, DOG-012 candidate): Cursor review in --mode ask takes 213-300+ s (runs 006, 008, 009), right at the 300 s default review budget, so it times out in some runs and is replaced. Raising the review budget or using a faster Cursor model is an operator choice (`--execution-budget review=600`); no code change made.

## DOG-012 — Default review budget cuts off half of Cursor's real reviews

Status: FIX COMMITTED (howlplane dogfood/DOG-012-review-budget 9a9a59c), awaiting regression runs 011 and 012
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

Status: FIX COMMITTED (howlplane dogfood/DOG-013-review-convergence 82414bf, stacked on DOG-012), awaiting regression runs
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

Status: FIX COMMITTED (same commit 82414bf)
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
Status: FIX COMMITTED (howlplane dogfood/DOG-013-review-convergence a1d8ce2)
Severity: Medium (false-success defense: after DOG-013 a reviewer may mark items NON-BLOCKING; the user must be able to see them)
Discovered in run: run-012
### Actual
run-012 report showed only "Independent audit: CLEAN". The report printed verdicts only for non-clean attempts, and the session manifest is removed when a session completes (unless `--retain-report`), so the CLEAN reviewer's text was gone.
### Resolution
Report prints `Review notes from <agent> (review, CLEAN):` with the stored excerpt. Test: test_clean_review_notes_are_shown_in_the_report (failed before the fix). 327 related tests passed; slopslint OK.
