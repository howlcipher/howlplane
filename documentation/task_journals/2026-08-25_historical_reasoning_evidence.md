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

## Final Readiness Report (Sections A-P)

### A. Repository

- Repository: /run/media/system/tallgeese/dev/howlplane
- Starting main SHA: 47e78e2f09876c57ecd2e138f4590a22e9e02e0d
- Campaign branch: dogfood/historical-reasoning-evidence
- Current campaign head: 4ec5fd1
- PR: https://github.com/howlcipher/howlplane/pull/48
- Worktree isolation: /run/media/system/tallgeese/dev/howlplane/.worktrees/ (Git ignored, campaign-owned names)
- CI Python: /run/media/system/tallgeese/dev/.ci_verify_venv/bin/python3
- All campaign worktrees are isolated from the primary worktree; no `git reset --hard` or force-push was used on the primary branch.

### B. Historical Fixture Catalog

Catalog: dogfood/historical-reasoning-evidence/fixture_catalog.yaml

| Fixture | PR | Class | Base SHA | Fix SHA | Used in |
|---|---|---|---|---|---|
| FIX-35 | #35 | bug_fix | dd66019fea14f1daca5fe18cc6adf27e9d377574 | 0bba5ed20e6a3b1a9139a22b8549b1b86015c03a | direct repair, decomposition, review topology |
| FIX-37 | #37 | bug_fix | ca7751f20938219d0e8bddf6bc03b0919daa4923 | 4dde0fb8375abf7c8acceb5e0a975e8cdd0736d0 | catalog only |
| FIX-39 | #39 | bug_fix | 5f7f3d6f3a71776468b35de963edcd43f50756a7 | 9ba628f9cb78623e21729369601057805cab2e23 | direct repair, multi-provider, composition, context experiment |
| FIX-41 | #41 | infrastructure | cb0527b8edd2bea935db1f57e33b57518ed42b2f | 0fc79c7f6bee8c03d2cbe0f8d345fca46f3fd004 | direct repair (fourth class) |
| FIX-42 | #42 | bug_fix | 8cd3d702915f4a86b22b2f5fcaa4c1333b1f1fec | 16a5d4109c9c00acd566c1c511eca5433e492f39 | catalog only |
| FIX-43 | #43 | test_improvement | d4d213ea6294ff64bf93cc663de27657460d7807 | 7ad4575dfbf56d68ea8774613f492a13e970556f | catalog only |

All defects are labeled HISTORICAL_REAL_FIXTURE. Test-only patches are applied to each historical base so the future production fix is not present during provider execution.

### C. Leakage Controls

- Historical cutoff SHA enforced per fixture (see catalog).
- Providers are started in isolated worktrees checked out at the base SHA.
- Only architecture docs and source files that existed at the base commit are available unless explicitly added via --context-docs.
- Future fix commits, PR bodies, and the historical diff itself are never included in the prompt.
- The runner audits changed files after each run; any run that edits disallowed files is classified as contaminated/scope failure.
- Context docs used in context experiment: documentation/CONTROL_PLANE.md at the base SHA.
- No answer-bearing future patches are supplied to providers.

### D. Successful Repair

Five successful repairs (initial verifier failed, provider edited allowed production files, final verifier passed):

1. FIX-39, claude_code, implementation role, base 5f7f3d6, verifier tests/test_control_plane.py::test_reviewer_requirements_replace_baseline. Edited src/control_plane/router.py. Run: FIX-39-claude_code-implementation-002.json (and repetitions 003-006).
2. FIX-39, codex, implementation role, base 5f7f3d6, same verifier. Edited src/control_plane/router.py. Run: FIX-39-codex-implementation-002.json.
3. FIX-35, claude_code, implementation role, base dd66019, verifier tests/test_review_integrity.py::test_orchestration_config_wires_provider_pool_into_review_cycle and two related tests. Edited src/control_plane/orchestrator.py and src/control_plane/synthesis/marathon.py. Run: FIX-35-claude_code-implementation-001.json.
4. FIX-41, claude_code, implementation role, base cb0527b, verifier tests/test_historical_fix41_ci_triggers.py. Edited .github/workflows/hygiene.yml, lint.yml, test.yml. Run: FIX-41-claude_code-implementation-001.json.
5. FIX-39, claude_code, composition implement role, base 5f7f3d6, same verifier as #1, using a pre-generated plan artifact. Edited src/control_plane/router.py. Run: FIX-39-composition-claude-impl-001.json.

Failed repairs retained as evidence:
- FIX-39-claude_code-implementation-8db18390: edited tests/test_control_plane.py instead of router.py.
- FIX-39-codex-implementation-001: CLI invocation error (--sandbox with --approve-for-me).
- FIX-39-composition-codex-impl-001/002: Codex did not emit file edits from the plan artifact.

