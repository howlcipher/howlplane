# Factory 24/7 Acceptance and Falsification

## Status

Acceptance harness ready for integration testing. Factory implementation not
accepted for unattended operation.

This report records the acceptance snapshot taken on 2026-08-31 against:

* `origin/main` at `47f5d741e58c70ab0fd46c0ce398c3900d9ffea2`
* pull request #67, persistent-operation architecture carveout
* pull request #68, durable work items and portfolio selection
* the concurrent `feat/factory-supervisor-slice` worktree, read only

The tests invoke no hosted provider and perform no GitHub mutation.

## Current Main Readiness

Current `main` is a governed single-task and bounded-campaign engine. It has
durable task checkpoints, provider capacity state, bounded provider failover,
authority envelopes, human-boundary gates, review and verification, Git and
pull-request integration, CI observation, and crash recovery for governed task
runs.

Current `main` does not provide `ai factory start`, a persistent factory
process, a durable work portfolio, capability reuse, repository proposals, or
factory status. Those surfaces remain in open or uncommitted work.

## Architecture Choice

The acceptance change is a tests-only black-box overlay. Factory-specific
tests skip on `main` until the factory modules exist, while harness self-tests,
authority tests, Git and CI tests, and resource-growth tests execute on `main`.
The same test files can be overlaid on the concurrent factory worktree without
copying its supervisor into this branch.

| Option | Advantages | Costs |
| --- | --- | --- |
| Tests-only black-box overlay | Isolated, cherry-pickable, does not compete with the supervisor, exercises real interfaces | Factory-specific contracts skip until implementation lands |
| Stack the tests on the implementation branch | Immediate native CI execution | Couples two engineers' branches and makes independent review harder |
| Build a reference supervisor in tests | Fully self-contained | Duplicates the production policy and can prove the wrong implementation |

The first option is used because it preserves worktree isolation and tests the
real implementation when present.

## Acceptance Harness

`tests/factory_acceptance_harness.py` provides:

* `FakeClock`, with explicit advance and injected sleep
* `ScriptedProviderPool`, with deterministic hosted and local capacity transitions
* non-generative provider readiness rechecks
* `CrashInjector`, with one-shot named durable-boundary failures
* `FakeAuthority`, with repository scope, explicit allows, and never-delegatable denies
* `FakeRepository`, with stable branch and pull-request idempotency keys
* CI state and merge-gate simulation
* `ScriptedDispatcher`, which returns governed-task outcomes without scheduling policy
* duplicate-dispatch and bounded-wait assertions

The harness contains no supervisor, portfolio selector, authority policy, or
provider-selection policy.

## Provider Failure Matrix

| Scenario | Expected result | Concurrent slice result |
| --- | --- | --- |
| Claude session exhausted, Codex available | Continue eligible Codex work | Pass |
| Hosted providers exhausted, local provider eligible | Continue local work | Pass |
| Every provider unavailable, no eligible local work | Persist `WAITING_FOR_PROVIDER` and exact next wake | Blocking failure: `run()` ignores persisted future `next_wake_at`, ticks immediately, and replaces it with failure backoff |
| `retry_after` reached | Non-generative recheck, then eligibility if healthy | Pass |
| Provider still unavailable | Bounded waits, no hot loop | Pass |
| Provider repeatedly reports exhaustion | Reuse capacity model and do not invoke it every tick | Blocking failure: ready work is selected before provider capacity is checked |

## Crash and Restart Matrix

Every required boundary is a named, one-shot harness crash point. The matrix
separates harness capability from production acceptance.

