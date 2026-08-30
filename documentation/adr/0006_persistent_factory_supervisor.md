# ADR 0006 — Persistent Factory Supervisor: Architecture Gap

## Status
Accepted — carve-out authorized by the repository owner on 2026-08-30
(`documentation/CONTROL_PLANE.md` §1.1.1). Implementation in progress.

## Context

HowlPlane is a mature *governed task engine*. Given a task it routes, implements
with bounded provider failover, independently reviews, reconciles, verifies
deterministically, and ships under an explicit authority envelope. Milestones
#56–#61 and PRs #50–#66 hardened exactly this surface.

What does not exist is a layer above a single task. Every command —
`ai work`, `ai resume`, `ai marathon`, `ai dogfood` — is a bounded CLI
invocation that starts, works, persists JSON to disk, and exits. Nothing in
`src/control_plane/` has a lifetime longer than one invocation.

The intended operating model is an owner who supplies goals, priorities,
constraints and authority — not a continuous stream of tickets. Absence of
direction must not mean absence of useful work. That requires a supervisor that
observes, discovers, prioritizes, executes through the existing governed
pipeline, parks what needs the owner, backs off when nothing is worth doing, and
remembers across restarts.

The existing foundation this builds on:

- `TaskSpec` / `RoutingDecision` / `VerificationPlan` / `ReviewCycleResult`
- `GovernedTaskOrchestrator` and `_execute_governed_engineering_improvement`
- `ProviderPoolManager` — availability, structured failure classification,
  per-class cooldowns, automatic re-probe
- `AuthorityProfile` / `AuthorityEnvelope` / `HumanBoundaryGate` /
  `ProposedAction`
- `ParkedTaskRecord` and `compute_blocks_other_work`
- `CrashRecoveryEngine`, `CheckpointManager`, `classify_lock_owner`, `atomic_io`
- `ExecutionTrajectory`, `TrajectoryObservation`, `ReasoningExperiment`
- `BacklogSource` over the ranked markdown backlogs
- `DurableCampaignState` for marathon campaigns

## Evidence

Measured from durable state in this repository on 2026-08-30. This is the
evidence the §1.1.1 carve-out requires, and the baseline against which the work
is judged.

| Measurement | Value |
| :--- | :--- |
| Recorded campaigns | 722 (`.dogfood_runs/*/campaign_state.json`) |
| Terminated on `all_providers_exhausted` | **231 (32%)** |
| Terminated on `completed_all_benchmarks` | 474 |
| Campaigns containing any `ParkedTaskRecord` | **0** |
| Evidence ledger | 139,870,255 bytes / 147,806 lines, single JSONL, no index, no rotation |
| Production callers of `discover_observations()` | **0** (two test modules only) |
| Files in `documentation/agent_memory/` | 1 (its own init file) |
| Pending backlog rows, howlplane | **0** across `issues.md` + `improvements.md` |
| Pending backlog rows, howlframe | 16 (3 bugs, 13 improvements, 1 below floor) |

Two consequences follow directly. The pool-exhaustion figure means a third of
all runs stop for a condition that resolves itself on a cooldown nothing is
alive to wait for. The howlplane-versus-howlframe backlog figures mean that
without discovery, a factory pointed at HowlPlane has literally nothing to do —
so discovery is not a later refinement, it belongs in the first slice.

## Gap Analysis

| New abstraction | Extends | Why the existing representation is insufficient |
|---|---|---|
| `FactorySupervisor` | `MarathonDogfoodEngine.run_backlog_marathon` | The marathon loop is bounded-and-stop by design — `max_tasks_reached`, `max_runtime_reached`, `resources_unavailable`, `backlog_exhausted`. It never waits and never re-checks. Nothing in the control plane has a lifetime longer than one CLI invocation. |
| `WorkItem` + `WorkItemOrigin` | `BacklogItem` | `BacklogItem.kind` is only `bug`/`improvement`, derived from the filename. Owner direction, discovered problems, and self-improvement are indistinguishable, so no portfolio balancing or anti-starvation policy can be expressed. |
| Repository discovery miners | `trajectory_discovery.PATTERN_MINERS` | The machinery is correct and the scope is wrong: four miners over `ExecutionTrajectory` only, never repository evidence. Verified unreachable from production code. |
| `discovery_core.reconcile_by_fingerprint` | the reconcile loop inside `discover_observations` | The reopen rule — reopen only on materially new evidence *fingerprints*, not merely new refs — is subtle, is what prevents re-proposing what the owner rejected, and must have exactly one implementation for two consumers under a 13-clone ceiling. |
| Blocker taxonomy + resolution items | `ParkedTaskRecord`, `RetryClassification` | Both classify a stop; neither creates bounded work to remove it. No parent/child dependency, no depth limit, no resume-parent-after-resolution path. |
| Owner inbox aggregation | `DurableCampaignState.render_markdown` | The record schema is already complete. What is missing is aggregation: it renders one campaign, surfaced by `ai dogfood --status <id>`. No cross-campaign or cross-repo view exists, and no command lists parked work. |
| `FactoryIndex` (derived SQLite) | `EvidenceLedger`, campaign and task-run directories | Answering "what did you build yesterday" requires scanning 139.8 MB plus 722 unindexed directories. The ledger has no index, no rotation, and no seek. |
| Portfolio policy | file-order precedence in `BacklogSource` | Ordering is bugs-file before improvements-file, then committed row order. There is no category weighting, no anti-starvation, and no way to stop the system spending every cycle on itself. |

