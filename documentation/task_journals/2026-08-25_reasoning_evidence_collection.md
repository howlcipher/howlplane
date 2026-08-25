# Milestone #60B: Bounded Reasoning Evidence Collection Campaign

## Durable State

STATUS:
    IN_PROGRESS

STARTING_MAIN:
    30b886211ce75dc132c8ef449ec857bf14df4b3d

WORKING_BRANCH:
    dogfood/reasoning-evidence-collection

PR:
    https://github.com/howlcipher/howlplane/pull/46

CURRENT_EXPERIMENT:
    EXP-60B-CONTEXT-002: PREREGISTERED

COMPLETED_EXPERIMENTS:
    NONE_FOR_60B

TRAJECTORY_COUNTS:
    EXISTING_LIVE: 2
    NEW_TRUSTWORTHY_60B: 0

PROVIDERS_OBSERVED:
    local_ollama

TASK_CLASSES_OBSERVED:
    test_improvement

KNOWN_FAILURES:
    The two existing live trajectories both record verification_failed.
    Phase 0 has no measurement failure. The full push gate emits preexisting
    third party Python, Bandit, and pdoc warnings despite a zero exit status.
    EXP-60B-CONTEXT-001 was rejected before provider execution because its
    descriptive task class is not in the production TaskSpec enum. It created
    no arm result and no trajectory. The other original definitions have the
    same planning defect and were not started.

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit and push the corrected preregistration checkpoint, then run the
    baseline and candidate arms of EXP-60B-CONTEXT-002 without repair.

LAST_VERIFIED_TESTS:
    Targeted #60A suite: 76 passed in 5.78 seconds.
    Initial push hook: 798 Python passed, all Go passed, flake8 passed,
    Bandit found no medium or high issue, Go build passed, docs generated.
    GitHub checks for 64957f8 all passed.

## Campaign Boundaries

This milestone collects evidence through the existing #60A machinery. It does
not add learned defaults, strategy profiles, exploration routing, model weight
changes, authority changes, repository scope changes, spend changes, or
production permissions.

The campaign is bounded to approximately six through ten new live trajectories
when provider availability permits. Missing dimensions will be reported as
NOT_OBSERVED rather than manufactured.

## Recovery State

CURRENT_BRANCH:
    dogfood/reasoning-evidence-collection

HEAD:
    f5990871b3b72e7f311287eea1448343c0daaacd

PR:
    https://github.com/howlcipher/howlplane/pull/46

EXPERIMENTS_COMPLETED:
    NONE_FOR_60B

EXPERIMENT_CURRENTLY_INCOMPLETE:
    EXP-60B-CONTEXT-001 is preserved at RUNNING with no arm results after
    pre-provider TaskSpec validation rejection. It is abandoned and replaced
    rather than mutated.

PROVIDERS_CURRENTLY_AVAILABLE:
    local_ollama service, model, and RAM probe passed. Claude Code, Codex,
    Devin CLI, and agy are installed and routable but quota quality is unprobed.

PROVIDERS_CURRENTLY_UNAVAILABLE:
    gemini_cli binary is missing.

EXACT_EVIDENCE_PATHS:
    documentation/evidence/reasoning/2026-08-24_context_canary.yaml
    documentation/evidence/reasoning/2026-08-25_campaign_plan.yaml
    logs/control_plane/milestone_60b_reasoning_evidence/experiments
    logs/control_plane/milestone_60b_reasoning_evidence/trajectories

EXACT_NEXT_EXPERIMENT:
    EXP-60B-CONTEXT-002

TESTS_RUN:
    Targeted #60A suite: 76 passed.
    Initial normal prepush hook: 798 Python passed, all Go passed, flake8,
    Bandit, Go build, and docs generation completed.
    GitHub checks: all six required checks passed for 64957f8.

REPOSITORY_STATUS:
    Intentional documentation changes for the preregistration correction.

## Progress Log

### 2026-08-25T01:04:59Z

Verified local main and origin/main at the required starting SHA. Verified the
working tree was clean before creating the campaign branch. Read the canonical
AGENTS.md, every repository rule, and the grounding, systems, environment,
development, quality, verification, commit, technical writing, automation, and
Python skills. Confirmed GitHub authentication, local Ollama, Python, Go,
flake8, Bandit, SlopsLint, adequate disk capacity, and adequate memory. Created
the branch and configured local SSH commit signing using the existing user key.

No experiment has started. The required targeted #60A tests remain the gate
before evidence inventory and preregistration.

### 2026-08-25T01:15:46Z

Pushed signed checkpoint 64957f8 through the normal hook and created draft PR
#46 immediately. GitHub reports the SSH signature as unknown_key, matching the
prior #60A SSH checkpoints; the commit object contains an SSH signature, but no
GitHub verified signature claim is made. All six GitHub checks passed.

Phase 0 passed all 76 focused #60A tests. Inventory found exactly two valid
current schema live trajectories and one valid live experiment. Legacy #59
campaigns and their verified replay fixtures remain explicitly classified and
are not promoted into current schema live counts. The ignored general trajectory
store contains deterministic test residue, including 40 digest mismatches that
production loading rejects. None count toward the campaign.

Preregistered three bounded experiments through the production coordinator.
Their immutable definitions and prediction digests are durable in the raw store
and mirrored in the tracked campaign plan. No provider arm has executed.

### 2026-08-25T01:26:21Z

Attempted to enter the first context arm, but production TaskSpec validation
rejected the descriptive task class `architecture_analysis` before any provider
call. The coordinator had already advanced EXP-60B-CONTEXT-001 to RUNNING, so
that immutable record remains visible with zero arm results and zero
trajectories. Preflight found the same plan defect in the unstarted routing and
review definitions. This is a campaign preregistration error, not a #60A
measurement defect: TaskSpec has consistently exposed nine canonical classes.

Preserved all three original records and preregistered replacement IDs through
the production coordinator using existing classes `other`, `bug_fix`, and
`test_improvement`. Hypotheses, strategies, providers, metrics, and
falsification criteria are unchanged. No provider has been invoked for #60B.