### E. Multi-Provider Comparison

Task held constant: FIX-39 (reviewer_requirements replaces baseline).
Verifier held constant: tests/test_control_plane.py::test_reviewer_requirements_replace_baseline.
Prompt and allowed files held constant. Provider varied.

| Provider | Result | Model observed | Duration |
|---|---|---|---|
| claude_code | passed | claude-sonnet-5 | ~36-49s |
| codex | passed | GPT-5 family only | ~329s |

Both providers produced a correct minimal fix in src/control_plane/router.py. Codex latency was materially higher.

### F. Provider Composition

Task: FIX-39.
Planner: claude_code (role=plan, prompt asks for a concrete implementation plan only).
Implementer: claude_code (role=implementation, receives the plan artifact).
Plan artifact: dogfood/historical-reasoning-evidence/FIX-39-claude-plan-artifact.txt.
Result: implementer passed the verifier after editing only src/control_plane/router.py.
meaningful=true: planner produced a substantive file-level plan; implementer executed it; roles were distinct and correctly recorded.

Cross-provider composition (Claude plan -> Codex implement) was attempted twice and failed because Codex did not emit file edits from the plan artifact. These failures are recorded.

### G. Decomposition

Task: FIX-35.
Natural subtasks:
1. Wire provider_pool through OrchestrationConfig into GovernedTaskOrchestrator._run_governed_loop (src/control_plane/orchestrator.py).
2. Pass MarathonDogfoodEngine's provider_pool into the OrchestrationConfig it constructs (src/control_plane/synthesis/marathon.py).
Baseline: FIX-35-claude_code-implementation-001 direct repair (passed, same edits in one run).
Candidate: FIX-35-claude_code-decomposition-001 with the prompt explicitly ordering the two subtasks.
Result: candidate passed the full verifier after editing both allowed files.
meaningful=true: the subtasks correspond to distinct subsystems (orchestrator config/wiring vs campaign engine integration) and the prompt enforced an ordering; the combined fix matches the historical fix.

### H. Context Experiment

Task held constant: FIX-39. Provider held constant: claude_code. Verifier held constant. Allowed files held constant.
Baseline prompt: task description + failing verifier output.
Candidate prompt: baseline + documentation/CONTROL_PLANE.md at base SHA as additional architecture context.

| Arm | Runs | Outcome |
|---|---|---|
| Baseline | 3 | all passed |
| Candidate | 3 | all passed |

Leakage audit: CONTROL_PLANE.md at the base commit describes the control-plane architecture but does not contain the future fix for router.py; context is therefore not answer-bearing.
Result: context did not change the repair outcome. Evidence is leakage-controlled but inconclusive (no observed difference).

### I. Fourth Task Class

Class: infrastructure.
Fixture: FIX-41.
Legitimacy: The fix changes GitHub Actions workflow triggers to satisfy a ruleset requirement for documentation-only canary PRs. This is a CI/infrastructure change, not a product bug fix or test improvement.
Trajectory: FIX-41-claude_code-implementation-001.json (initial failed, final passed).

### J. Review Topology

Real patch reviewed: FIX-35 final patch (src/control_plane/orchestrator.py and src/control_plane/synthesis/marathon.py) produced by the claude_code repair.
Deterministic truth: tests/test_review_integrity.py pass on the reviewed patch.

Topology 1 (correctness_only): 0 findings.
Topology 2 (correctness_regression): 2 medium findings, both raised by regression-reviewer:
- F1-stale-architecture-comment: existing orchestrator comment no longer matches the new provider_pool wiring.
- F2-review-exhaustion-not-visible-to-failover-loop: review-side provider exhaustion is not folded back into marathon.py's failover decision.

Disagreement: correctness-only found nothing; regression reviewer found two non-blocker issues. Both findings are consistent with the patch being correct for the stated historical scope while leaving adjacent architectural tension.
Conclusion: review topology changes the findings without changing the deterministic truth.

### K. New Trajectories

13 new trajectories recorded in dogfood/historical-reasoning-evidence/runs/:

| ID | Class | Provider | Model | Outcome | Repair |
|---|---|---|---|---|---|
| FIX-35-claude_code-implementation-001 | bug_fix | claude_code | not observed | passed | success |
| FIX-35-claude_code-decomposition-001 | bug_fix | claude_code | claude-sonnet-5 | passed | success |
| FIX-39-claude_code-implementation-002/003/004 | bug_fix | claude_code | not observed | passed | success |
| FIX-39-claude_code-implementation-005 | bug_fix | claude_code | claude-haiku-4-5* | passed | success |
| FIX-39-claude_code-implementation-006 | bug_fix | claude_code | claude-sonnet-5 | passed | success |
| FIX-39-codex-implementation-002 | bug_fix | codex | GPT-5 family only | passed | success |
| FIX-39-composition-claude-impl-001 | bug_fix | claude_code | not observed | passed | success |
| FIX-39-context-001/002/003 | bug_fix | claude_code | not observed | passed | success |
| FIX-41-claude_code-implementation-001 | infrastructure | claude_code | claude-sonnet-5 | passed | success |
| review-topology-correctness_only | review | claude_code | not observed | N/A | N/A |
| review-topology-correctness_regression | review | claude_code | not observed | N/A | N/A |

