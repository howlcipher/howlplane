# HowlPlane — Operational Resilience Architecture

**Document Version:** 1.0.0  
**Milestone:** #56 — Operational Resilience  
**Repository:** `howlcipher/howlplane`  
**Status:** Canonical Reference & Operational Guide  

---

## 1. Governing Principles & Resilience Invariants

HowlPlane operates as a local transaction coordinator and safety harness for software engineering agent processes. Unlike naive agent frameworks that restart from scratch on process death or blindly re-execute commands, HowlPlane enforces deterministic crash recovery, durable resumability, repository-level concurrency control, safe cancellation, and exactly-once consequential execution semantics without requiring background daemons or Redis. **No database is required for correctness:** durable files remain the single source of truth, and the optional factory index is a derived, rebuildable accelerator that can be deleted at any time and restored with `howlplane factory reindex`. No recovery, authority, or verification decision may read from it.

### Core Resilience Invariants

```text
NO INTERRUPTION MAY SILENTLY:
  1. Lose task history or uncommitted code mutations
  2. Repeat consequential or bounded execution actions
  3. Overwrite or corrupt another concurrent task's work
  4. Trust partial, truncated, or unverified task artifacts
  5. Bypass independent review or deterministic verification
  6. Incorrectly transition to COMPLETE state
```

---

## 2. Concurrency Control & Multi-Level Locking

HowlPlane prevents concurrent agent collisions, race conditions, and corrupted git working trees through file-based locks enriched with PID and process start timestamp verification.

```text
       RepoLock (Target Workspace Root)
  ┌──────────────────────────────────────────────┐
  │  .task_runs/.repo.lock                       │
  │  • Locks workspace for active mutations      │
  │  • Stale PID detection via OS create_time    │
  │  • Permits read-only inspection (ai status)  │
  └──────────────────────┬───────────────────────┘
                         │
                         ▼
       TaskLock (Per-Task Execution Scope)
  ┌──────────────────────────────────────────────┐
  │  .task_runs/<task_id>/.task.lock             │
  │  • Prevents concurrent ai resume / ai run    │
  │  • Scope limited to single task lifecycle    │
  └──────────────────────────────────────────────┘
```

### 2.1 Repository Mutation Lock (`RepoLock`)
- **Path:** `<workspace>/.task_runs/.repo.lock`
- **Schema:** `howlplane.lock/v1`
- **Fields:** `task_id`, `pid`, `hostname`, `command`, `lock_type: "repository_mutation"`, `started_at`, `process_create_time`.
- **Liveness & Stale Reclamation:** On lock collision, HowlPlane inspects the recorded PID and compares `/proc/<pid>/stat` process start ticks (or OS equivalent). If the process is dead, nonexistent, or PID has wrapped around to a different binary, the lock is automatically reclaimed with an audit log. Active processes block concurrent mutations with `RepositoryLockedError`.
- **Read-Only Coexistence:** Inspection commands (`ai status`, `ai route`, `ai doctor`) do not acquire `RepoLock` and execute safely while a task is running.

### 2.2 Task Run Lock (`TaskLock`)
- **Path:** `<workspace>/.task_runs/<task_id>/.task.lock`
- **Schema:** `howlplane.lock/v1`
- **Function:** Serializes resume, approval, and remediation operations on a specific task run, preventing duplicate worker invocations.

### 2.3 Lock Ownership Lineage

One governed run reaches the same lock through more than one component: `ai
resume` takes the task lock, validates the run, and then hands control to the
orchestrator, which needs that same lock to do the work. Ownership is therefore
tracked per **lifecycle**, not per object and not per process.

- A successful acquisition creates a `LockOwnership` token (lineage id, lock
  path, task, operation) and registers it against the canonical lock path.
- A component handed that token may re-enter the lock; the lineage depth
  increases and the lock file is released only when every holder has released.
- A caller with no token, or a token from a different lineage, is refused
  exactly as a foreign process would be. Sharing a PID is not sharing
  authority, so `os.getpid()` is deliberately not the test.
- Another live process is still blocked, stale locks are still reclaimable, and
  ambiguous ownership still fails closed.

Before this, `ai resume` acquired the task lock and the orchestrator then tried
to acquire it again as a separate object, so every documented recovery of an
interrupted run failed with `Task Run lock already held`
(HOWLFRAM-SLOPFIX-06).

### 2.4 Acquisition and Cleanup

All locks and the progress tracker are acquired inside a single `ExitStack`, so
a failure at any startup point unwinds every earlier acquisition. The repository
lock used to be taken before the task lock but outside the guarded block, so a
task-lock failure stranded `.git/howlplane.lock` and blocked every later run.
Progress now starts only once the locks are held, so a resume that cannot
acquire never overwrites the durable progress of the run it was recovering.