| Boundary | Harness | Production contract result |
| --- | --- | --- |
| Before observation collection | Injectable | Not yet bound to a supervisor seam |
| After observation persisted | Injectable | Not yet bound to a supervisor seam |
| After work-item admission | Injectable | Atomic object write exists; restart contract pending |
| After portfolio selection | Injectable | Not yet bound to a supervisor seam |
| Before dispatch | Injectable | Work item is persisted `IN_PROGRESS` before the dispatcher call |
| After dispatch begins | Injectable | Restart parks the retained item and does not duplicate dispatch |
| After governed task finishes, before portfolio update | Injectable | Blocking external reconciliation gap remains |
| While parking work | Injectable | Atomic work-item write exists; owner-request idempotency pending |
| While recording provider wait | Injectable | Atomic supervisor state write exists; persisted-wake handling fails |
| While creating a repository proposal | Injectable | Atomic proposal write by proposal ID exists |
| While updating capability registry | Injectable | Atomic capability write by capability ID exists |
| During idle wait | Injectable | Restart works, but `run()` does not honor the previously persisted wake first |

The restart test proves no duplicate dispatch when the process dies after the
durable `DISPATCHING` boundary. It does not claim exactly-once external
execution after a governed task or GitHub call has already completed.
The production supervisor does not yet expose an injected crash seam spanning
the full boundary list, so the matrix itself remains a strict expected failure
and unattended restart safety is not accepted by implication.

## Exactly-Once and At-Least-Once Semantics

| Operation | Realistic semantic | Required reconciliation |
| --- | --- | --- |
| Work-item transition | Atomic single-object update | Retain dispatch ID and reconcile any in-progress governed task |
| Capability registration | Idempotent overwrite by capability ID | Reject ID collisions and load the durable record on restart |
| Repository proposal | Idempotent overwrite by proposal ID | Derive proposal ID deterministically from the admitted need |
| Branch creation | At least once | Stable task branch plus remote observation before retry |
| Push | At least once | Compare local and remote branch SHA |
| Pull-request creation | At least once | Stable head branch, discover existing PR, recover number and URL |
| CI observation | Repeatable observation | Bind the verdict to the observed PR head SHA |
| Merge | At least once | Query PR merged state and verify merge SHA on remote main |

`GitIntegrationExecutor.query_execution_status()` can discover an existing
pull request by stable task branch. The governed marathon Git path does not call
that method before `execute()`, and the existing-PR result returns a message
without a durable receipt containing PR number and URL. A crash after GitHub
accepts `create_pull_request` can therefore replay the lifecycle without enough
metadata to resume at CI. This is `BLOCKING_24_7`.

## Portfolio Falsification

The suite asserts:

* owner direction preempts caps and lower-priority work
* parked and blocked work cannot win selection
* starvation changes ordering only within the applicable priority tier
* an empty portfolio returns `no_dispatchable_work`
* cap removal is detected by mutation-style fault injection
* maintenance and creative-experiment caps prevent category domination
* the aggregate non-product repository cap prevents one non-product repository
  from consuming the window
* renamed evidence references do not constitute materially new evidence

The last contract currently fails: `WorkItemStore.admit_evidence()` reopens an
item when only a new reference is supplied and the evidence fingerprint is
unchanged. This can resurrect rejected work without new evidence and is
`BLOCKING_24_7`.

The designated product repository is exempt from repository fairness and can
consume the entire dispatch window while another repository remains ready.
That violates the acceptance requirement that no one repository monopolize
the factory and is `BLOCKING_24_7`.

## Authority Invariants

The executing tests attempt to modify or enable:

* authority profiles and envelopes
* the human-boundary gate and bounded executor
* force push
* required-check bypass
* branch-protection weakening
* production deployment
* independent review and reconciliation
* deterministic verification
* hygiene policy enforcement
* the factory dispatch controller

Core authority files and explicit never-delegatable actions park correctly.
The force-push detector is mutation-tested by removing the never-delegatable
entry and constructing a deliberately weakened envelope; the assertion fails
as expected.

Review, reconciliation, verification, and hygiene-policy modules are not
classified as authority-enforcement self-modification on `main`. The factory
subtree protection exists in #68 but not on `main`. The wider policy surface is
`BLOCKING_24_7`: capability self-improvement must not be able to weaken the
mechanisms that judge future capability changes.

## Capability Reuse

