# Milestone #60C: Comparable Multi Provider Reasoning Evidence

## Durable State

STATUS:
    POST_CAMPAIGN_AUDIT_COMPLETE_AWAITING_COMMIT_AND_FULL_VERIFICATION

STARTING_MAIN:
    a45458a237c7ceba38fb8cf7d7b27e9a017f7cef

WORKING_BRANCH:
    dogfood/reasoning-evidence-comparability

PR:
    https://github.com/howlcipher/howlplane/pull/47

CURRENT_HEAD:
    f48f5ee9f8e11d9dc8d420fa9aa03662fa298a31

STAGED_WORK:
    README.md
    change_log.md
    documentation/evidence/reasoning/2026-08-25_comparability_results.yaml
    documentation/task_journals/2026-08-25_reasoning_evidence_comparability.md

CURRENT_EXPERIMENT:
    NONE

COMPLETED_EXPERIMENTS:
    EXP-60C-CONTEXT-001
    EXP-60C-PROVIDER-001
    EXP-60C-REPAIR-001
    EXP-60C-COMPOSITION-001
    EXP-60C-DECOMPOSITION-001
    EXP-60C-REVIEW-001

TRAJECTORY_COUNTS:
    EXISTING_CURRENT_SCHEMA_LIVE: 8
    NEW_TRUSTWORTHY_60C: 14
    TOTAL_LIVE_CURRENT_SCHEMA: 22
    TARGET_NEW_60C: 14 (campaign target met; additional evidence required per readiness recommendation)

COVERAGE_MATRIX:
    RAW_EXECUTED_ATTEMPTS: 22
    CAMPAIGNS: MILESTONE-60A 2; MILESTONE-60B 6; MILESTONE-60C 14
    TASK_CLASSES: other 12; bug_fix 6; test_improvement 4
    PRIMARY_PROVIDERS: local_ollama 17; claude_code 3; codex 2
    PROVIDER_INVOCATIONS: local_ollama 21; claude_code 7; codex 2
    OBSERVED_MODEL_IDS: local Ollama qwen2.5-coder:7b-instruct only; Claude and Codex NOT_OBSERVED
    OUTCOMES: success 9; verification_failed 13
    REPAIRS: no_repair 19; one_unsuccessful_repair 3; successful_repair 0
    REVIEWER_TOPOLOGIES: no_reviewers 18; correctness_only 1; correctness_plus_regression 1; general_reviewer_only 1; correctness_plus_security 1
    CONTEXT_STRATEGIES: context.task_plus_acceptance/v1 6; context.changed_files_plus_architecture/v1 1; context.relevant_documentation/v1 7; context.changed_files_only/v1 2; none 6
    RETRIEVAL_STRATEGIES: retrieval.failure_signature/v1 3; retrieval.no_historical/v1 1; none 18
    DECOMPOSITION_STRATEGIES: planning.direct_implementation/v1 1; planning.subsystem_decomposition/v1 1; none 20
    REVIEW_STRATEGIES: review.general_single/v1 2; review.correctness_regression/v1 1; review.correctness_security_split/v1 1; none 18
    PROVIDER_COMPOSITION: NOT_OBSERVED (2 live raw attempts preserved)
    MEANINGFUL_TASK_DECOMPOSITION: NOT_OBSERVED (2 live raw attempts preserved)
    CLOUD_PRIMARY_TRAJECTORIES: 5
    LATENCY: observed for all 22 trajectories
    COST: NOT_OBSERVED

PROVIDER_STATES:
    local_ollama: executed successfully during campaign (qwen2.5-coder:7b-instruct); current probe pending
    claude_code: executed successfully during campaign; exact model NOT_OBSERVED; cost NOT_OBSERVED; no authentication prompt; current probe pending
    codex: executed successfully during campaign; exact model NOT_OBSERVED; cost NOT_OBSERVED; no authentication prompt; current probe pending
    devin_cli: installed and routable at #60B close; current probe pending
    agy: installed and routable at #60B close; current probe pending
    gemini_cli: binary absent at #60B close; current probe pending

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit and push the staged audit update as a durability checkpoint, then run the full verification matrix (Python, Go, lint, Bandit, SlopsLint, docs) before marking the PR ready for review.

## Campaign Boundaries

This continuation collects a second bounded set of comparable live evidence through the existing reasoning experiment machinery. It does not introduce strategy performance profiles, learned routing, cognitive optimization, exploration, exploitation, model training, authority changes, paid capacity, or manufactured observations.

The campaign prioritizes reproducibility, cloud provider diversity, legitimate repair, provider composition, meaningful decomposition, and a second review topology fixture. Missing dimensions remain NOT_OBSERVED when legitimate evidence cannot be obtained.

## Recovery State

CURRENT_BRANCH:
    dogfood/reasoning-evidence-comparability

HEAD:
    f48f5ee9f8e11d9dc8d420fa9aa03662fa298a31

LAST_CONFIRMED_REMOTE_MAIN:
    a45458a237c7ceba38fb8cf7d7b27e9a017f7cef