### 2.5 Reclaiming Locks (`ai unlock`)

`ai unlock <task>` inspects **both** the task-run lock and the repository lock,
so what it can act on matches what `ai status` reports. For each it validates
that the lock belongs to the named task and is a task-owned lock, then:

| Owner state | Behavior |
| :--- | :--- |
| `ACTIVE` | Refused. A running process is never displaced. |
| `STALE` | Explicit, audited reclaim. |
| `AMBIGUOUS` | Reclaimable only by this deliberate human action. |
| Another task's lock | Refused. |
| Nothing relevant held | Truthful no-op. |

Every outcome is recorded to the evidence ledger (`unlock_requested`,
`unlock_refused`, `stale_lock_reclaimed`). This is a reclaim path, not an
arbitrary lock remover.

---

## 3. Crash Recovery & Durable Lifecycle Resume

When a process crashes or is interrupted (`SIGINT`, power loss, terminal close), HowlPlane inspects the durable task run directory at `.task_runs/<task_id>` to determine the exact recovery strategy.

### 3.1 Stage Checkpoints
- **Path:** `.task_runs/<task_id>/checkpoints/<stage_name>.json`
- **Schema:** `howlplane.checkpoint/v1`
- **Attributes:** `stage_name`, `status`, `pid`, `hostname`, `repo_fingerprint`, `started_at`, `completed_at`.

### 3.2 Stage Recovery Classifications

| Interrupted Stage | Disk Delta Status | Recovery Classification | Action Taken on `ai resume` / rerun |
| :--- | :--- | :--- | :--- |
| `planning` | None | `RERUN_STAGE` | Re-runs task router and verification plan generation cleanly |
| `implementing` | Empty / No changes | `RERUN_STAGE` | Re-launches implementation agent cleanly |
| `implementing` | Code delta present | `RECONCILE_FIRST` | Recovers captured delta against baseline without blind re-execution; advances to review |
| `reviewing` | Partial reviewer logs | `RECONCILE_FIRST` | Reuses reviewers whose persisted verdict was clean or findings; re-runs missing, invalid, and failed ones |
| `remediating` | New changes written | `RECONCILE_FIRST` | Discovers updated diff and routes to re-review cycle |
| `verifying` | Unchanged workspace | `RERUN_STAGE` | Reruns incomplete verification checks from plan |
| `verifying` | Drifted workspace | `INVALIDATE_AND_RETRY` | Invalidates prior review & verification; triggers re-review |
| `awaiting_human` | Decision packet saved | `RECONCILE_FIRST` | Preserved across restarts; requires `ai approve` or `ai reject` |

### 3.3 Reviewer Execution Evidence

A reviewer's markdown transcript and findings file cannot settle whether it
succeeded: a zero-byte transcript beside an empty findings list is exactly what
a clean review and a dead provider both leave behind. Each reviewer therefore
persists what actually happened:

```
reviews/<role>/result.json                        # effective outcome
reviews/<role>/attempts/<NN-resource>/result.json  # every provider tried
reviews/<role>.md                                  # transcript (legacy-compatible)
reviews/<role>_findings.yaml                       # findings (legacy-compatible)
```

`result.json` records the role, resource and provider identity, timings,
process exit code, launch outcome, raw and normalized failure, whether output
was present and structurally valid, findings count, disposition, failover
transitions and the independence result.

**Resume reads that verdict; it never re-derives one.** Status is one of
`clean`, `findings_detected`, `output_invalid`, `malformed_output` or
`reviewer_failure`, and only the first two count as complete. Runs predating
`result.json` fall back to the same validator the live path uses, so an empty
transcript reconstructs as `output_invalid` rather than clean. Inferring
"clean" from `len(findings) == 0` is what let a dead reviewer's empty output
pass as a completed review, and skipped the retry it was owed
(HOWLFRAM-SLOPFIX-06).

`ai status` reports the same durable dispositions -- `completed_clean`,
`completed_with_findings`, `invalid`, `failed`, `running`, `pending` -- rather
than treating the presence of two files as a completed review.

### 3.4 Recovery Is Audited

Recovery is governed execution, so it leaves evidence like any other stage.
`ai resume` records `resume_requested`, `resume_lock_state`, `resume_started`,
and then `resume_completed` or `resume_failed`, capturing the previous
lifecycle state, every relevant lock's classification and owner, the outcome,
the failure reason, and the resulting checkpoint. **A failed resume is itself
durable evidence** -- SLOPFIX-06's two failed attempts mutated progress and
lock state and recorded nothing.

---