| Case | Result |
| --- | --- |
| Exact verified interface match | Reused |
| Inactive or unverified record | Rejected by suitability check |
| Deprecated record | Rejected by suitability check |
| No suitable record | Does not claim reuse |
| Natural home, one consumer | Local project fix, no repository proposal |
| Three consumers, clear purpose, bounded maintenance, deterministic verification | Repository proposal eligible |
| Creative idea without evidence | Human decision |
| Reuse detector disabled by fault injection | Acceptance assertion fails |

Capability records still lack risk-domain and authority-domain compatibility,
do not enforce their recorded verification age, cannot discover a compatible
interface under a different capability ID, and cannot rank or reject multiple
compatible candidates deterministically. An otherwise verified capability can
therefore cross a security or authority boundary, or obvious reuse can be
missed. These are `BLOCKING_AUTONOMOUS_REPO_CREATION`.

## Repository Creation

A proposal is eligible only with no natural home, a clear purpose, bounded
maintenance, deterministic verification, evidence, and multiple consumers or
a strong lifecycle-isolation reason. The suite rejects a one-off local helper
and an unevidenced creative product idea.

The bootstrap contract must require, where applicable:

* README and purpose metadata
* AGENTS.md
* project manifest
* tests and lint
* CI and security scanning
* SlopsLint and hygiene policy
* branch-protection expectations
* changelog and versioning
* owner metadata
* HowlPlane discoverability
* capability registration

The current contract accepts an empty `bootstrap_plan`. That is
`BLOCKING_AUTONOMOUS_REPO_CREATION`.

The following deterministic evidence inputs also remain absent from the
proposal model: an existing external tool disposition, explicit owner direction
for a new repository, operational-burden evidence, and a separately reviewable
authority decision. Production repository creation must remain disabled until
these are represented.

## Owner-Need Discovery

The acceptance interface requires separate functions for explicit owner
direction and repeated inferred need. One isolated script execution must not
admit a reusable-tool need. Repeated equivalent evidence across independent
repositories may propose an inferred need.

No production factory discovery module currently exposes these deterministic
thresholds. The contract is a strict expected failure and is `BLOCKING_24_7`.

## CI and Pull-Request Lifecycle

The acceptance tests cover:

* existing PR discovery by stable branch
* failed CI
* cancelled CI
* pending CI
* absent required checks
* skipped required checks
* new commit after a prior CI verdict

Failure, cancellation, pending, and absence fail closed. Two merge blockers
remain:

1. A skipped required check is classified terminal green.
2. `GitIntegrationRecord` does not bind CI evidence to the PR head SHA, so a
   new commit cannot mechanically invalidate the prior verdict.

Both are `BLOCKING_AUTONOMOUS_LOW_RISK_MERGE`.

## Deterministic 72-Hour Simulation

The passing overlay scenario advances 72 hours in 0.52 seconds with zero live
provider quota. It contains:

* three repositories
* ten initial ready work items
* one blocked dependency
* one parked owner decision
* one post-restart owner-direction item
* owner direction, backlog, maintenance, self-improvement, creative,
  discovered-problem, and inferred-need origins
* Claude exhaustion at hour 1 and recovery at hour 6:01
* Codex exhaustion at hour 2:30 and recovery at hour 10
* local-provider continuation while hosted providers are unavailable
* one engineering failure
* one CI failure
* nine successful dispatches
* one verified capability reuse
* one evidence-qualified repository proposal
* one restart at hour 12

Final assertions:

* eleven unique dispatches and no duplicate dispatch
* owner work dispatched first
* two failures reported with their real reasons
* parked and blocked items retained
* local continuation observed
* recovered hosted capacity used
* capability reuse recorded
* exactly one repository proposal awaiting authority
* final state stable in `WAITING_FOR_WORK` or `WAITING_FOR_PROVIDER`

Portfolio caps remain respected across the dispatch window; every initially
ready item eventually runs without violating the owner-first assertion.

## Observability Contract

Factory status must answer, from structured durable state:

* whether the factory is alive
* current work and task IDs
* why it is idle
* recent completed and failed work
* parked work and owner requests
* exact next wake
* unavailable providers and cooldowns
* added or reused capabilities
* repository proposals awaiting authority

