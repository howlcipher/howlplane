# Milestone #60A — Reasoning Strategy Dogfooding

## Recovery State

STATUS:
    IN_PROGRESS

STARTING_MAIN:
    c9ec82c126973b86327a1da7773fa1219207d423

WORKING_BRANCH:
    feat/reasoning-strategy-dogfooding-canary

RECOVERED_LOCAL_COMMIT:
    1fdb6680aae604092ef3af56153d874e5376cb65

REMOTE_BRANCH_EXISTS:
    true at 6d6551ecb6b0a05aa23f1b9b700dbe40defb1890

CURRENT_PHASE:
    core durability implementation verified; checkpoint documentation in progress

LAST_COMPLETED_WORK:
    Normal push of signed commits ef5e74d and 6d6551e passed the complete pre-push
    hook and created the remote branch. Draft PR #45 is open. The shared experiment
    coordinator, artifact integrity policy, material-evidence discovery, campaign
    trajectory reference, and staged recovery tests are implemented and targeted
    verification is green.

UNCOMMITTED_FILES:
    README.md
    change_log.md
    documentation/adr/0005_reasoning_strategy_dogfooding_architecture_gap.md
    documentation/task_journals/2026-08-24_reasoning_strategy_trajectories.md
    src/control_plane/orchestrator.py
    src/control_plane/reasoning/__init__.py
    src/control_plane/reasoning/_json_store.py
    src/control_plane/reasoning/artifact_safety.py
    src/control_plane/reasoning/execution_trajectory.py
    src/control_plane/reasoning/experiment_coordinator.py
    src/control_plane/reasoning/experiment_evaluator.py
    src/control_plane/reasoning/reasoning_experiment.py
    src/control_plane/reasoning/strategy_registry.py
    src/control_plane/reasoning/trajectory_discovery.py
    src/control_plane/synthesis/campaign_state.py
    src/control_plane/synthesis/marathon.py
    tests/test_reasoning_experiment_recovery.py
    tests/test_reasoning_strategy_dogfooding.py

CURRENT_BLOCKER:
    None.

LAST_TEST_RESULT:
    Targeted reasoning, orchestrator, marathon, dogfood hardening, and hygiene
    regression: 114 passed. SlopsLint: source 14 / 14 and tests 29 / 29.
    Targeted flake8: clean. First remote checkpoint pre-push: 770 Python passed,
    all Go passed, flake8 and Bandit passed, Go build and docs build passed.

NEXT_SAFE_ACTION:
    Create and push the signed core durability checkpoint, then run one bounded
    live context-strategy experiment through the durable coordinator.

WORKTREE_STATE:
    DIRTY_INTENTIONAL

## Handoff Packet

TIMESTAMP:
    2026-08-24T21:37:48Z

CURRENT_PROVIDER_AGENT:
    OpenAI Codex / primary agent

BRANCH:
    feat/reasoning-strategy-dogfooding-canary

HEAD:
    6d6551ecb6b0a05aa23f1b9b700dbe40defb1890

BASE_MAIN:
    c9ec82c126973b86327a1da7773fa1219207d423

CURRENT_PHASE:
    core durability implementation verified; checkpoint documentation in progress

FILES_CHANGED:
    README.md
    change_log.md
    documentation/adr/0005_reasoning_strategy_dogfooding_architecture_gap.md
    tests/test_reasoning_strategy_dogfooding.py
    documentation/task_journals/2026-08-24_reasoning_strategy_trajectories.md
    src/control_plane/orchestrator.py
    src/control_plane/reasoning/
    src/control_plane/synthesis/campaign_state.py
    src/control_plane/synthesis/marathon.py
    tests/test_reasoning_experiment_recovery.py

TESTS_RUN:
    Targeted integration matrix: 114 passed. SlopsLint: python_src 14 / 14 and
    python_tests 29 / 29. Targeted flake8: clean. First remote checkpoint hook:
    770 Python passed, all Go passed, flake8 and Bandit passed, build and docs
    passed.

TESTS_NOT_RUN:
    Full repository verification has not rerun after the core durability changes;
    the normal push hook will run it for the checkpoint.

KNOWN_FAILURES:
    None in current targeted verification. Push attempt 1's Bandit B108 failure
    was repaired in signed commit 6d6551e; push attempt 2 completed normally.

EXACT_NEXT_STEP:
    Stage the exact core implementation, tests, README, changelog, ADR, and journal;
    create a signed checkpoint and push normally without bypassing the hook.

## Progress Log

