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
    NONE

TRAJECTORY_COUNTS:
    EXISTING_CURRENT_SCHEMA_LIVE: 22
    NEW_TRUSTWORTHY_60D: 0
    TOTAL_LIVE_CURRENT_SCHEMA: 22

COVERAGE_MATRIX:
    Refer to prior #60C coverage matrix; #60D targets the sparse dimensions.

PROVIDER_STATES:
    claude_code: installed, authenticated status TBD
    codex: installed, authenticated status TBD
    agy: installed
    local_ollama: installed and routable (qwen2.5-coder:7b-instruct)

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit the fixture catalog and repair runner, then execute FIX-39 repair with live providers.

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
