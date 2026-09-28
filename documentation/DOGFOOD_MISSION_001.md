# HowlPlane Dogfood Mission 001

**Campaign:** `2026-09-27-continuous-improvement`

## Mission

Use HowlPlane to help analyze and improve HowlPlane itself as the control plane for a long-running autonomous engineering factory.

Primary question:

> Can HowlPlane reliably preserve intent and coordinate work across multiple tasks, workers, providers, failures, interruptions, handoffs, and time?

This is an execution specification for a future dogfood run. Begin the mission only when explicitly launched with the command in [Launch](#launch). Read this entire document before planning or changing anything. Audit actual implementation before trusting documentation.

## Operating principles

### Ownership and concurrent execution

**INSPECT BROADLY. MODIFY NARROWLY.**

This mission owns only:

- HowlPlane implementation
- orchestration lifecycle and state
- scheduling and task graphs
- workers and providers
- handoffs and resumability
- reconciliation
- orchestration observability

The concurrent ecosystem mission owns cross-component architecture, interoperability, shared conventions, contracts, schemas, identifiers, installers, ecosystem integration, cross-component observability, and integration documentation.

The concurrent HowlFrame mission owns HowlFrame implementation, plans, policy evaluation, authorization, hashing/integrity, TTL, apply, verify, rollback, replay protection, and execution evidence.

Inspect related repositories whenever needed to establish behavior, but do not edit another mission's owned component. Record cross-component or Frame-specific problems as structured findings with the correct `recommended_owner` instead of making competing edits.

All three missions may run simultaneously. Therefore:

- do not reuse another mission's temporary workspace
- use campaign-, run-, orchestration-, task-, and execution-scoped artifact names
- do not assume a globally exclusive process
- avoid shared unscoped temp files, logs, generated artifacts, and locks
- preserve run identity and attribution
- surface locking or global-state problems as findings rather than bypassing them

### Evidence before changes

For every candidate defect:

1. establish current behavior,
2. reproduce or demonstrate the problem,
3. preserve evidence,
4. determine expected behavior,
5. make the smallest justified in-scope change,
6. add regression coverage,
7. rerun the exposing scenario, and
8. record before/after results.

Do not perform speculative architecture rewrites merely because an agent prefers another design. Do not manufacture failures to increase finding count.

### Real self-hosting

This is not a static code review. Use HowlPlane itself to coordinate meaningful portions of the mission wherever practical, including decomposition, independent analysis, test execution, remediation planning, implementation assignments, verification, and reconciliation.

When HowlPlane cannot orchestrate work that falls reasonably within its intended responsibilities, preserve that failure as dogfood evidence before using a manual escape hatch. Do not immediately bypass it without recording the reason, exact state, commands, identifiers, artifacts, and intervention required.

### Severity

Use exactly this vocabulary:

- **P0** — safety/integrity failure that prevents trustworthy autonomous operation
- **P1** — major capability is broken or cannot reliably complete
- **P2** — substantial reliability, architecture, integration, recovery, concurrency, or correctness weakness
- **P3** — meaningful UX, diagnostics, observability, or maintainability issue
- **P4** — optional improvement

**Do not inflate severity.**

### Findings and correlation

Create machine-readable findings as well as Markdown reports. Finding IDs use `HP-*` (for example, `HP-001`). At minimum each finding must conform to this shape; extensions are allowed:

```json
{
  "id": "HP-001",
  "campaign_id": "2026-09-27-continuous-improvement",
  "component": "howlplane",
  "severity": "P2",
  "category": "resumability",
  "summary": "...",
  "evidence": [],
  "reproduction": [],
  "expected_behavior": "...",
  "recommended_owner": "howlplane",
  "dependencies": [],
  "status": "open"
}
```

Generated evidence should preserve, where feasible, `campaign_id`, `run_id`, `orchestration_id`, `task_id`, `execution_id`, and `parent_id`. Do not invent identifiers the implementation cannot reasonably produce; record missing correlation capability as a finding.

### Git discipline

Before changes, inspect repository contribution practices, branch and worktree state, protected/default branch expectations, and verification instructions. Do not overwrite unrelated work. Do not silently commit directly to a protected/default branch when the workflow expects branches or pull requests. Do not merge work merely to make the report appear successful. Preserve human authority boundaries.

## Investigation scope

Inspect and exercise the implementation of:

- orchestration lifecycle and state model
- scheduler and task decomposition
- task graph and dependencies
- worker assignment and worker contracts
- provider abstraction, selection, rotation, quota, and session boundaries
- journals and run records
- `HANDOFF REQUIRED`
- interruption handling and resumability
- artifact passing and verification
- retries, reconciliation, and idempotency
- concurrency and partial branch failure
- diagnostics, observability, and CLI behavior

Record exact source revisions, configuration, commands, inputs, outputs, identifiers, artifact paths, exit statuses, and operator interventions for every dogfood run.

## State model

Determine the canonical implemented lifecycle rather than inferring one from aspirational documentation. Produce a transition model backed by source and executable evidence. Evaluate whether transitions are:

- explicit and deterministic
- persisted and reconstructable
- resumable
- idempotent where appropriate
- safe after interruption
- safe under concurrency
- guarded against stale state and invalid transitions

For each state identify legal predecessors/successors, required durable data, side effects, retry semantics, terminality, and operator action.

## Handoff required

Exercise `HANDOFF REQUIRED` as a genuine resumable state, not a report label. Validate:

- durable persistence of the handoff state and reason
- provider change without loss of task identity
- context reconstruction from persisted records
- artifact and decision continuity
- deterministic next-state selection
- continuation without a human restating the mission

Treat any manual reconstruction or lost context as evidence.

## Resume testing

Using harmless, isolated test orchestrations, safely interrupt execution at multiple lifecycle points. Prefer deterministic fault injection or controlled process termination over timing-sensitive tests. After resume verify recovery of:

- objective
- orchestration ID
- task graph
- completed and incomplete tasks
- current states and dependencies
- worker/provider assignments
- artifacts and prior decisions
- verification state
- next valid action

Repeated resume must not duplicate completed work or side effects.

## Concurrency and task graphs

Run independent sibling tasks in parallel when safe. Verify task, journal, artifact, worker, and provider isolation; correct attribution; and deterministic reconciliation. Test partial branch failure. A failed branch must not incorrectly convert successful siblings into completed or failed states, and blocked descendants must remain distinguishable from failed or completed work.

Test dependency ordering, fan-out/fan-in, cancellation or interruption behavior, stale updates, and conflicting results. Preserve evidence of scheduling and reconciliation decisions.

## Provider behavior

Exercise where feasible:

- provider selection and eligibility
- fallback and rotation
- exhaustion and quota/session boundaries
- provider handoff
- provider-specific failures

Plane must preserve the task and its history when provider identity changes. Provider identity, interface, resource, observed model, and task assignment should remain distinguishable. Avoid real spend or external side effects unless already authorized by repository policy.

## Worker contract

Determine whether every worker receives explicit, sufficient information:

- objective
- scope and ownership boundary
- constraints
- current state
- relevant artifacts and prior decisions
- expected outputs and success criteria
- parent task and dependencies
- campaign, run, orchestration, task, and execution identifiers where available

Determine whether worker output is structured and can be interpreted deterministically rather than through fragile prompt inference. Test malformed, incomplete, stale, conflicting, and misattributed output safely.

## Failure semantics

Determine whether Plane distinguishes at least:

- retryable provider error
- permanent execution failure
- validation failure
- policy rejection
- handoff required
- human decision required
- infrastructure error
- malformed worker output
- stale state
- conflict

Do not hide materially different states behind generic failure. Verify that classification controls safe retry, blocking, handoff, observability, and final status.

## Idempotency

Test safe repeated retry, resume, and reconciliation. Look for duplicate tasks, duplicated side effects, duplicate completion, overwritten artifacts, corrupted state, repeated worker dispatch, and inconsistent accounting. Use isolated fixtures and avoid consequential duplicate actions.

## Observability

An operator must be able to determine:

- what is running and why
- active, completed, failed, and blocked tasks
- dependencies and parent/child relationships
- worker/provider assignments
- artifacts and evidence
- failures and their classification
- next expected action
- whether human input is required
- whether and how execution can resume

Improve only evidence-based, in-scope deficiencies. Ensure logs and structured state agree and include sufficient correlation.

## Remediation and verification

Only remediate validated HowlPlane-owned defects. Every fix requires:

- preserved reproduction and expected behavior
- the smallest justified correction
- regression coverage at the appropriate tier
- rerun of the negative case
- rerun of the success path
- before/after evidence from the final working tree

After remediation, perform an independent audit intended to falsify correctness. Reconcile all findings without silent dismissal. Record unresolved, disputed, out-of-scope, and human-decision findings explicitly.

## Required outputs

Place version-controlled reports under `docs/` or clearly run-scoped artifacts under the established run-artifact location:

- `HOWLPLANE_DOGFOOD_REPORT.md`
- `HOWLPLANE_FINDINGS.json`
- `HOWLPLANE_STATE_MODEL.md` when useful

The final report must include:

1. Executive Summary
2. Current Architecture
3. Canonical State Model
4. Dogfood Runs
5. Self-Hosting Results
6. Concurrency Results
7. Resumption Results
8. HANDOFF REQUIRED Results
9. Provider Rotation Results
10. Worker Contract Findings
11. Task Graph Findings
12. Failure Semantics
13. Observability
14. Defects
15. Fixes
16. Regression Coverage
17. Before/After Results
18. Remaining Findings
19. Recommended Remediation Waves

Finally, based on evidence rather than optimism, evaluate whether Plane could support many queued tasks, several concurrent workers, multiple providers, quota exhaustion, interrupted execution, long-lived projects, verification workers, human approval boundaries, retries, and execution resumed hours or days later.

Optimize for reliable autonomy, not maximum autonomy.

## Completion criteria

The mission is complete only when:

- the implemented lifecycle and transition behavior are evidenced
- meaningful work was self-hosted where practical
- concurrency, partial failure, handoff, resume, provider, worker-contract, and idempotency scenarios were exercised safely
- all failures and manual interventions were preserved
- all findings are valid machine-readable records with correct ownership
- each in-scope fix has regression coverage and before/after rerun evidence
- an independent audit challenged the results
- no ecosystem- or Frame-owned implementation was modified

## Successor Mission Contract

After this mission has completed:

1. implementation,
2. deterministic verification,
3. independent audit,
4. remediation of valid in-scope findings,
5. verification after remediation,
6. final acceptance,
7. findings reconciliation,

the agent must decide whether meaningful follow-up work remains.

Do not create another mission merely because iteration is possible, for cosmetic churn, or to keep agents busy. Create `documentation/DOGFOOD_MISSION_002.md` only when one or more of the following is true:

- an unresolved P0 finding exists
- an unresolved P1 finding exists
- meaningful P2 work remains
- multiple P3 findings share a root cause worth addressing
- completed remediation exposes another necessary maturity step
- deterministic evidence demonstrates a missing capability
- a blocked capability becomes actionable
- architecture needs a bounded follow-up before the system can progress safely

If no worthwhile successor exists, record in the final completion report:

```
NEXT_MISSION: NOT_REQUIRED
```

If meaningful work remains, create `documentation/DOGFOOD_MISSION_002.md`. The successor must be derived from Mission 001 evidence, must not merely duplicate Mission 001, and must explicitly identify:

- predecessor mission (`documentation/DOGFOOD_MISSION_001.md`)
- campaign ID (`2026-09-27-continuous-improvement`)
- findings that justify it, citing finding identifiers as evidence whenever possible
- work completed by the predecessor
- work that must not be repeated
- unresolved findings
- newly exposed findings
- dependencies
- repository ownership
- objective
- scope
- explicit non-goals
- deterministic verification
- independent audit requirements
- success criteria
- stop conditions

Mission numbering applies recursively to future missions. A Mission N may generate Mission N+1 when justified (for example, `DOGFOOD_MISSION_002.md` may generate `DOGFOOD_MISSION_003.md`). Each successor must reference its predecessor. Never overwrite previous mission documents; they are historical execution contracts.

Creating a successor mission does not authorize executing it. Do not invoke `howl orchestrate` recursively, `howl factory` recursively, or launch Mission N+1 from Mission N. Return control to the outer Factory supervisor after Mission N completes.

### Factory handoff

The final completion report for this mission must end with a machine-readable final section containing:

```
MISSION_STATUS: <COMPLETE|BLOCKED|IDLE>
NEXT_MISSION: <documentation/DOGFOOD_MISSION_002.md or NOT_REQUIRED>
OPEN_P0: <count>
OPEN_P1: <count>
OPEN_P2: <count>
OPEN_P3: <count>
BLOCKED: <true|false>
RECOMMENDED_PRIORITY: <advisory only>
FACTORY_NOTES: <any portfolio context>
```

`RECOMMENDED_PRIORITY` is advisory only; the master Factory makes the final portfolio decision. A project is allowed to report that its own next mission should not run yet.

## Launch

Run from this repository only when ready to begin the mission:

```bash
howl orchestrate \
  "Execute HowlPlane Dogfood Mission 001 exactly as specified in documentation/DOGFOOD_MISSION_001.md. Read the entire mission before planning. Work within the ownership boundaries defined there. Dogfood HowlPlane against itself, remediate validated in-scope findings, verify them deterministically, perform independent audit, and produce the required final evidence." \
  --repo "$PWD" \
  --heartbeat 15
```