### 2026-08-24T20:40:22Z

Recovered the local only branch and commit without discarding work. Verified
that origin/main remains at the expected starting SHA after fetching origin.
Verified that no remote branch named
feat/reasoning-strategy-dogfooding-canary exists. Preserved the two coherent
uncommitted import normalization edits reported by the prior agent.

### 2026-08-24T20:43:30Z

Independently reproduced SlopsLint at python_src 14 / 14 and python_tests
52 / 29. Reproduced the hygiene policy regression. Verified all 48 existing
reasoning tests pass when the nested verification processes are permitted.
Discovered an additional local only recovery artifact:
src/control_plane/reasoning/builtin_strategies.json. The broad repository JSON
ignore rule hides it from Git status, but committed strategy registry code reads
it during import. It must remain preserved until the tracked packaging defect is
resolved.

## Recovered Commit Audit

| Capability | State | Git proven implementation and remaining gaps |
| --- | --- | --- |
| ExecutionTrajectory | Partial | The commit adds a schema versioned dataclass, atomic JSON store, observable fields, redaction, hidden reasoning removal, and terminal orchestrator capture. It does not persist lifecycle stages, exception trajectories, bounded collections, stable resume identity, or actual campaign references. Random trajectory IDs permit duplicate accounting after a resumed run. Loading discards the persisted digest and recomputes it, so digest verification cannot detect persisted tampering. |
| ReasoningExperiment | Partial | The commit adds baseline and candidate snapshots, prediction fields, result fields, setter guards, digests, and an atomic store. No runner enforces persistence before execution, result recording can replace prior results, loading discards persisted digests, and no stage checkpoint supports crash recovery after definition, baseline, or candidate. `REQUIRES_MORE_EVIDENCE` is absent. |
| Strategy identifiers and versioning | Partial | The registry rejects an in process same identity digest conflict and snapshots persist config digests. Identity validation does not require the path suffix to match the separate version. Builtin definitions are in an ignored local JSON file absent from commit 1fdb668, so a clean checkout cannot import the package. Repository supplied registry data can still construct definitions; tests only prove that doing so does not mutate a separate fresh registry. |
| Experiment types | Representation only | All seven required type names are accepted by one dataclass, with two extra represented types. There is no shared execution mechanism or experiment runner. |
| Deterministic evaluation | Partial | Explicit metrics and falsification criteria determine outcomes without a model vote. Verification, first pass success, repairs, availability, latency, cost, and optional escape rate are represented. Confirmed defect evidence, reproducibility evidence, malformed criterion handling, and robust availability versus quality separation remain incomplete. |
| Anti self confirmation | Partial | Prediction digest checks protect setter based mutation after start, and failed candidate results can persist. Direct mutation is detected only before evaluation or result recording, stored digests are not verified on load, and result lists may be replaced after execution. Reviewer disagreement is preserved only as trajectory data. |
| Trajectory discovery | Partial | Four deterministic miners create fingerprinted evidence linked observations without changing routing. No challenge or experiment creation path is wired, campaign state recording is unused, and architecture omission uses a final status comparison inconsistent with the orchestrator's `complete` status. |
| Dedup and reopening | Partial | Observation fingerprints deduplicate active observations and completed experiment comparisons can be queried. Any new trajectory reference is treated as materially new evidence; no evidence digest or reopening reason proves materiality. Experiment accounting lacks a durable fingerprint. |
| Authority isolation | Test only plus structural separation | Reasoning types do not call authority mutation APIs, and tests show arbitrary strategy config cannot change an AuthorityEnvelope. There is no experiment runner boundary to validate because the runner is absent. |
| Redaction | Partial | Execution trajectories reuse evidence ledger string sanitization and strip named hidden reasoning fields. Experiment, strategy, and observation stores do not apply the same redaction or collection bounds. Provider and verification payloads can remain unbounded. |
| Crash and resume | Absent | Existing tests only save the same in memory trajectory or experiment twice. They do not simulate crashes after definition, baseline, or candidate, and do not prove cross agent reconstruction from Git, journal, and persisted artifacts. |
| Tests | Partial and hygiene failing | Forty eight tests cover many data representations and evaluator cases. Several assertions prove only comments or structural absence rather than governed behavior. The monolithic 1,266 line file introduces 52 active test clones against a ceiling of 29. |
| Documentation | Draft only | ADR 0005 documents the gap and proposed integration, including a lightweight runner that does not exist. README and change_log do not describe #60A yet. |

## Architecture Decision for Hygiene Recovery

