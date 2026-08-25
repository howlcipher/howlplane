# Milestone #60B: Bounded Reasoning Evidence Collection Campaign

## Durable State

STATUS:
    IN_PROGRESS

STARTING_MAIN:
    30b886211ce75dc132c8ef449ec857bf14df4b3d

WORKING_BRANCH:
    dogfood/reasoning-evidence-collection

PR:
    PENDING_DRAFT_CREATION

CURRENT_EXPERIMENT:
    NONE

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
    No Phase 0 measurement failure is known, but this session has not yet run
    the targeted #60A suite.

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Push this signed checkpoint, create the draft PR, then run the targeted
    #60A measurement suite before inventorying or collecting evidence.

LAST_VERIFIED_TESTS:
    NOT_RUN_THIS_SESSION

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
    30b886211ce75dc132c8ef449ec857bf14df4b3d

PR:
    PENDING_DRAFT_CREATION

EXPERIMENTS_COMPLETED:
    NONE_FOR_60B

EXPERIMENT_CURRENTLY_INCOMPLETE:
    NONE

PROVIDERS_CURRENTLY_AVAILABLE:
    local_ollama binary is present; model and service availability remain to be
    checked without estimating.

PROVIDERS_CURRENTLY_UNAVAILABLE:
    NOT_YET_INVENTORIED

EXACT_EVIDENCE_PATHS:
    documentation/evidence/reasoning/2026-08-24_context_canary.yaml

EXACT_NEXT_EXPERIMENT:
    NONE_UNTIL_PHASE_0_AND_PREREGISTERED_PLAN_COMPLETE

TESTS_RUN:
    NONE_THIS_SESSION

REPOSITORY_STATUS:
    Intentional documentation changes for the initial checkpoint.

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
