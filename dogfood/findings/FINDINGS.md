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
