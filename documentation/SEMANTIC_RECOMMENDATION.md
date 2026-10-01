# Semantic Recommendation (HowlInstinct)

HowlPlane can ask HowlInstinct one bounded question while choosing an AI
resource: which already eligible resource is best suited to perform this task?
The answer is advice. Plane decides what to do with it. Confidence is not
permission.

This feature is off by default. With it off, route output, selection evidence,
and Factory behavior are unchanged.

## Who owns what

| Component | Role |
| --- | --- |
| HowlForge | System 0 workforce facts and deterministic matching. It stays deterministic and is not called by this feature. It needed no change. |
| HowlInstinct | System 1 bounded judgment with explicit uncertainty and a receipt. It does not launch, retry, authorize, or filter anything. |
| HowlPlane | Policy, routing, orchestration, execution, and fallback. It owns every threshold. |
| HowlFrame | Authority over consequential actions. Nothing here grants it. |

```
System 0  deterministic software      Plane hard eligibility, deterministic recommendation
System 1  HowlInstinct judgment       ranks among resources Plane already approved
System 2  generative AI workers       the selected resource performs the task
System 3  human authority             unchanged
```

## Pipeline

```
TaskSpec
  -> hard eligibility (permission, egress, readiness, capability, authority,
     capacity, workspace trust, paid API, budget)
  -> deterministic recommendation (CognitiveRecommendation)
  -> optional HowlInstinct judgment (shadow or active only)
  -> Plane acceptance policy (minimum_instinct_margin)
  -> stable ranking (economics, capacity, recommendation, avoidance, resource_id)
  -> selected resource -> existing execution
```

The semantic layer sees exactly the survivors of hard filtering, sorted by
resource id. A resource Plane excluded cannot be offered, and a judgment that
names anything else is rejected as `UNKNOWN_RESOURCE`. The recommendation only
affects the third ranking key. Economics and capacity rank first, so a judgment
cannot choose a more expensive or exhausted resource over a better ranked one.

## Modes

| Mode | Behavior |
| --- | --- |
| `off` | Default. No call, no evidence block. |
| `shadow` | HowlInstinct is asked and the judgment is recorded. The final selection stays deterministic. Use this to measure disagreement. |
| `active` | An ACCEPTED judgment becomes the recommendation used for ranking. Every other outcome falls back to the deterministic recommendation. |

## Configuration

Operator TOML (`~/.config/howlplane/config.toml` or `HOWLPLANE_LOCAL_CONFIG`):

```toml
[ai_resources.semantic_recommendation]
mode = "shadow"                  # off | shadow | active
command = "howlinstinct"
minimum_instinct_margin = 0.35   # required when mode is not off; no default
timeout_seconds = 5
decision_roles = ["implementation", "remediation"]
strict = false                   # true blocks routing on an untrusted result
allow_in_local_only = false
```

`howlplane route "objective" --semantic shadow|active|off` overrides the mode for
one read-only route. Credentials never appear here. HowlInstinct reads its own
`HOWLINSTINCT_*` settings, and Plane passes no secret on the command line.

## Transport

Plane runs `howlinstinct decide --input - --json`, writes one JSON request to
stdin, and parses stdout strictly. There is no shell, no task text in argv, and
no daemon. Plane does not know any provider wire protocol. HowlInstinct hides
whether the backend is its offline mock, a Jev compatible endpoint, or a future
engine. OpenJEV compatible endpoints use the same provider boundary inside
HowlInstinct; compatibility has not been verified live from this repository.

The request is one `choice` question. The state is a redacted task summary
(objective truncated to 1000 bytes, task class, risk, tier, skills, role). Each
option is an eligible resource id with a short description of tier, economics,
locality, capacity, and capabilities.

## Outcomes and fallback

| Status | Meaning | Result |
| --- | --- | --- |
| `DISABLED` | Mode off | Deterministic |
| `NOT_APPLICABLE` | Role not configured, fewer than two candidates, explicit override, task forbids egress, or `local_only` | Deterministic, no call |
| `ACCEPTED` | Valid judgment with `instinct_margin >= minimum_instinct_margin` | Used in `active`, recorded in `shadow` |
| `LOW_CONFIDENCE` | Margin below the minimum, or not reported | Deterministic |
| `UNAVAILABLE` | CLI missing, provider down, or recent failure cooldown (30 seconds) | Deterministic |
| `TIMEOUT` | No answer within `timeout_seconds` | Deterministic |
| `MALFORMED_RESPONSE` | Not the expected JSON, bad numbers, bad distribution | Deterministic plus evidence |
| `INVALID_RECEIPT` | Receipt fails the vendored schema or disagrees with the judgment | Deterministic plus evidence |
| `UNKNOWN_RESOURCE` | Named a resource Plane did not offer | Rejected, deterministic |
| `CONFIG_ERROR` | HowlInstinct reported a configuration or request problem | Deterministic plus evidence |

With `strict = true`, the integrity failures (`MALFORMED_RESPONSE`,
`INVALID_RECEIPT`, `UNKNOWN_RESOURCE`, `CONFIG_ERROR`) block selection with
`SEMANTIC_RECOMMENDATION_FAILED` instead of falling back. A missing value is
never a negative judgment: an unreported `provider_confidence` or
`instinct_margin` stays null everywhere.

An explicit operator override (`--agent`, or a Factory per attempt pin) is
authoritative and never consults HowlInstinct. Reviewer selection is untouched,
because review roles are not decision roles by default.

## Evidence

`howlplane route --json` and the existing execution trajectory carry a
`semantic_recommendation` block (schema `howlplane.semantic_recommendation/v1`)
only when the mode is not `off`. It records the candidate ids, deterministic
recommendation, selected resource, distribution, `provider_confidence`,
`instinct_margin`, the Plane minimum used, status, whether it was applied, the
final selection, whether the judgment disagreed with the deterministic
recommendation, and the HowlInstinct receipt and its digest. The receipt holds
hashes of state and question, never the raw state. The vendored receipt schema
lives in `contracts/howlinstinct/` with its pinned source.

## Factory

Factory has no separate selector. `execute_factory_work_item` calls
`ProviderPoolManager.select_candidates(task=...)`, which calls `select_resource`,
the only place the judgment is requested. The orchestrator then routes the
pinned resource as an explicit override, which reports `NOT_APPLICABLE`. Paths
that call an agent backend directly (`orchestrate`, role binding, readiness
probes) do not select resources and are outside this feature. A pool built with
no configuration (the legacy `ProviderPoolManager()`) is semantic off.

## Evaluation

`evals/resource_routing/cases.json` lists nine representative task classes with
no ground truth label. `scripts/evaluate_semantic_routing.py` replays them in
shadow mode and writes one JSONL observation per case: candidate set,
deterministic and semantic recommendations, margin, confidence, receipt digest,
final selection, and null outcome fields (resource used, execution outcome,
verification, review, remediation count, timeouts or failover). Outcomes are
filled only from recorded trajectories (`--trajectories DIR`). Nothing learns
from this data and production ranking never changes automatically. HowlInstinct
ships a matching `resource_routing` suite for its own harness.

The offline mock provider is lexical. Runs against it prove plumbing, not
routing quality, and say nothing about whether System 1 improves routing. That
question needs a real provider and collected execution outcomes.

## Future decisions

The interface is generic enough for reviewer selection, failure classification,
retry likelihood, result completeness, and security relevance. None is
implemented. HowlForge could later supply deterministic candidate descriptions
(its `howlforge.match/v1` output) to Plane; nothing requires that today.