## 4. Incremental Review & Verification Caching

Independent reviewer executions are isolated, incremental, and recorded in real time:

- **Review Cycle Directory:** `.task_runs/<task_id>/reviews/` (Cycle 1) and `.task_runs/<task_id>/remediation/cycle-XX/re_review/` (Subsequent cycles).
- **Artifacts:** `<role_id>.md` (raw review brief output) and `<role_id>_findings.yaml` (structured findings).
- **Incremental Resume:** If an orchestrator process is killed after reviewer 1 (`correctness-reviewer`) finishes but before reviewer 2 (`security-reviewer`) starts, resuming the cycle loads reviewer 1's findings directly from disk and invokes only reviewer 2.
- **Drift Invalidation:** If the git working tree changes between reviews or verification, the review cache is invalidated fail-closed.

---

## 5. In-Flight Process Tracking & Safe Cancellation

HowlPlane manages child agent sub-processes explicitly to avoid orphaned background workers or accidental worktree destruction.

### 5.1 Process Registration
When launching an implementation or reviewer backend, the process is recorded in `.task_runs/<task_id>/process.json` with its PID, start ticks, backend ID, and invocation command.

### 5.2 Safe Cancellation (`ai cancel <task-id>`)
1. **Target Verification:** Inspects registered PID and verifies create ticks to avoid signaling re-used PIDs.
2. **Graceful Escalation:** Sends `SIGTERM`, waits up to 3.0s for graceful shutdown, and sends `SIGKILL` only if the child process fails to exit.
3. **Artifact Integrity:** Cleans up `process.json` and transitions `TaskSpec` to `cancelled`.
4. **Code Preservation:** Uncommitted files written by the agent are preserved in the git workspace for developer review (no destructive `git reset --hard`).

---

## 6. Exactly-Once Consequential Execution Semantics

Consequential and high-risk actions (package publishing, database migrations, production deployments, release candidate creation) require explicit human authority and bounded executor delegation.

### 6.1 Action Invariants
- **Intent Is Not Authority:** An implementation agent's proposal is an untrusted draft until explicit human authorization is recorded.
- **Approval Is Not Execution:** Human approval authorizes an action against a specific repository state fingerprint; it does not perform the mutation.
- **Execution Is Not Complete Until Verified:** Completion requires a cryptographically verifiable execution receipt from a trusted bounded executor (e.g. `howlcipher/howlchangeops`).

### 6.2 Idempotent Resume & Replay Hazard Mitigation
On `ai resume <task_id>`:
1. **State Binding Check:** Computes `RepositoryStateFingerprint` (commit SHA + status + diff) and verifies it matches the approved fingerprint. Any repository drift raises `StaleApprovalError` (fail-closed).
2. **Native Executor Status Query:** Before calling `executor.execute()`, HowlPlane queries the bounded executor's receipt ledger (e.g. `HowlChangeOpsExecutor.query_execution_status`).
3. **Duplicate Prevention:** If a native receipt already exists on disk (e.g. from an interrupted execution that completed on the backend before HowlPlane recorded it), the receipt is imported and verified without issuing a duplicate mutation.
4. **Receipt Provenance Check:** The execution receipt is verified against expected commit SHA, repository name, decision ID, and digital signature.

---

## 7. Atomic I/O & Fail-Closed Artifact Loading

All task artifacts (`task.yaml`, `evidence_ledger.jsonl`, `checkpoints/*.json`, `reviews/*.yaml`) are written using atomic file swaps:
1. Content is written to a temporary file (`.<name>.<pid>.tmp`) on the same filesystem.
2. The file descriptor is flushed and synced (`fsync`).
3. `os.replace` atomically updates the target path.

If an artifact is truncated (0-byte) or unparseable, loaders fail closed with `CorruptArtifactError` rather than guessing or continuing in an undefined state.

---

## 8. Command Reference

| Command | Usage | Description |
| :--- | :--- | :--- |
| `ai status` | `ai status` | Displays repository lock state, active task runs, recovery classifications, and pending approvals |
| `ai cancel` | `ai cancel <task_id>` | Gracefully terminates in-flight child processes and transitions task to `cancelled` while preserving code |
| `ai approve` | `ai approve <task_id> [--reason <msg>]` | Records human approval with repository fingerprint binding (idempotent) |
| `ai reject` | `ai reject <task_id> [--reason <msg>]` | Records human rejection and transitions task to `failed` |
| `ai resume` | `ai resume <task_id>` | Recovers interrupted tasks, checks drift, executes authorized bounded actions, and advances to `complete` |
| `ai work` | `ai work <task_id>` | Runs governed task orchestrator with automatic `RepoLock` acquisition and stage checkpoints |
