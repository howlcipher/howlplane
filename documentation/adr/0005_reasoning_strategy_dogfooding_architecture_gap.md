# ADR 0005 — Reasoning Strategy Dogfooding: Architecture Gap

## Status
Draft — Milestone #60A Phase 0

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

## Proposed Integration Points

1. `ExecutionTrajectory` is created/updated by `GovernedTaskOrchestrator` at
   lifecycle boundaries (planned, implementing, reviewing, remediating, verifying,
   complete/failed). It references task spec, route, verification plan, review
   cycles, provider events, and repair cycles.
2. `ReasoningExperiment` is created by a lightweight experiment runner that
   reuses the orchestrator to execute baseline and candidate configurations.
3. `StrategyRegistry` is a small code-defined registry (`src/control_plane/reasoning/`
   or `src/control_plane/strategies/`) mapping `strategy_id` -> immutable
   `StrategyDefinition` with a SHA-256 digest over its canonical JSON.
4. `ExperimentEvaluator` consumes two trajectories (baseline/candidate) and
   computes deterministic outcomes (`SUPPORTED`, `WEAKLY_SUPPORTED`, `FALSIFIED`,
   `INCONCLUSIVE`, `NOT_YET_MEASURABLE`).
5. `TrajectoryObservation` mines completed trajectories for recurring patterns
   (e.g. repeated architecture omission, reviewer dismissal, local-first success)
   and emits candidate `improvements.md` rows through the existing backlog format.
6. `ExperimentFingerprint` deduplicates by hashing (experiment_type,
   baseline_strategy_id, candidate_strategy_id, task_class, key metric names).
   Reopening requires explicit `reopened_by_evidence_refs`.

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
