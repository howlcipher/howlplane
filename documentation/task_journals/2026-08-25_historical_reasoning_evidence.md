# Milestone #60D: Historical Engineering Replay Lab

## Durable State

STATUS:
    EVIDENCE_COLLECTION_IN_PROGRESS

STARTING_MAIN:
    47e78e2f09876c57ecd2e138f4590a22e9e02e0d

WORKING_BRANCH:
    dogfood/historical-reasoning-evidence

PR:
    https://github.com/howlcipher/howlplane/pull/48

CURRENT_HEAD:
    bfcf023

STAGED_WORK:
    documentation/task_journals/2026-08-25_historical_reasoning_evidence.md
    dogfood/historical-reasoning-evidence/run_fixture_repair.py
    dogfood/historical-reasoning-evidence/fixture_catalog.yaml

CURRENT_EXPERIMENT:
    NONE

COMPLETED_EXPERIMENTS:
    FIX-39-claude_code-implementation-002: successful repair (initial failed, final passed)
    FIX-39-claude_code-implementation-003: successful first-pass repair
    FIX-39-claude_code-implementation-004: successful first-pass repair
    FIX-39-claude_code-implementation-005: successful first-pass repair with observed model metadata
    FIX-39-claude_code-implementation-006: successful first-pass repair with observed model metadata
    FIX-39-codex-implementation-002: successful repair (initial failed, final passed)
    FIX-35-claude_code-implementation-001: successful repair (initial failed, final passed)
    FIX-41-claude_code-implementation-001: successful repair (initial failed, final passed, task_class=infrastructure)
    FIX-39-composition-claude-impl-001: provider composition (plan + implement, both claude_code) passed
    FIX-35-decomposition-claude-001: task decomposition (orchestrator subtask + marathon subtask) passed
    FIX-39-context-baseline: 3 claude_code baseline runs, all passed
    FIX-39-context-candidate: 3 claude_code runs with documentation/CONTROL_PLANE.md context, all passed
    FIX-35-review_topology-correctness_only: 0 findings on the final patch
    FIX-35-review_topology-correctness_regression: 2 findings on the final patch (both medium, both from regression-reviewer)

TRAJECTORY_COUNTS:
    EXISTING_CURRENT_SCHEMA_LIVE: 22
    NEW_TRUSTWORTHY_60D: 13
    TOTAL_LIVE_CURRENT_SCHEMA: 35

COVERAGE_MATRIX:
    Current 60D successful repairs: 5 (FIX-39 x2 providers, FIX-35, FIX-41)
    Failed repairs: 4 (FIX-39-claude-001 edited tests; FIX-39-codex-001 bad CLI flags; FIX-39-composition-codex-impl-001/002 produced no edits)
    Multi-provider comparison: FIX-39 held constant across claude_code and codex; both passed.
    Provider composition: FIX-39 with claude_code plan artifact and claude_code implementation passed.
    Task decomposition: FIX-35 with explicit orchestrator + marathon subtasks passed.
    Context experiment: FIX-39, 3 baseline vs 3 candidate runs, all passed; context did not change outcome.
    Review topology: FIX-35 patch, correctness_only found 0 findings; correctness_regression found 2 medium findings.
    Fourth task class: infrastructure observed via FIX-41 workflow-trigger fix.
    Model metadata: claude-sonnet-5 observed in recent runs; Codex GPT-5 family observed via --json probe.

PROVIDER_STATES:
    claude_code: installed, authenticated, edits files when --allowedTools is provided, exposes canonicalModel in JSON output
    codex: installed, authenticated, requires --approve-for-me (mutually exclusive with --sandbox), exposes model family but not exact snapshot in exec --json
    agy: installed
    local_ollama: installed and routable (qwen2.5-coder:7b-instruct)

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit campaign artifacts, run full verification, finalize readiness report.

## Campaign Boundaries

This milestone closes remaining evidence gaps using REAL HISTORICAL ENGINEERING FAILURES rather than trivial synthetic tasks. Defects come from merged HowlPlane history. Verification uses the actual deterministic tests that proved those defects. Trajectories are classified LIVE for current execution, but underlying defects are labeled HISTORICAL_REAL_FIXTURE.

Architecture redesign of #60A is prohibited absent demonstrated instrumentation defects.

No StrategyPerformanceProfile, learning layer, exploration, drift routing, or cognitive optimization machinery is added.

## Preflight Evaluation

TASK:
    Milestone #60D historical engineering replay lab.

AGENT_AND_MODEL:
    Devin CLI with Adaptive routing; exact serving model not asserted unless exposed by provider evidence.

SKILLS_ROUTED:
    devin_cli
    route_task
    review_change
    reconcile_reviews
    verify_change
    ship_check

RISK_AND_REASONING:
    Medium risk evidence campaign using deterministic verification and live providers. Historical defects replayed in isolated worktrees.

FREE_TOOLS:
    Existing repository control plane, local Ollama, installed provider CLIs, Git, GitHub CLI, Python, Go, flake8, Bandit, SlopsLint, git worktree.

## Progress Log

### 2026-08-25T10:00:00Z

Verified main at 47e78e2f with clean worktree and matching origin. Created working branch dogfood/historical-reasoning-evidence and this journal. No fixture catalog yet; subagent exploring historical PRs #35, #37, #39, #41, #42, #43 in parallel.

### 2026-08-25T14:35:00Z

Verified the historical fixture catalog against real repository commits. Worktrees created for FIX-35, FIX-37, and FIX-39 base SHAs; applying only the future test patch at each base reproduces the historical failure. Initial Claude Code direct repair on FIX-39 passed the deterministic verifier. Codex direct repair on FIX-39 also passed after correcting the CLI invocation to use --approve-for-me without --sandbox. FIX-35 direct repair with Claude Code also passed, including both orchestrator.py and marathon.py changes. Provider composition experiment (Claude plan artifact + Claude implementation) on FIX-39 passed. Exact model metadata still not observed from CLI outputs. Composition with Codex as implementer failed because Codex did not emit file edits from the plan artifact; the runner currently captures no stdout/stderr from Codex exec.

### 2026-08-25T16:15:00Z

Completed remaining evidence collection. Added FIX-41 infrastructure fixture with a deterministic YAML-based verifier so the fourth task class is covered. Successful repairs now span FIX-39 (Claude and Codex), FIX-35 (Claude), and FIX-41 (Claude). Task decomposition on FIX-35 passed when the prompt explicitly split orchestrator wiring and marathon integration into ordered subtasks. Context experiment on FIX-39 ran 3 baseline and 3 candidate repetitions with documentation/CONTROL_PLANE.md as additional context; all passed, so the experiment is leakage-controlled but inconclusive on outcome. Review topology on the FIX-35 patch found 0 issues under a single correctness reviewer and 2 medium issues under correctness+regression, demonstrating that topology changes findings without invalidating the deterministic truth (tests pass). Claude JSON output exposes canonicalModel claude-sonnet-5 and cost; Codex exec --json exposes only the GPT-5 family, not an exact snapshot.
