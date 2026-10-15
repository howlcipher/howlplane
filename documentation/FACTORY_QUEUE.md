# Factory Task Queue

`howlplane factory queue QUEUE.json` is a finite supervisor over an explicit,
human-owned queue of engineering tasks. It launches each eligible task as one
ordinary `howlplane orchestrate` session, records which session the task became
and how it ended, and moves on to the next eligible task. It exits once no task
is eligible. It never commits, merges, or pushes.

## Responsibilities

The queue owns only:

- reading and validating the queue file (it never writes it),
- priority and dependency selection,
- the task revision lifecycle (fingerprints, resume, supersede),
- dispatching canonical orchestration sessions (new or `resume`),
- recording queue-level outcomes in its ledger.

Orchestration owns everything else: provider routing, failover, capability and
capacity tracking, execution budgets and timeouts, independent audit,
verification, workspace trust, session recovery, and worktree ownership. The
queue never reimplements any of these. It passes task options to the real
`orchestrate` parser, so they obey its exact choices.

## Queue file

```json
{
  "schema": "howlplane.factory_queue/v1",
  "repo": "../my-repo",
  "tasks": [
    {"id": "A", "goal": "Add input validation", "status": "APPROVED", "priority": 1,
     "constraints": ["no new dependencies"], "verify": ["make", "test"]},
    {"id": "B", "goal": "Document validation", "status": "APPROVED", "depends_on": ["A"]}
  ]
}
```

Only `APPROVED` tasks run. `PROPOSED`, `HOLD`, and `CANCELLED` are reported as
skipped. Lower `priority` runs first, then queue order. The whole file is
validated before anything runs: unknown fields, unknown dependencies, cycles,
and invalid orchestration options reject the queue.

## Task identity and revision

The `id` identifies a task. The **fingerprint** identifies a **revision** of that
task. It is a SHA-256 digest of the execution contract: every task field except
`status` and `priority`, plus the resolved repository path. That covers the id,
goal, constraints, verification command, dependency set, agents, execution
budget, and session options. Dependency order does not matter. Approving,
holding, or reprioritizing a task changes when it runs, not what it is, so it
keeps the same revision.

| Situation | Meaning |
| --- | --- |
| same id, same fingerprint | the same revision |
| same id, different fingerprint | a new revision |

Every outcome is scoped to one revision. A finished revision is complete only
for that fingerprint. Editing a completed task makes a new revision that runs
again.

## HANDOFF REQUIRED: resume or supersede

Since orchestration sessions that stop at `HANDOFF REQUIRED` (or `INTERRUPTED`,
or a recoverable `BLOCKED`) are resumable, the queue decides what to do with
them explicitly.

**Same revision.** The session stays the canonical unfinished session for that
task. Without `--retry` it is reported, not rerun:

```
A: previous outcome HANDOFF REQUIRED; edit the task or pass --retry A to resume session 1a2b3c4d
```

With `--retry A` the queue dispatches `orchestrate resume` on that same session
and never starts a parallel one. Orchestration then reconciles the worktree, so
an operator's external repair is detected and verified instead of redone.

**New revision.** When the operator materially edits a task, its previous
revision must not continue against the new definition. Immediately before the
new revision launches, the queue retires each still-resumable session of an
older revision through `orchestration.supersede`:

```
FACTORY SUPERSEDE A revision 3f09c1d2a7b4 -> 9e8d7c6b5a41; retiring orchestration session 5c1e...
FACTORY START     A revision 9e8d7c6b5a41
```

`supersede` keeps the session manifest. The status becomes `SUPERSEDED`, and it
records the reason, the replacement, and the previous status and stage. It is
no longer resumable, so it no longer owns the worktree. The ledger gains a
`superseded` record with the old and new fingerprints, the session id, the
previous status, the reason (`task definition changed`), and the timestamp. A
superseded revision is never relaunched on its own. If an operator reverts the
edit, that revision needs `--retry`, and it starts a fresh session.

A session is superseded only when the queue's own ledger proves that it belongs
to an older revision of the same task in the same queue. Every other resumable
session in the repository blocks the task and is left untouched:

```
A: not run; repository /repo has an unfinished orchestration session 7a1b2c3d (HANDOFF REQUIRED) that is not this task's; ...
```

Nothing is retired unless the new revision can launch right away. If the
worktree has uncommitted changes, or the old session's coordinator is still
alive, the old session keeps its worktree and the task is reported as blocked.

## Dependencies

A dependency is satisfied only by a successful outcome (`COMPLETE` or
`COMPLETE WITH WARNINGS`) of its **current** revision. Superseding, or finishing
a superseded session by hand, never releases dependents. If A v1 is superseded
by A v2, B stays `waiting on A` until A v2 succeeds. Live session evidence
counts: completing the current revision's session with `orchestrate resume`
releases its dependents on the next run.

## Retry

`--retry ID` (repeatable) permits one more dispatch of the current revision
after a non-successful outcome. It resumes the revision's session when that
session is still resumable, and otherwise starts a new session. An edited task
never needs `--retry`: a changed fingerprint is new work, not a retry.

## Dry run

`--dry-run` reads the queue, ledger, and session state and reports what a run
would do next: start, resume, or supersede then start. It never supersedes,
writes the ledger, or launches orchestration.

