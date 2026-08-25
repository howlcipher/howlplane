# Milestone #61: Configurable AI Resource Pool

## Architecture and Recovery Header

STATUS:
    IN_PROGRESS

STARTING_MAIN:
    dea91b187c6a5117db09eb9755b6cafbb428038f

WORKING_BRANCH:
    feat/configurable-ai-resource-pool

PR:
    https://github.com/howlcipher/howlplane/pull/49

CURRENT_PHASE:
    test_driven_resource_foundation

LAST_COMPLETED_PHASE:
    architecture_audit

LATEST_CHECKPOINT_COMMIT:
    2ea6fbbe0fdee815ecadb44ab3827425fd0a2fcb

PROVIDER_ARCHITECTURE_DECISIONS:
    Extend the existing provider pool, routing, capability, review, trajectory,
    evidence, and persistence abstractions. Do not create a parallel provider
    management system.
    Treat operator permission, runtime readiness, capability, capacity,
    economics, cognitive recommendation, and authority as separate decisions.
    Select provider resources by distinct provider, interface, resource, and
    observed model identities. Never invent an unobserved model identifier.
    Keep the future cognitive optimization seam advisory and subordinate to
    hard policy.
    Extend AgentProfile with provider_id, interface_id, resource_id, observed
    model identity, locality, economics, role eligibility, and capability
    facts while retaining legacy fields and agent IDs.
    Keep ProviderPoolManager as the single runtime readiness, capacity,
    failure normalization, recovery, reviewer diversity, and final resource
    selection owner.
    Keep TaskRouter as deterministic cognitive recommendation. It ranks only
    candidates already admitted by hard policy and cannot enable resources,
    egress, spend, capability, or authority.
    Preserve AgentBackendRegistry as the adapter registry and add generic
    readiness metadata there without generation probes.
    Extend ExecutionTrajectory through an explicit compatibility migration;
    do not add an analytics log.
    Treat the unwired Go internal/provider interface as a compatibility
    surface, not a second production provider manager.

FILES_CHANGED:
    README.md
    change_log.md
    documentation/task_journals/2026-08-25_configurable_ai_resource_pool.md

TESTS_RUN:
    git fetch origin
    gh pr view 48 merge verification
    git merge-base ancestry verification
    git status and post-merge log inspection
    pre-push: 798 Python tests passed with one dependency warning
    pre-push: Go tests passed
    pre-push: Go build passed
    pre-push: flake8 passed
    pre-push: Bandit found no medium or high severity issues
    pre-push: documentation generation passed with dependency annotation warnings
    architecture audit: source traced across registry, backends, pool, router,
    governed orchestration, reviewer failover, marathon, CLI, doctor,
    configuration, atomic I/O, evidence ledger, and trajectory persistence

KNOWN_FAILURES:
    NONE

UNVERIFIED_WORK:
    Resource configuration, selection, capacity persistence, CLI, trajectory,
    and egress acceptance tests have not yet been implemented.

NEXT_SAFE_ACTION:
    Write failing deterministic tests for resource identity, configuration,
    readiness, policy, capability, capacity, economics, recommendation,
    onboarding, subset operation, and structured no-resource outcomes.

WORKTREE_STATE:
    Clean after the initial signed checkpoint and enforced pre-push suite. The
    checkpoint contains documentation and recovery metadata only.

## Start Gate Evidence

GitHub reported PR #48 as merged at 2026-08-25T15:27:50Z with merge commit
`dea91b187c6a5117db09eb9755b6cafbb428038f`. Git ancestry verification proved
that commit is reachable from `origin/main`, and `git pull --ff-only origin
main` reported the local branch current before feature branch creation.

## Architecture Options To Evaluate

| Option | Benefits | Costs | Initial disposition |
| --- | --- | --- | --- |
| Extend the current shared provider pool | Preserves failover, review, evidence, and runtime state semantics | Requires careful compatibility work across existing consumers | Preferred pending source audit |
| Add a separate resource selector beside the provider pool | Isolates new code initially | Creates split capacity truth and forces every consumer to reconcile two systems | Rejected by milestone invariant |
| Replace the current provider pool | Offers a clean conceptual model | Risks regressions in #60A through #60D and breaks serialized history | Rejected by milestone invariant |

## Recovery Contract

A fresh agent must read this journal, confirm the branch and PR head, inspect
the latest signed commit, run the recorded targeted verification, and perform
only `NEXT_SAFE_ACTION`. Durable Git state, the PR, tests, trajectories, and
evidence are authoritative. Conversation history and private model reasoning
are not required for recovery.

## Progress Log

### 2026-08-25T16:30:00Z

Verified the #60D hard start gate against both GitHub PR state and Git ancestry.
Synchronized a clean local `main` at the actual post-#60D merge SHA, created
the feature branch, loaded all repository rules and relevant skills, and
created the durable Milestone #61 recovery header.

### 2026-08-25T15:42:00Z

Created signed checkpoint `2ea6fbb`, passed the repository pre-push suite,
pushed the branch, and opened draft PR #49. The hook reported pre-existing
pdoc dependency annotation warnings and skipped `golangci-lint` and `gosec`
because those optional executables are not installed; all enforced commands
returned success.

### 2026-08-25T15:50:00Z

Completed the production architecture audit. The existing Python provider pool
already shares availability state with marathon implementation and governed
review failover, and it correctly separates several engineering failures from
quota exhaustion. It currently probes every registered backend at construction,
stores only provider-level identity, uses static priority tables, snapshots
only status strings into campaign state, and does not restore that state on
restart. Agent registry availability is declarative rather than operator
authorization, route visibility does not use live capacity, and trajectory
identity stops at agent/provider plus optional observed model metadata. The
new work will extend these seams rather than replace them.

The repository also contains `internal/provider`, a Go adapter interface and
health router with no concrete production adapters and no `ai` command wiring.
Promoting it would create a second state mechanism beside the established
Python orchestration path, so it remains an inert compatibility surface.

Architecture alternatives were evaluated as follows.

| Decision | Benefits | Costs | Result |
| --- | --- | --- | --- |
| Put all new behavior directly in `provider_pool.py` | One visible module | Excessive coupling and a likely hygiene regression | Rejected |
| Create a separate resource manager | Easy greenfield modeling | Split readiness and exhaustion truth across subsystems | Rejected |
| Add focused resource modules used by `ProviderPoolManager` | Keeps typed policy and decision contracts cohesive while preserving one production state owner | Requires compatibility adapters for current agent-oriented APIs | Selected |
| Make `TaskRouter` perform hard policy and capacity filtering | Fewer call sites initially | Conflates cognitive recommendation with authority and economics | Rejected |
| Keep `TaskRouter` advisory after pool filtering | Stable future cognitive seam and enforceable precedence | Requires a structured selection decision passed between layers | Selected |