Two viable test structures were evaluated. Splitting the monolith without shared
fixtures would improve navigation but preserve clone blocks and fail the policy.
Shared builders plus focused tests reduce real repetition and make each
capability independently maintainable, at the cost of a small helper API used
only by tests. The selected direction uses a narrow factory layer and
parameterization where cases share behavior and differ only in values. This
directly addresses the clone root cause without changing policy.

The repository already had a broader tests/_dogfood_test_helpers.py support
module, so the refactor extended that convention instead of adding a second
overlapping helper module. The tests remain in one focused file for this first
checkpoint because shared lifecycle factories removed the actual duplication;
later capability work may split modules only when their responsibilities grow.

For builtin strategy data, force tracking the ignored JSON was the smallest
diff but would leave future clean packaging implicit. Embedding the repetitive
definitions in Python would package automatically but would increase source
duplication and mix data with registry logic. The selected tracked YAML artifact
uses the recovered bytes unchanged, remains protected by the existing pinned
digest, and is explicitly included by setuptools. Its compact content remains
JSON compatible YAML, so no parser or dependency change is required.

### 2026-08-24T21:03:06Z

Completed hygiene recovery. Shared builders and experiment result lifecycle
helpers reduced python_tests active clones from 52 to 29. The two recovered
source import normalizations keep python_src at 14. No policy, ceiling, ignore,
threshold, or tombstone file changed. Converted the ignored local builtin
strategy artifact to tracked packaged YAML without changing its bytes or pinned
SHA256. Updated README and change_log for the in progress checkpoint. All 48
reasoning tests, the previously failing hygiene regression, SlopsLint, targeted
flake8, and git diff checks pass.

### 2026-08-24T21:15:59Z

Created signed checkpoint ef5e74debf7191840c935a955ccb89f9017ed38e.
Push attempt 1 ran the normal pre-push hook. It passed all 770 Python tests, all
Go tests, and flake8, then Bandit rejected hardcoded temporary directory use at
tests/test_reasoning_strategy_dogfooding.py. The push was aborted and no remote
branch was created. Replaced the shared /tmp/obs_test path with pytest's isolated
tmp_path fixture; verification is pending.

### 2026-08-24T21:17:14Z

Verified the Bandit repair: full Bandit scan reports no medium or high issues;
the reasoning suite plus hygiene regression passes 49 tests; SlopsLint remains
at python_src 14 / 14 and python_tests 29 / 29; targeted flake8 and git diff
checks are clean.

### 2026-08-24T21:37:48Z

Push attempt 2 completed through the normal pre-push hook and created the remote
branch at signed commit 6d6551ecb6b0a05aa23f1b9b700dbe40defb1890. The
hook passed 770 Python tests, all Go tests, flake8, Bandit, the Go build, and the
documentation build. Draft PR #45 was opened immediately afterward at
https://github.com/howlcipher/howlplane/pull/45.

Implemented the audited durability increment. Reasoning artifacts now share a
bounded, redacted, hidden-reasoning-free serialization policy; object IDs cannot
escape their stores; persisted trajectory, experiment, and strategy digests fail
closed on tampering. Strategy identity suffixes must match their versions.
ReasoningExperimentStore now permits only forward, append-only lifecycle
checkpoints while preserving immutable pre-registration fields and terminal
results.

Added one authority-free ReasoningExperimentCoordinator used by every supported
experiment type. It persists definitions before execution, uses stable
experiment/arm/sample trajectory identities, recovers trajectories written just
before a crash, prevents duplicate experiment accounting, and deterministically
evaluates only after both arms are durable. Fresh processes can report the exact
next phase from persisted artifacts. Crash simulations cover definition,
baseline, and candidate checkpoints. Campaign state now records the exact
trajectory ID returned by the governed orchestrator, and configured trajectory
persistence no longer swallows failures.

Discovery now computes material observable evidence fingerprints independent of
trajectory IDs and timestamps. Identical failed evidence cannot reopen a disposed
hypothesis; materially different evidence records a durable reopening reason and
history. Observations can enter a pre-registered challenge without changing
routing. The evaluator separates provider availability from engineering quality
and supports observed confirmed defects, escaped defects, and reproducibility.

Targeted reasoning, recovery, orchestrator, marathon, hardening, and hygiene
verification passes 114 tests. SlopsLint remains at python_src 14 / 14 and
python_tests 29 / 29, with no policy changes or tombstones. Targeted flake8 and
git diff checks pass. Full verification is delegated to the normal checkpoint
push hook next.
