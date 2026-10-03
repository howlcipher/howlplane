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

Status: FIX COMMITTED (b07a4e0), push in progress, awaiting regression in run-003
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
Repository: howlplane; Branch: dogfood/DOG-001-persist-verdict-text; SHA: b07a4e0; Push status: IN PROGRESS

### Regression verification
run-003 (pending)

### Notes
This also closes the earlier concern that a later CLEAN review hides an earlier reviewer's findings: acceptance now sees both.

## DOG-003 — Empty reviewer output is reported as "findings"

Status: OPEN (not yet repaired; lower priority)
Severity: Medium (misleading UX)
Discovered in run: run-002
Owning component: HowlPlane orchestration
Repository: howlplane

### Actual
Cursor review exited 0 with empty stdout (verdict_excerpt blank, ~4 minutes). HowlPlane recorded `AUDIT_FINDINGS_OR_UNCONFIRMED` and printed "FAIL ... review failed", which reads as defects found. Also the Claude planner is skipped with EXECUTION_PERMISSION_REQUIRED in run-001 ("unattended execution unavailable").

### Expected
A distinct, explicit failure such as "reviewer returned no verdict", so the user knows it is a provider problem, not code findings.

### Notes
Fix candidate: classify empty/verdictless review stdout separately (touches the failure enum at orchestration.py ~L65 and its tests).
