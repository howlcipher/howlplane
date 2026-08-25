# Configurable AI Resource Pool

HowlPlane uses one shared `ProviderPoolManager` for readiness, current capacity,
hard filtering, final selection, implementation failover, and reviewer failover.
`AgentRegistry` supplies declarative profiles, `AgentBackendRegistry` supplies
adapters, and `TaskRouter` supplies an advisory deterministic recommendation
only after hard eligibility. The older Go provider interface remains a
compatibility API, not a second production pool.

## Identity and authority

- `provider_id`: organization or runtime family, such as `openai` or `ollama`.
- `interface_id`: access contract, such as `codex_subscription_cli`.
- `resource_id`: operator-configurable unit, such as `codex` or `local_ollama`.
- `model_id`: exact observed/configured model, or JSON `null` when unobserved.

Registration means an adapter/profile is known. Configuration means the
operator named the resource. `enabled = true` grants permission to consider it,
not authority to use it for every task. Readiness, capability, current capacity,
economics, egress, repository authority, and required review remain independent.
Provider intelligence and future recommendations cannot grant authority.

## Operator configuration

Repository defaults do not enable a personal provider set. Put local choices in
`~/.config/howlplane/config.toml`, or set `HOWLPLANE_LOCAL_CONFIG` to another
private TOML file. This path is outside the repository and is not committed.
See `config/provider_resources.example.toml` for a sanitized template.

```toml
[ai_resources]
operating_mode = "connected"

[ai_resources.providers.claude_code]
enabled = true

[ai_resources.providers.codex]
enabled = true

[ai_resources.providers.local_ollama]
enabled = true

[ai_resources.provider_policy]
strategy = "adaptive_capacity"
subscription_first = true
prefer_existing_capacity = true
external_before_local = true
allow_paid_api = false
preserve_independent_review = true
preferred_external = ["claude_code", "codex"]
preferred_local = []
cooldown_seconds = 300
```

Supported subsets include a mixed pool, two external resources, one external
resource, and local-only. A resource may remain configured while its executable
or authentication is unavailable; startup stays valid and the resource is
reported but excluded. `enabled = false` stops selection and probing without
deleting historical trajectories, experiments, or evidence.

Legacy configuration without `providers` migrates deterministically. Existing
`local_only` configuration permits only registered local Ollama; existing
connected configuration retains the formerly registered provider set. No
migration enables metered APIs, and `allow_paid_api` defaults to false.

Validation rejects unknown or duplicate resource IDs, duplicate YAML keys,
unknown preferences, unsupported strategies, invalid interface/model
references, and malformed values. A missing optional executable or temporary
outage is runtime readiness, not a configuration error.

## Selection pipeline

The exact order is:

1. registered resources;
2. operator-configured and enabled resources;
3. non-generative runtime readiness;
4. authority, privacy, safety, task egress, and operating-mode policy;
5. task and role capability eligibility;
6. shared current capacity;
7. economic policy;
8. advisory deterministic cognitive/routing recommendation;
9. stable ranking by economics, available capacity, recommendation, diversity
   avoidance, and lexical `resource_id` tie-break.

An explicit `--agent` overrides automatic preference only within the eligible
set. It cannot bypass local-only, no-egress, capability, paid API, authority, or
review. An empty set returns a versioned `BLOCKED` outcome with reason
`NO_ELIGIBLE_AI_RESOURCE` and explainable exclusions.

`CognitiveRecommendation` is the stable future optimization seam. It receives
only already eligible candidates and cannot alter permission, egress, spend,
budget, repository scope, approval, or authority. Milestone #61 does not learn
or persist provider-quality preferences.

## Economics and capacity

Economic classes are `SUBSCRIPTION`, `METERED_API`, `LOCAL`, and `UNKNOWN`.
`subscription_first` ranks an allowed subscription ahead of local, unknown, and
metered resources. `allow_paid_api = false` removes metered candidates before
recommendation, so subscription exhaustion never silently creates spend.
When paid API use is allowed, `max_metered_invocations` is an attempt budget:
every invocation is counted even when its engineering result fails, and the
resource is excluded after the configured limit. Configuration rejects a
positive metered budget while paid API use is forbidden.
HowlPlane records no price, quota percentage, or reset time unless observed.

Current states include `AVAILABLE`, `DEGRADED`, `RATE_LIMITED`,
`SESSION_EXHAUSTED`, `QUOTA_EXHAUSTED`, `AUTH_REQUIRED`,
`MISSING_EXECUTABLE`, `UNREACHABLE`, `UNAVAILABLE`, `DISABLED`, and `UNKNOWN`.
They are atomically stored in `~/.config/howlplane/provider_capacity.json`.
Availability failures update that single state for implementation and review.
Engineering failures, failed tests, review defects, malformed output, and
verifier rejection do not become quota exhaustion. Temporary states recover
through observed retry/cooldown, targeted reset/re-probe, or later success; no
resource is permanently blacklisted.

Configured model overrides apply only to adapters that declare model selection
support. The selected and inventoried identity then records that configured
model; unobserved hosted CLI models remain JSON `null`. Marathon failover also
records the prior resource ID on the resulting trajectory, without reclassifying
the underlying engineering result as capacity exhaustion.

## Local-only and independent review

With `operating_mode = "local_only"`, hosted resources are marked not probed
before adapter lookup. There is no hosted health request, readiness probe,
planning, generation, or review. Local Ollama is a normal registered resource,
not a magical fallback, and is selected only for roles/capabilities it supplies.
Its current profile supports bounded planning, synthesis, and eligible review,
but not repository implementation because it has no repository execution
contract.

When independent review is required, selection prefers another provider family
from the implementer. Routing records `review_diversity_achieved` truthfully. A
same-provider/model request is not described as independent, and unavailable
diversity does not weaken required review.

## CLI

`ai providers` shows all registered resources and distinct configured, enabled,
readiness, authentication, and capacity facts. `--json` emits
`howlplane.ai_resources/v1` and never guesses quota percentages.

`ai providers reset <resource-id>` clears and re-probes only current state for
that resource and appends an audit event. It does not remove trajectories,
experiments, performance history, or other evidence.

`ai route "objective" --role implementation` is read-only. It loads persisted
capacity without probing or generation and explains requirements, eligible and
excluded resources, economics, recommendation, and likely selected identity.
`--json` emits `howlplane.resource_selection/v1`.

`ai doctor` validates configuration and runs non-generative readiness checks.
Hosted CLI checks inspect executable presence only; generation capacity and
authentication remain unknown unless safely observed elsewhere. In local-only
mode it does not touch hosted adapters.

## Provider onboarding and removal

Adding a provider normally requires only an `AgentProfile` registration, an
`AgentBackend` adapter with non-generative readiness and normalized execution,
and operator configuration. The CI-only `fake_future_provider` proves inventory,
configuration, capability filtering, selection, durable capacity, and trajectory
recording without edits to the orchestrator, generic selection algorithm,
review selector, CLI inventory, or trajectory schema. Disabling/removing a
resource changes future eligibility only; historical evidence stays valid.

## Trajectory evidence

`ExecutionTrajectory` v2 extends the existing artifact instead of creating an
analytics log. It records candidates, exclusions and stages, selected identities,
role, capacity before/after, economics, cognitive recommendation,
implementation/reviewer provider events, failover, diversity, verification, and
result. Version 1 trajectories continue to load and verify against their
original digest rules. Resume retains stable trajectory identity and does not
duplicate provider events.