PR:
    https://github.com/howlcipher/howlplane/pull/47

EXACT_PRIOR_EVIDENCE_PATHS:
    documentation/evidence/reasoning/2026-08-24_context_canary.yaml
    documentation/evidence/reasoning/2026-08-25_campaign_plan.yaml
    documentation/evidence/reasoning/2026-08-25_campaign_results.yaml
    documentation/evidence/reasoning/2026-08-25_comparability_plan.yaml
    documentation/evidence/reasoning/2026-08-25_comparability_results.yaml
    logs/control_plane/milestone_60b_reasoning_evidence/experiments
    logs/control_plane/milestone_60b_reasoning_evidence/trajectories
    logs/control_plane/milestone_60c_reasoning_evidence/experiments
    logs/control_plane/milestone_60c_reasoning_evidence/trajectories

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

### 2026-08-25T10:20:00Z

Created the full bounded #60C campaign plan and preregistered all six executable experiments through ReasoningExperimentCoordinator before any provider prompt. EXP-60C-CONTEXT-001 then produced four live local Ollama trajectories, two additional samples per exact #60B arm. All four failed the exact verifier; the candidate responses consumed substantially more latency without passing. Coordinator resume left the count unchanged at four, all trajectory digests and the experiment prediction digest validated, and no raw prompt was persisted. The experiment result is INCONCLUSIVE with baseline_n=2 and candidate_n=2.

### 2026-08-25T10:28:00Z

EXP-60C-PROVIDER-001 held task, source context, prompt reference, verifier, tools, and repair policy constant across Claude Code and Codex. Both providers were already authenticated, both exact responses passed, neither exposed a trustworthy model identifier or cost, and no authentication prompt occurred. Resume dedup and all digest checks passed at six total trajectories. The deterministic evaluator reports INCONCLUSIVE.

### 2026-08-25T10:36:00Z

EXP-60C-REPAIR-001 used the real failed notes fixture from DOGFOOD-20260822-005043-16adca. Baseline failed without repair. Candidate failed its first exact classification and received exactly one evidence-backed remediation grounded in the preserved server-health connection-refused evidence; that remediation also failed exact verification. Successful repair remains NOT_OBSERVED. EXP-60C-DECOMPOSITION-001 compared one direct extraction with two real bounded provider subtasks and exact reconciliation; both passed, while decomposition took approximately twice the latency. Resume dedup and digest checks passed after each experiment at eight and ten trajectories respectively. Both evaluations are INCONCLUSIVE.

### 2026-08-25T10:45:00Z

EXP-60C-REVIEW-001 used a different historical defect class: an empty correctness reviewer artifact paired with an empty findings list. The general reviewer missed the exact oracle; the correctness plus security topology produced a confirmed exact finding. EXP-60C-COMPOSITION-001 compared Claude planning and implementing with Claude planning plus Codex implementing; both passed the same exact verifier and exposed no model or cost. Resume dedup and digest checks passed at twelve and fourteen trajectories. All six experiments are complete and INCONCLUSIVE. The tracked results summarize every trajectory and limitation; full final verification was intentionally not run per campaign instruction.

### 2026-08-25T13:55:00Z

Post-campaign audit completed over all 22 current-schema live trajectories. Corrected EXP-60C-COMPOSITION-001 classification to NOT_OBSERVED for meaningful provider composition because the fixture was trivial exact fact extraction and the stored provider_events label both invocations role=review. Corrected EXP-60C-DECOMPOSITION-001 classification to NOT_OBSERVED for meaningful task decomposition because the candidate manufactured two subtasks for a simple two-fact extraction. Combined context evidence across #60B and #60C is baseline 0/3 passes, candidate 1/3 passes, with the single candidate pass confounded by directly supplied answer-bearing source context and both #60C candidate repetitions failing. Trajectory discovery ran two passes over all 22 trajectories and returned zero new observations; existing OBS-LOCAL-FIRST-7d18349014334fbe remained open at occurrence_count 2 with 2 evidence refs, no fingerprints were duplicated or reopened, and no strategy changed. Final coverage matrix, model/cost quality, cloud comparison details, repair search result, review topology metrics, evidence quality audit, invalid/rejected interpretations, competing explanations, and the readiness recommendation COLLECT_MORE_TRAJECTORY_EVIDENCE were written to documentation/evidence/reasoning/2026-08-25_comparability_results.yaml. README.md and change_log.md were updated to avoid claiming meaningful composition or decomposition. No raw trajectory or experiment JSON was modified.

## Handoff Packet

SESSION_INTERRUPTION:
    NONE

PROVIDER_INTERRUPTION:
    NONE

EXPERIMENT_CURRENTLY_INCOMPLETE:
    NONE

NEXT_EXPERIMENT:
    NONE until additional trajectory evidence is collected per the readiness recommendation COLLECT_MORE_TRAJECTORY_EVIDENCE.

NEXT_SAFE_ACTION:
    Commit and push the staged audit update as a durability checkpoint, then run the full verification matrix (Python, Go, lint, Bandit, SlopsLint, docs) before marking the PR ready for review.