```
Next task: A
  A: would supersede previous revision 3f09c1d2a7b4 -> 9e8d7c6b5a41; would retire orchestration session 5c1e... (HANDOFF REQUIRED)
  A: would start revision 9e8d7c6b5a41 as a new orchestration session
```

## Output and exit codes

Progress lines (`FACTORY SELECT`, `SUPERSEDE`, `START`, `RESUME`, `RESULT`) go to
stderr. With `--json`, stdout carries only the JSON summary (`results`,
`superseded`, `blocked`, `skipped`, `stop_reason`), and session reports go to
stderr. The exit code is 0 when every dispatched task succeeded, 130 when
interrupted, and 2 otherwise, including when a previous revision could not be
retired.

`--max-tasks N` and `--stop-on-failure` bound a run. Only one run can work a
given queue at a time.

## Ledger and state

The ledger is an append-only JSONL file outside every repository, at
`$XDG_STATE_HOME/howlplane/factory_queue/<queue-hash>.jsonl`, or at `--ledger
PATH`. Records are `started`, `finished`, and `superseded`. They are redacted
before writing and hold ids, fingerprints, statuses, and paths, never task
output. A corrupt or malformed ledger line, or an unreadable session manifest,
stops the run before anything is dispatched or retired.

## Existing mission binding

An external engineering campaign can retain its canonical `state.json`, `queue.json`
and accepted-item ledger while using this finite native queue. The operator prepares
a reviewed binding; it is a compatibility and conservation contract, not an approval
envelope. External documents or model text cannot grant authority through it.

```json
{
  "schema": "howlplane.mission_binding/v1",
  "workspace": "/owned/mission",
  "mission_id": "EXISTING-001",
  "maximum": 25,
  "accepted_ids": ["EXISTING-001-A"],
  "accepted_ledger": "accepted.jsonl",
  "sources": {
    "state.json": "SHA256_OF_EXACT_BYTES",
    "queue.json": "SHA256_OF_EXACT_BYTES",
    "accepted.jsonl": "SHA256_OF_EXACT_BYTES"
  },
  "queue_sha256": "SHA256_OF_EXACT_NATIVE_QUEUE",
  "native_ledger": "/owned/mission/native-queue.jsonl",
  "repair_limit": 3,
  "tasks": {"remaining": {"purpose": "engineering", "queue_id": "remaining", "root_cause": "recorded-cause", "secondary_workflow": null}}
}
```

The example is structural, not an executable authorization. The accepted IDs must
match the canonical budget count and real `task_completed` ledger rows; explicitly
nonengineering rows are excluded. Every source stays inside the workspace, every
mapped task names an existing canonical queue item, and the native ledger location
is fixed. One canonical item cannot acquire multiple native aliases. Each engineering task explicitly names its existing root-cause counter and optionally a secondary workflow. Exhausted applicable root-cause/cycle limits bar engineering dispatch. Existing task attempts plus recorded native starts share the repair limit across revisions and aliases. Native engineering starts also consume the applicable shared root-cause allowance and reserve one significant secondary repair/retest cycle per dispatch; attribution is stored in the native ledger, so aliases and revisions cannot lose it. Distinct engineering dispatches reserve the remaining slots; completion of
a native session does not automatically accept or recount a product delivery.

`state.json` supplies `mission`, `budget.accepted_items`, `budget.maximum`,
`root_cause_attempts`, `secondary_workflow_cycles` and optional `pending_decisions`,
`status` and `stop_reason`. Canonical queue records supply `id`, `attempts` and
optional `status`. Full source hashes preserve fields the adapter does not interpret.
Changes require a new deliberate reviewed binding; there is no automatic ceiling
extension, retry reset, approval grant or campaign creation.

```bash
howlplane factory queue QUEUE.json --mission-contract BINDING.json --dry-run --json
howlplane factory queue QUEUE.json --mission-contract BINDING.json --max-tasks 1 --json
```

Execution acquires the existing workspace's `controller.lock` host flock before
loading mutable native state, holds it through native session execution, and also
uses the existing native queue/session locks. Contention exits without writing a
conflict checkpoint into the other controller's records. Source/contract/queue
changes stop dispatch. The inference ban is enforced for the native execution.

A mapping with `purpose: verification` may operate at the engineering ceiling but
cannot start or supersede a session. It requires the same task's unfinished
acceptance checkpoint and an `artifact_fingerprint`, obtained using the native
`fingerprint(evidence(repo))` helpers against the reviewed accepted Git state.
The queue invokes `orchestrate resume --verification-only`: only an already
successful implementation with pending acceptance qualifies, explicit verification
runs again, and failed checks, mutations or rejection hand off without reimplementation.
This mode cannot create a new session. Existing native worker fencing, provider
routing, timeouts and reports remain responsible for recovery.

The service renderer accepts `--queue-file QUEUE.json --mission-contract BINDING.json`
together with its existing explicit checkout/state/target/Python arguments. It checks
the binding and exact task set, renders `factory queue`, bounds the finite run and
uses native `--retry` for the pinned tasks. Repeated launches remain subject to the
same canonical and native attempt history. Configuration errors exit1 and mission
boundaries exit78, both excluded from service restart. Rendering is not installation;
review the source, final regression/CI and bound inputs, checkpoint and fence the old
controller, then install/start the single verified service. Never deploy a candidate
as the running controller or describe an interactive exit as automatic recovery.
