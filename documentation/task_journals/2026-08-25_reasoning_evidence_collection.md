# Milestone #60B: Bounded Reasoning Evidence Collection Campaign

## Durable State

STATUS:
    FULL_VERIFICATION_PASSED_PENDING_FINAL_PUSH_AND_CI

STARTING_MAIN:
    30b886211ce75dc132c8ef449ec857bf14df4b3d

WORKING_BRANCH:
    dogfood/reasoning-evidence-collection

PR:
    https://github.com/howlcipher/howlplane/pull/46

CURRENT_EXPERIMENT:
    NONE

COMPLETED_EXPERIMENTS:
    EXP-60B-CONTEXT-002: INCONCLUSIVE
    EXP-60B-ROUTING-002: INCONCLUSIVE
    EXP-60B-REVIEW-002: FALSIFIED

TRAJECTORY_COUNTS:
    EXISTING_LIVE: 2
    NEW_TRUSTWORTHY_60B: 6

PROVIDERS_OBSERVED:
    local_ollama
    claude_code; provider available, model identifier not observable

TASK_CLASSES_OBSERVED:
    test_improvement
    other
    bug_fix

KNOWN_FAILURES:
    The two existing live trajectories both record verification_failed.
    Phase 0 has no measurement failure. The full push gate emits preexisting
    third party Python, Bandit, and pdoc warnings despite a zero exit status.
    EXP-60B-CONTEXT-001 was rejected before provider execution because its
    descriptive task class is not in the production TaskSpec enum. It created
    no arm result and no trajectory. The other original definitions have the
    same planning defect and were not started.
    EXP-60B-CONTEXT-002 baseline failed exact verification after inventing the
    threshold and miner names. The candidate passed. The one sample per arm
    evaluation remains INCONCLUSIVE.
    Both EXP-60B-ROUTING-002 arms failed first-pass verification and their one
    bounded remediations also failed. Claude was available; the failures are
    output-quality evidence, not provider-availability evidence.
    The EXP-60B-REVIEW-002 candidate topology completed both reviews but missed
    the known identifier defect and generated two false positives. The single
    correctness-reviewer baseline detected the defect. The candidate is
    FALSIFIED for this fixture only.

CURRENT_BLOCKERS:
    NONE

NEXT_SAFE_ACTION:
    Commit the final evidence and verification record, push through the normal
    gate, observe GitHub CI, mark the draft PR ready, and merge only if policy
    permits.

LAST_VERIFIED_TESTS:
    Targeted #60A suite: 76 passed in 5.78 seconds.
    Initial push hook: 798 Python passed, all Go passed, flake8 passed,
    Bandit found no medium or high issue, Go build passed, docs generated.
    GitHub checks for 64957f8 all passed.
    Correction push hook at 9533c40: 798 Python passed, all Go passed,
    flake8 passed, Bandit found no medium or high issue, Go build passed,
    and docs generated with the previously recorded third party warnings.
    Final explicit matrix: 798 Python passed in 123.54 seconds; Go test, Go
    build, go vet, flake8, Bandit, SlopsLint check and origin/main ratchet,
    docs build, and diff check all passed. SlopsLint remained at 14 source and
    29 test clones with no ceiling or ignore change.

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
    85c8f22dc0d64e9a54790ea56adef2ecaaaea9a3

PR:
    https://github.com/howlcipher/howlplane/pull/46

EXPERIMENTS_COMPLETED:
    EXP-60B-CONTEXT-002
    EXP-60B-ROUTING-002
    EXP-60B-REVIEW-002

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
    documentation/evidence/reasoning/2026-08-25_campaign_results.yaml
    logs/control_plane/milestone_60b_reasoning_evidence/experiments
    logs/control_plane/milestone_60b_reasoning_evidence/trajectories

EXACT_NEXT_EXPERIMENT:
    NONE; run evidence quality audit and full verification

TESTS_RUN:
    Targeted #60A suite: 76 passed.
    Initial normal prepush hook: 798 Python passed, all Go passed, flake8,
    Bandit, Go build, and docs generation completed.
    GitHub checks: all six required checks passed for 64957f8.

REPOSITORY_STATUS:
    Intentional tracked evidence, coverage, quality, and decision-gate changes.

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

