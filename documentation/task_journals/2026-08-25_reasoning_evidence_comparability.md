# Milestone #60C: Comparable Multi Provider Reasoning Evidence

## Durable State

STATUS:
    DRAFT_PR_OPEN_INSTRUMENTATION_VERIFICATION_PENDING

STARTING_MAIN:
    a45458a237c7ceba38fb8cf7d7b27e9a017f7cef

WORKING_BRANCH:
    dogfood/reasoning-evidence-comparability

PR:
    https://github.com/howlcipher/howlplane/pull/47

CURRENT_EXPERIMENT:
    NONE

COMPLETED_EXPERIMENTS:
    NONE

TRAJECTORY_COUNTS:
    EXISTING_CURRENT_SCHEMA_LIVE: 8
    NEW_TRUSTWORTHY_60C: 0
    TARGET_NEW_60C: approximately 10 through 16 when provider availability permits

COVERAGE_MATRIX:
    TASK_CLASSES: 3
    PRIMARY_PROVIDERS: local_ollama 7; Claude 1
    PROVIDER_INVOCATIONS: local_ollama 8; Claude 3
    OBSERVED_MODEL_IDS: local Ollama models only; Claude model unobserved
    OUTCOMES: 2 successful; 6 failed
    REPAIRS: 0 successful; 2 unsuccessful; 6 without repair
    REVIEW_TOPOLOGIES: none; correctness only; correctness plus regression
    CONTEXT_STRATEGIES: 4
    RETRIEVAL_STRATEGIES: failure signature; no explicit retrieval
    ROUTING_STRATEGIES: local first; frontier first
    PROVIDER_COMPOSITION: NOT_OBSERVED
    DECOMPOSITION: NOT_OBSERVED
    LATENCY: observed for all trajectories
    COST: NOT_OBSERVED

PROVIDER_STATES:
    local_ollama: previously available; current probe pending
    claude_code: previously available; current probe pending; exact model unobserved
    codex: installed and routable at #60B close; current probe pending
    devin_cli: installed and routable at #60B close; current probe pending
    agy: installed and routable at #60B close; current probe pending
    gemini_cli: binary absent at #60B close; current probe pending

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit and push this initial durability checkpoint, create a draft PR, then run the focused #60A and #60B instrumentation tests before preregistration or provider execution.

## Campaign Boundaries

This continuation collects a second bounded set of comparable live evidence through the existing reasoning experiment machinery. It does not introduce strategy performance profiles, learned routing, cognitive optimization, exploration, exploitation, model training, authority changes, paid capacity, or manufactured observations.

The campaign prioritizes reproducibility, cloud provider diversity, legitimate repair, provider composition, meaningful decomposition, and a second review topology fixture. Missing dimensions remain NOT_OBSERVED when legitimate evidence cannot be obtained.

## Recovery State

CURRENT_BRANCH:
    dogfood/reasoning-evidence-comparability

HEAD:
    a45458a237c7ceba38fb8cf7d7b27e9a017f7cef

LAST_CONFIRMED_REMOTE_MAIN:
    a45458a237c7ceba38fb8cf7d7b27e9a017f7cef

PR:
    https://github.com/howlcipher/howlplane/pull/47

EXACT_PRIOR_EVIDENCE_PATHS:
    documentation/evidence/reasoning/2026-08-24_context_canary.yaml
    documentation/evidence/reasoning/2026-08-25_campaign_plan.yaml
    documentation/evidence/reasoning/2026-08-25_campaign_results.yaml
    logs/control_plane/milestone_60b_reasoning_evidence/experiments
    logs/control_plane/milestone_60b_reasoning_evidence/trajectories

## Preflight Re Evaluation

TASK:
    Milestone #60C comparable multi provider evidence and repair, composition, decomposition coverage.

AGENT_AND_MODEL:
    Devin CLI with Adaptive routing; exact serving model not asserted unless exposed by provider evidence.

SKILLS_ROUTED:
    devin_cli
    resume_task
    route_task
    review_change
    reconcile_reviews
    verify_change
    ship_check

RISK_AND_REASONING:
    Medium risk evidence campaign using deterministic verification and live providers. Architecture change is prohibited absent demonstrated instrumentation defects.

FREE_TOOLS:
    Existing repository control plane, local Ollama, installed provider CLIs, Git, GitHub CLI, Python, Go, flake8, Bandit, and SlopsLint. No new tool installation or paid capacity is authorized.

## Planned Sequence

1. Create the durable branch, journal checkpoint, push, and draft PR.
2. Run focused trajectory, experiment, preregistration, digest, attribution, repair, deduplication, redaction, and resume tests.
3. Load all eight current schema live trajectories and preregister the complete bounded campaign before provider execution.
4. Execute comparable context repetitions and a held constant multi provider task.
5. Search for a legitimate successful repair lifecycle without weakening verification.
6. Execute one meaningful provider composition comparison and one meaningful decomposition comparison when fixtures and providers permit.
7. Execute another appropriate review topology fixture and preserve natural disagreement with deterministic truth.
8. Inspect naturally exposed model and cost metadata without guessing.
9. Run trajectory discovery, comparability classification, evidence challenge, and quality audit.
10. Run the full verification matrix, observe required checks, complete the readiness gate, and merge only under current policy.

## Progress Log

### 2026-08-25T09:20:00Z

Verified the canonical checkout at the exact required main SHA with a clean worktree and matching origin main. The required SHA is its own ancestor. No other agent process is using this repository; the observed agy process is rooted in howlchangeops. Loaded repository rules and the continuation, routing, review, reconciliation, verification, ship, and Devin CLI procedures. The prior #60B journal confirms eight current schema live trajectories and the stated sparse dimensions. Created the requested campaign branch. No experiment or provider probe has started.

### 2026-08-25T09:30:00Z

Created signed checkpoint 4a02320 and pushed it through the normal prepush gate. The gate passed 798 Python tests plus the repository Go, lint, Bandit, build, and documentation stages; only the previously documented third party warnings appeared. Opened draft PR 47. The worktree is clean after the push. No experiment or provider probe has started.

## Handoff Packet

SESSION_INTERRUPTION:
    NONE

PROVIDER_INTERRUPTION:
    NONE

EXPERIMENT_CURRENTLY_INCOMPLETE:
    NONE

NEXT_EXPERIMENT:
    NONE until instrumentation tests pass and the full plan is preregistered.

NEXT_SAFE_ACTION:
    Commit and push the initial checkpoint, create the draft PR, and record its URL here.