The concurrent status payload adds timestamps, current dispatch identity,
admission counts, recent completions and failures, provider wake conditions,
and proposal IDs. It still omits provider inventory, parked work details,
capability changes, and a direct idle reason. This is `BLOCKING_24_7` because an
operator cannot reconstruct the unattended interval accurately.

## Resource and Disk Growth

Live measurements on 2026-08-31:

| Category | Size or count |
| --- | --- |
| `logs/control_plane` | 156 MiB, 1,836 files |
| evidence ledger | 148,565,503 bytes, 156,943 lines |
| `.task_runs` | 13 MiB, 821 files |
| `.dogfood_runs` | 9.1 MiB, 1,497 files |

The #67 design snapshot recorded 139,870,255 bytes and 147,806 ledger lines.
The live source is larger, so this report uses the live measurements and flags
the design snapshot as an update candidate.

`EvidenceLedger.list_all_entries()` reads the whole JSONL and there is no
bounded iterator, cursor, derived index, or retention interface. Canonical
evidence must not be deleted to hide growth. A rebuildable index and bounded
query surface are required before factory status depends on the ledger. The
acceptance test is a strict expected failure classified `BLOCKING_24_7`.

Verification views have explicit cleanup status. No equivalent documented
retention contract was found for evidence-ledger segments, factory state,
provider scratch, or task-run history.

## Blocking Defects

### BLOCKING_24_7

1. Persisted provider wake is ignored on entry to `run()`.
2. Ready work is selected before provider capacity is checked.
3. The production supervisor lacks the full injected crash-boundary seam.
4. The designated product repository can monopolize the dispatch window.
5. Git lifecycle does not query external execution status before retry and
   cannot recover existing PR metadata.
6. Work can reopen on a renamed evidence reference without a new fingerprint.
7. Review, reconciliation, verification, and hygiene enforcement are outside
   the self-authority modification boundary.
8. Owner versus inferred-need discovery thresholds are absent.
9. Factory status cannot answer the complete unattended-operation questions.
10. Evidence-ledger query and growth are unbounded.

### BLOCKING_AUTONOMOUS_REPO_CREATION

1. Capability reuse has no risk/authority compatibility, freshness gate,
   compatible-interface discovery, or multiple-candidate resolution.
2. Repository bootstrap safety fields are optional and an empty plan is valid.
3. External-tool reuse, explicit owner direction, and operational burden are
   not deterministic proposal inputs.

### BLOCKING_AUTONOMOUS_LOW_RISK_MERGE

1. A skipped required check authorizes merge.
2. CI evidence is not bound to the PR head SHA.

### NICE_TO_HAVE

No speculative nice-to-have findings were added.

## Test Sensitivity

The suite intentionally injects or removes protections and confirms the
corresponding assertion fails for:

* zero-second busy-loop wait
* duplicate PR creation after an accepted external call
* provider readiness before `retry_after`
* never-delegatable force push
* portfolio self-improvement cap
* capability reuse lookup
* new-repository evidence threshold

Strict expected failures turn into CI failures if the implementation begins to
pass, forcing the defect marker to be reviewed and removed instead of silently
leaving stale xfails.

## Factory Implementer Interface Contract

The production slice must satisfy these assumptions:

1. Inject clock and sleep; read persisted `next_wake_at` before the first tick.
2. Persist observation, admission, selection, dispatch identity, and work-item
   transition boundaries atomically and reconcile retained dispatch IDs.
3. Ask the provider pool for task-eligible capacity before dispatch.
4. Persist work terminal or parked state before clearing current dispatch.
5. Continue from authority and dependency waits when unrelated work is ready.
6. Query external Git and GitHub truth before repeating any side effect.
7. Bind CI evidence to PR head SHA and require explicit terminal success.
8. Treat capability reuse as verified interface plus risk and authority domain.
9. Keep repository creation proposal-only and require the bootstrap safety
   contract.
10. Expose complete structured operator status without scanning unbounded
    canonical evidence on every call.
