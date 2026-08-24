# ADR 0005 — Reasoning Strategy Dogfooding: Architecture Gap

## Status
Accepted and implemented — Milestone #60A

## Context
Milestone #60A adds the ability for HowlPlane to discover and experiment with its
own *system-level* reasoning choices (provider composition, context, retrieval,
review topology, planning, tools, verification sequencing) without claiming to
modify the base intelligence of proprietary models.

The existing unattended engineering foundation (#56–#59.2) already implements:

- `TaskSpec` / `RoutingDecision` / `VerificationPlan` / `ReviewCycleResult`
- `EvidenceLedger` with redacted `EvidenceEntry` events
- `DurableCampaignState` for marathon dogfooding campaigns
- `ProviderPoolManager` for provider selection, failover, reviewer assignment
- `HumanBoundaryGate` / `AuthorityEnvelope` for delegated authority
- `CheckpointManager` + `atomic_io` for crash-safe persistence
- `MetricsCalculator` for aggregate operational reports

## Gap Analysis

| New abstraction | Extends | Why existing representation is insufficient |
|---|---|---|
| `ExecutionTrajectory` | `EvidenceLedger`, `DurableCampaignState`, `OrchestrationResult`, task run artifacts | The ledger is append-only events, not a schema-versioned, digest-stable orchestration history. `OrchestrationResult` is in-memory; `DurableCampaignState` aggregates benchmarks, not per-task trajectories. Repair cycles, review findings, verification results, and provider events are scattered across files. |
| `ReasoningExperiment` | `DurableCampaignState.benchmark_history`, `ProviderPoolManager` selection logic | No first-class durable artifact compares baseline vs candidate *strategies* with immutable predictions, falsification criteria, and deterministic evaluation. Benchmark history records product outcomes, not reasoning-strategy hypotheses. |
| `StrategyRegistry` + `StrategyDefinition` | `AgentProfile`, `RoutingDecision.rationale`, ad-hoc prompt text | Reasoning behavior currently lives in arbitrary prompt text and router rationale. There is no stable, versioned, digest-stable strategy identifier (e.g. `context.architecture_aware/v1`) that can be compared across experiments. |
| `ExperimentEvaluator` / deterministic comparison | `MetricsCalculator` | Metrics are descriptive summaries, not deterministic experiment outcomes. No mechanism prevents the proposing model from declaring its own candidate the winner. |
| `TrajectoryObservation` | Existing SEEK/OBSERVE backlog discovery | Historical trajectories are not mined for evidence-backed observations. `issues.md` / `improvements.md` are manually authored. |
| `ExperimentFingerprint` + reopening | N/A (new dedup layer) | No deduplication prevents endless rediscovery of the same reasoning-strategy hypothesis; no structured reopening with "what evidence is new". |
| Redaction rules for reasoning artifacts | `evidence_ledger.sanitize_value` | Trajectory/experiment persistence must explicitly exclude hidden chain-of-thought, private model reasoning, and unnecessary source dumps. |

## Rejection of Speculative Abstractions

- **No new orchestration framework** — reuse `GovernedTaskOrchestrator`, `TaskSpec`,
  `ProviderPoolManager`, `EvidenceLedger`, `CheckpointManager`, and campaign state.
- **No automatic learned preference profile / StrategyPerformanceProfile** — out of
  scope for this milestone.
- **No fine-tuning / RL / LoRA / weight updates** — explicitly forbidden.
- **No new authority mechanism** — `AuthorityProfile` / `AuthorityEnvelope` /
  `HumanBoundaryGate` remain the only authority paths.
- **No repository-trusted strategy policy** — strategy definitions, metrics, and
  authority rules are code-defined and digest-verified; repository content cannot
  silently redefine them.

## Decision and Integration Points

1. `ExecutionTrajectory` is created by `GovernedTaskOrchestrator` for a terminal
   governed result. It references task spec, route, verification plan, review
   cycles, provider events, and repair cycles, then exposes its stable ID to the
   durable campaign state. The shared artifact policy bounds and redacts payloads,
   removes hidden reasoning fields, and verifies the stored digest on load.
2. `ReasoningExperimentCoordinator` is the single experiment lifecycle for every
   supported type. It persists the immutable definition before execution,
   checkpoints baseline and candidate trajectory summaries append-only, derives
   stable per-arm trajectory IDs, and resumes the exact missing stage after a
   crash before applying deterministic evaluation.
3. `StrategyRegistry` is a small code-defined registry (`src/control_plane/reasoning/`
   or `src/control_plane/strategies/`) mapping `strategy_id` -> immutable
   `StrategyDefinition` with a SHA-256 digest over its canonical JSON.
4. `ExperimentEvaluator` consumes two trajectories (baseline/candidate) and
   computes deterministic outcomes (`SUPPORTED`, `WEAKLY_SUPPORTED`, `FALSIFIED`,
   `INCONCLUSIVE`, `NOT_YET_MEASURABLE`).
5. `TrajectoryObservation` mines completed trajectories for recurring patterns
   (e.g. repeated architecture omission, reviewer dismissal, local-first success)
   and emits candidate `improvements.md` rows through the existing backlog format.
6. Observation fingerprints deduplicate a proposed comparison. A disposed
   observation reopens only when a new trajectory reference also contributes a
   materially new observable evidence fingerprint; the durable reopening history
   records the reason, references, fingerprints, and timestamp.

## Consequences

The coordinator adds one persisted lifecycle stage to `ReasoningExperiment`, but
avoids seven separate experiment orchestrators and makes pre-registration and
resume behavior mechanically testable. Stable IDs make replay idempotent; callers
must provide a distinct event or sample identity when a task is intentionally run
again as new evidence. Fail-closed digest loading means corrupted or legacy
pre-merge artifacts require explicit recovery rather than being silently trusted.
Authority profiles, envelopes, TTLs, repository scope, merge and spend budgets,
credentials, publishing rights, and branch protection remain outside the
coordinator input and output types.

## At Least Two Future Experiments Enabled

1. **Context strategy comparison** — compare `context.full_repository/v1` vs
   `context.changed_files_plus_architecture/v1` for cross-module repair tasks,
   measuring verified first-pass success and repair cycles.
2. **Review topology comparison** — compare `review.general_single/v1` vs
   `review.correctness_security_split/v1` vs
   `review.two_independent_reconcile/v1`, measuring confirmed defects, false
   positives, and remediation cycles.

Both require the same `ExecutionTrajectory`, `ReasoningExperiment`, and
`StrategyRegistry` abstractions above.