*Run 005 model extraction selected a fast routing model from the JSON; the substantive response model is believed to be claude-sonnet-5 (confirmed by direct probe). This inconsistency is recorded as an evidence-quality caveat.

### L. Trajectory Discovery

No new observation fingerprints were added to trajectory_discovery.py; this was an evidence replay campaign, not a live production run. Existing observation OBS-LOCAL-FIRST-7d18349014334fbe remains open (occurrence count 2 in prior data). No duplicates were produced because each run uses a unique run_id and isolated worktree.

### M. Final Coverage Matrix

| Dimension | Count / Value |
|---|---|
| Total new trajectories | 13 |
| Existing current-schema live trajectories | 22 |
| Total live current-schema trajectories | 35 |
| Task classes observed | bug_fix (FIX-35, FIX-39), infrastructure (FIX-41), review (review topology) |
| Providers used | claude_code, codex |
| Model metadata observed | claude-sonnet-5 (Claude), GPT-5 family only (Codex) |
| Successful repairs | 5 |
| Failed repairs | 4 |
| Successful provider composition | 1 (Claude plan + Claude implement) |
| Failed cross-provider composition | 2 (Codex implement from Claude plan) |
| Successful decomposition | 1 (FIX-35) |
| Review topologies | 2 (correctness_only, correctness_regression) |
| Context repetitions | 6 (3 baseline + 3 candidate) |
| Avg latency FIX-39 Claude | ~38s |
| Avg latency FIX-39 Codex | ~329s |
| Cost observed | claude-sonnet-5 ~$0.17-0.34 per FIX repair; claude-haiku-4-5 ~$0.002 |

### N. Evidence Quality

- Invalid runs: 0 deleted; all failures retained.
- Contamination: 1 contaminated run (FIX-39-claude_code-implementation-8db18390 edited tests instead of allowed production file). Classified as scope/contamination failure.
- Future-fix exposure: 0. No future commit content was supplied to providers.
- Digest failures: none observed.
- Redaction: hidden chain-of-thought is not persisted; ExecutionTrajectory stores observable orchestration data only.
- Duplicate prevention: unique run_ids, isolated worktrees, deterministic fixture metadata.
- Caveat: early runs did not capture exact model IDs because the runner did not yet request JSON metadata from Claude.

### O. Tests/CI

Pre-push verification executed:
- Python tests: 798 collected, passed (pre-existing pdoc type-annotation warnings only).
- Go tests: passed.
- Flake8/Bandit/SlopsLint: passed on modified campaign scripts.
- Documentation generation: completed with existing pdoc warnings.
- GitHub push: successful from campaign branch to origin dogfood/historical-reasoning-evidence.

Focused checks:
- run_fixture_repair.py and run_review_topology.py pass flake8 error-class checks.
- fix41_test_only.patch applies cleanly at base SHA cb0527b and produces failing tests that pass after provider edits.

### P. Readiness

Evidence collected in this campaign:
- Successful repair: demonstrated across 3 historical fixtures and 2 providers.
- Meaningful provider composition: demonstrated (same provider, distinct roles).
- Meaningful task decomposition: demonstrated on a two-subsystem fix.
- Multi-provider comparison: demonstrated on FIX-39.
- Fourth task class: infrastructure demonstrated via FIX-41.
- Context comparison: leakage-controlled but inconclusive.
- Review topology: different topologies produced different findings while deterministic truth held.
- Model metadata: partially observed; Codex does not expose exact snapshot.

Remaining gaps (small, can be closed without a new milestone):
1. Cross-provider composition (Claude plan -> Codex implement) failed; needs one more attempt with a stronger Codex prompt or a Codex plan + Claude implement variant.
2. Context experiment is inconclusive; additional fixtures or harder tasks may be needed before context strategy conclusions can be drawn.
3. Codex exact model snapshot is not exposed by the CLI.
4. Sample size per cell is small (1-3 runs).

These gaps are not blockers for beginning structured evidence collection during Cognitive Optimization prep, but they do not yet meet the conservative threshold for declaring the reasoning infrastructure fully characterized. The experiment_evaluator requires at least 3 samples for SUPPORTED; several cells are at or below that line.

Final decision: COLLECT_MORE_TRAJECTORY_EVIDENCE

IS_ANOTHER_DEDICATED_EVIDENCE_MILESTONE_ACTUALLY_JUSTIFIED? NO