### 2026-08-25T01:32:24Z

Completed EXP-60B-CONTEXT-002 through the production coordinator. Both arms
used local Ollama with the same task, acceptance criteria, exact verifier, and
no repair. The task-only baseline invented a threshold and three unregistered
miner names, so it failed. The bounded relevant-source candidate returned the
correct threshold and all four miners in source order and passed after the
preregistered whitespace normalization. Observable latencies were 5.7 and
6.264 seconds; cost was not observable and was not inferred.

Deterministic evaluation returned INCONCLUSIVE with one sample per arm despite
the candidate's directional win. Both trajectory digests and the experiment
prediction digest validate. A completed-run resume check left the trajectory
count unchanged at two and did not call the executor.

### 2026-08-25T01:41:51Z

Completed EXP-60B-ROUTING-002 through the production coordinator using the
same historical agy quota fixture, prompt, context scope, and exact verifier.
The local-first baseline and Claude-first candidate both failed first-pass
verification. Each received the one preregistered remediation. Both repairs
also failed: they used unsupported quality labels, and the candidate repair
added prose. The failures are classified as reasoning output failures because
both providers completed their calls; there was no availability failure.

The two trajectories preserve one live repair attempt each, with zero
successful repairs. Observable total latencies were 15.075 and 31.037 seconds.
Claude's model identifier and all costs were unobservable and were not inferred.
Deterministic evaluation returned INCONCLUSIVE. Both trajectory digests and the
experiment prediction digest validate, provider commands redact prompts, and a
resume check left the trajectory count unchanged at four.

### 2026-08-25T01:47:34Z

Completed EXP-60B-REVIEW-002 through the production review runner and parser.
The single correctness reviewer detected the exact historical identifier
delimiter defect and passed the deterministic oracle. The correctness plus
regression topology completed both reviews but missed the defect, produced two
false positives, and failed. The candidate reviewers naturally disagreed:
correctness said initiation-only language needed no fix, while regression
suggested adding a success or failure assertion that the acceptance criteria
explicitly prohibit. Exact acceptance evidence reconciles the disagreement;
both candidate findings are dismissed with recorded reasons.

Deterministic evaluation returned FALSIFIED because candidate verification was
0.0 versus baseline 1.0. This is fixture-specific evidence, not a universal
topology preference. All trajectory and experiment digests validate, provider
commands redact prompts, and completed-run resume held six trajectories.

Ran trajectory discovery across two #60A and six #60B records. It created one
open fingerprinted observation from the two successful low-risk local runs.
Two identical repeat discovery passes returned zero new observations and kept
the store at one. No observation was reopened or automatically adopted.

### 2026-08-25T01:53:16Z

Completed the final quality audit. All six #60B trajectories load through the
production store and validate their content digests, linked immutable strategy
definitions, provider and reviewer attribution, verification evidence, repair
counts, and tracked evidence digests. Sixteen file evidence references exist,
nine provider events are attributed, three reviews completed, and two repair
cycles are present. No hidden reasoning field, raw prompt, unredacted Claude
command, secret-like value, malformed #60B trajectory, or duplicate resume was
found.

The three invalid original preregistrations remain visible and excluded. Forty
pre-campaign deterministic test artifacts with digest mismatches remain rejected
by production loading and excluded from live counts. The decision gate is
COLLECT_MORE_TRAJECTORY_EVIDENCE because all comparisons have one sample per
arm, cloud-provider coverage has one primary trajectory, no repair succeeded,
and decomposition and provider-composition evidence remain unobserved.

### 2026-08-25T01:57:31Z

The explicit final verification matrix passed: 798 Python tests, all Go tests,
Go build, go vet, flake8 with zero selected errors, Bandit with no medium or
high issue, SlopsLint classification and enforcement, the SlopsLint ratchet
against origin/main, docs generation, and diff hygiene. The committed SlopsLint
ceilings remain unchanged at 14 source and 29 test clones. The only warnings
were the already recorded third party Pydantic, pdoc, and Bandit parser output.

Durability so far: six signed checkpoint commits and six normal pushes, no
handoff and no provider or AI session limit. The final checkpoint and its
normal push remain before GitHub CI and PR readiness.
