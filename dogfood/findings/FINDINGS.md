# Findings

## DOG-001 — Review/acceptance verdict text is discarded; user cannot see why a session stopped

Status: OPEN (investigating, run-001)
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
TBD

### Tests / Commit / Regression verification
TBD

### Notes
Generated app itself (independently checked after handoff): `python3 -m household_tasks --help` works; `bash scripts/test.sh` passes 11 tests incl. cross-process persistence. Files are uncommitted in the target working tree (README.md modified, others untracked); the session did not commit them.
