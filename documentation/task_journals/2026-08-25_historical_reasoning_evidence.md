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
    FIX-39-codex-implementation-002: successful repair (initial failed, final passed)
    FIX-35-claude_code-implementation-001: successful repair (initial failed, final passed)
    FIX-39-composition-claude-impl-001: provider composition (plan + implement, both claude_code) passed

TRAJECTORY_COUNTS:
    EXISTING_CURRENT_SCHEMA_LIVE: 22
    NEW_TRUSTWORTHY_60D: 4
    TOTAL_LIVE_CURRENT_SCHEMA: 26

COVERAGE_MATRIX:
    Current 60D successful repairs: 3
    Multi-provider comparison: FIX-39 held constant across claude_code and codex; both passed.
    Provider composition: FIX-39 with claude_code plan artifact and claude_code implementation passed.
    Model metadata: exact hosted model IDs not yet observed from CLI outputs.

PROVIDER_STATES:
    claude_code: installed, authenticated, edits files when --allowedTools is provided
    codex: installed, authenticated, requires --approve-for-me (mutually exclusive with --sandbox)
    agy: installed
    local_ollama: installed and routable (qwen2.5-coder:7b-instruct)

CURRENT_BLOCKERS:
    Codex exec mode does not expose model metadata or execution transcript in stdout/stderr.

NEXT_SAFE_ACTION:
    Add context-experiment support to runner, run review topology on FIX-35 patch, attempt FIX-37 or FIX-35 decomposition, and finalize coverage matrix.

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