## Rejection of Speculative Abstractions

- **No new orchestration framework** — reuse `GovernedTaskOrchestrator`,
  `TaskSpec`, `ProviderPoolManager`, `EvidenceLedger`, `CheckpointManager`,
  `DurableCampaignState`.
- **No new authority mechanism** — `AuthorityProfile` / `AuthorityEnvelope` /
  `HumanBoundaryGate` remain the only authority paths, exactly as ADR 0005
  established. `NEVER_DELEGATABLE_BOUNDARIES` is untouched. Portfolio admission
  is not authority.
- **No concurrency** — sequential execution across independently parked tasks is
  sufficient for v1; concurrency is complexity without demonstrated need.
- **No package ecosystem or artifact registry** — deferred until the factory can
  show it re-derived something it already had.
- **No learned prioritization model** — ranking is a transparent, legible policy
  over evidence, not a score pretending to precision.
- **No database as source of truth** — files remain authoritative; the index is
  a deletable accelerator.
- **No outcome measurement in this slice** — deliberately deferred, and named in
  the Consequences below as the reason readiness is capped.

## Decision

1. A new `src/control_plane/factory/` package holds the supervisor, work-item
   model, portfolio policy, discovery, blocker handling, inbox, and derived
   index. It calls the governed pipeline; it does not reimplement any part of it.
2. `MarathonDogfoodEngine` gains `work_one_item()`, extracted from the body of
   `run_backlog_marathon`'s loop. The marathon keeps its bounds, stop reasons and
   return payload; both loops then share one execution body rather than cloning
   the park/complete/fail bookkeeping.
3. `DurableObjectStore` moves to `src/control_plane/durable_store.py` and the
   fingerprint/reconcile algorithm to `src/control_plane/discovery_core.py`,
   both with re-export shims so no existing import changes.
4. The supervisor binds an authority envelope **once**, at campaign creation.
   `_bind_authority_envelope` deliberately resets an envelope's TTL when given an
   explicit profile on resume — correct as operator reauthorization, but a
   supervisor passing the profile every tick would silently turn a 12-hour
   delegation into a perpetual one. The supervisor therefore passes `None`
   thereafter, reports `OWNER_REQUIRED` on expiry, and requires an explicit
   `factory reauthorize` to renew.
5. Self-modification is fail-closed by path prefix, and the supervisor verifies a
   digest of its own controller every tick, stopping cleanly rather than
   hot-reloading itself.
6. The derived index is never read for an authority, recovery, or execution
   decision; any staleness, corruption, or schema drift forces a rebuild or a
   file-scan fallback, never a different answer.

## Consequences

The control plane gains a process lifetime it has never had, and with it the
obligation to be honest about idleness: the supervisor's default disposition is
to stop dispatching and back off, not to find something to commit. The measured
231-campaign exhaustion figure becomes the primary regression signal.

Because outcome measurement is deferred, the factory can tell the owner what it
built but not yet whether it helped. That is the explicit reason the persistent
factory is assessed `FACTORY_READY_WITH_LIMITATIONS` and the wider system
`AUTONOMOUS_ORG_EARLY` rather than anything stronger, and it is the highest-value
next milestone.

Known limitations, stated rather than discovered later: the supervisor is
foreground-only, so unattended duration is capped by a terminal session; the
12h/10h envelope TTLs cap it further, degrading to propose-only until the owner
reauthorizes; discovery covers four evidence sources, not twenty, and cannot mine
the evidence ledger until it is indexed or rotated; the factory never authors
into the owner's markdown backlogs; and it does not run inside an isolated
worktree of itself, so the controller-digest check turns concurrent self-edits
into a clean stop rather than a corruption.

## Evidence This Milestone Must Emit

Because §1.1.1 obliges every subsequent factory pull request to cite operational
evidence, this milestone must produce the numbers that gate the next one:
the disposition histogram across ticks; the count of runs continued by backoff
that would previously have stopped at `resources_unavailable`; the ratio of
proposals generated to proposals admitted, as a measure of restraint; and the
number of parked tasks that actually reached the owner.
