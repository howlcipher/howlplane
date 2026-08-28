# Task, Run Event, and Handoff Specification (schema family v1)

This document is the canonical, human-readable specification for the five
JSON artifacts that carry a task through the framework's provider runtime:
the task request that starts a run, the normalized events a run emits while
it executes, the final result record, the structured handoff produced when a
run is interrupted or switched between providers, and the shared failure
taxonomy those artifacts reuse. Each artifact carries its own version marker
in a `schema` field — `ai.task_request/v1`, `ai.run_event/v1`,
`ai.run_result/v1`, `ai.handoff/v1`, and `ai.failure_class/v1` — rather than a
single top-level `schema_version` integer, since the five contracts version
independently.

The architectural vision for these artifacts lives in
`documentation/AI_FRAMEWORK_BLUEPRINT.md` sections 8.2 (the provider
contract), 8.3 (the shared failure taxonomy), and 11 (the run, event, and
artifact contracts, including the run directory layout in 11.1). This
document narrows that vision to five artifacts: the file formats and the
rules an instance of each must satisfy to validate. It is the Phase 2
counterpart to `documentation/PROJECT_MANIFEST_SPEC.md`, which specifies
Phase 1 (the portable project manifest) in the same blueprint series.

Machine-readable counterparts:

| Artifact | Path |
| --- | --- |
| Task Request JSON Schema | `schemas/task-request.schema.json` |
| Run Event JSON Schema | `schemas/run-event.schema.json` |
| Run Result JSON Schema | `schemas/run-result.schema.json` |
| Handoff JSON Schema | `schemas/handoff.schema.json` |
| Failure taxonomy JSON Schema | `schemas/failure-class.schema.json` |
| Capability enum JSON Schema (referenced by `required_capabilities`) | `schemas/capability.schema.json` |
| Example fixtures | `examples/runs/task_request.json`, `examples/runs/run_event.json`, `examples/runs/run_result.json`, `examples/runs/handoff.json` |
| Schema/fixture drift test suite | `tests/test_task_event_schema_drift.py` |
| Go runtime types (task/event/result/handoff) | `internal/runtime` (`TaskRequest`, `RunEvent`, `RunResult`, `Handoff`, `FailureClass`, each with `Load<Type>`/`Parse<Type>`/`Validate`) |
| Go provider interface and router | `internal/provider` (`Provider` interface, `Router`, `RouteDecision`) |
| Shared capability enum (Go) | `internal/capability` (`capability.IsKnown`), used by both `internal/project` and `internal/runtime` |

If this document and `schemas/*.json` ever disagree, the schemas are
authoritative and this document is a bug.

---

## 1. Purpose

A run is the unit of work the framework hands to a provider. These five
artifacts are the provider-neutral contract that makes a run observable and
resumable regardless of which provider executed it:

- a **task request** describes what to do and under what constraints,
- a stream of **run events** records what happened, normalized to a common
  shape regardless of which provider emitted them,
- a **run result** records the final outcome,
- a **failure taxonomy** classifies why a run did not succeed, shared between
  the run result and provider failure-classification logic,
- a **handoff** captures enough structured state that an interrupted or
  provider-switched run can be resumed without re-deriving context.

All five are deliberately plain JSON: no provider-specific fields, no
executable content, and — per blueprint 3.6 (append-oriented auditability) —
shaped for human review as well as machine validation.

## 2. File location and lifecycle

- **Location:** unlike `.ai-project.toml`, these are not repository source.
  They are generated at run time under the platform-appropriate user state
  directory described in blueprint 11.1, for example on Linux:

  ```text
  ~/.local/state/ai-framework/runs/<run-id>/
  ├── request.json         <- one ai.task_request/v1 document
  ├── events.jsonl         <- ai.run_event/v1 documents, one per line, appended
  ├── result.json          <- one ai.run_result/v1 document
  ├── handoff.md            <- rendered from an ai.handoff/v1 document
  └── ...                  <- route.json, validation.json, git.patch, etc. (out of scope here)
  ```

- **Task Request:** written once, at the start of a run, as `request.json`.
- **Run Event:** each event is one JSON object serialized as a single line
  and appended to `events.jsonl`. The file is newline-delimited JSON (JSONL),
  not a JSON array — a run's event stream is written incrementally as the
  provider or workflow stage produces events, and JSONL allows appending
  without rewriting the file.
- **Run Result:** written once, when the run reaches a terminal state, as
  `result.json`.
- **Handoff:** the JSON shape in this document is not itself the on-disk
  artifact. It is the structured data a workflow renders into `handoff.md`,
  the human-readable file a resuming provider or operator reads (blueprint
  11.4). Tooling that produces a handoff should treat the JSON shape as the
  source of truth and `handoff.md` as its rendering.
- **Committed:** no. Unlike the project manifest, none of these five
  artifacts are checked into a project's repository. They are run-scoped
  state, identified by `run_id`, and belong to the framework's state
  directory rather than to a project.
- **Not these artifacts:** `route.json`, `context_manifest.json`,
  `validation.json`, and `git.patch`, also named in blueprint 11.1's run
  directory layout, are separate artifacts with their own (future) contracts
  and are out of scope for this document.

---

## 3. Field reference

Field types and requiredness below match `schemas/task-request.schema.json`,
`schemas/run-event.schema.json`, `schemas/run-result.schema.json`, and
`schemas/handoff.schema.json` exactly.

### 3.1 Task Request (`ai.task_request/v1`)

The request that kicks off a run (blueprint 11.2).

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `schema` | string, const `"ai.task_request/v1"` | **Yes** | Identifies the schema and version this document conforms to. |
| `task` | string, non-empty | **Yes** | The task instruction, in natural language. |
| `mode` | string, non-empty | **Yes** | The named execution mode (for example `edit`, `review`, `research`, `plan`). Modes gate which capabilities may be allowed. |
| `project_root` | string, non-empty | **Yes** | The resolved, absolute path to the project root this task runs against. |
| `task_type` | string, non-empty | **Yes** | The task classification produced by the workflow's classify stage (for example `implementation`, `review`, `research`). |
| `required_capabilities` | array of strings | **Yes** | Capabilities this task needs. Each entry should be a known capability string from `schemas/capability.schema.json` (see section 4 of `PROJECT_MANIFEST_SPEC.md` for the vocabulary). |
| `preferred_providers` | array of strings | **Yes** | Ordered provider name preferences for this task. Preferences, not direct executable definitions — an unavailable provider is skipped in favor of the next entry, the same semantics as `[routing]` in the project manifest. |
| `metadata` | object | No | Free-form, workflow-specific metadata. Open (`additionalProperties: true`). |

`required_capabilities` defaults to deny: per blueprint section 10.3, a
capability not listed in this array is not granted to the run, regardless of
what the target project's manifest permits. The array can only narrow what a
run may do relative to project and global policy, never widen it — this
document does not specify the precedence resolution itself (blueprint 8.4),
only the request field that participates in it.

Even though `required_capabilities` is required, an empty array (`[]`) is
valid and means the task needs no capabilities beyond whatever a mode grants
implicitly.

### 3.2 Run Event (`ai.run_event/v1`)

A single normalized event emitted by a provider or workflow stage during a
run (blueprint 11.3), appended as one line to `events.jsonl` in the run's
artifact directory (section 2 above; blueprint 11.1).

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `schema` | string, const `"ai.run_event/v1"` | **Yes** | Identifies the schema and version this document conforms to. |
| `run_id` | string, non-empty | **Yes** | Identifier of the run this event belongs to. |
| `attempt_id` | string, non-empty | **Yes** | Identifier of the attempt within the run this event belongs to. |
| `timestamp` | string, RFC 3339 pattern | **Yes** | When the event occurred, for example `2026-01-01T00:00:00Z`. |
| `source` | string, non-empty | **Yes** | Origin of the event, for example `provider:codex` or `workflow:review`. |
| `type` | string, enum | **Yes** | The normalized event category: `message`, `tool_call`, `tool_result`, `warning`, `error`, `status`, or `metric`. |
| `payload` | object | **Yes** | Event-type-specific data. Open (`additionalProperties: true`). |

`timestamp` is enforced with a regex `pattern`
(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$`)
rather than JSON Schema's `format: date-time` keyword. This is a deliberate
dependency-avoidance choice: under Draft 2020-12, `format` is
annotation-only by default — a validator is not required to actually reject
a malformed value unless the format-assertion vocabulary is explicitly
enabled, which typically means adding an extra library or configuration on
top of a base JSON Schema validator. A `pattern` is enforcing everywhere a
Draft 2020-12 validator runs, out of the box, with no added dependency.

`payload` is intentionally open because its shape depends on `type` (a
`tool_call` payload looks nothing like a `metric` payload); this schema
validates the envelope, not the payload contents.

### 3.3 Run Result (`ai.run_result/v1`)

The final outcome record for a run, written to `result.json` (blueprint 11.1
and 18).

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `schema` | string, const `"ai.run_result/v1"` | **Yes** | Identifies the schema and version this document conforms to. |
| `run_id` | string, non-empty | **Yes** | Identifier of the run this result belongs to. |
| `status` | string, enum | **Yes** | The final outcome of the run: `succeeded`, `failed`, `cancelled`, or `handed_off`. |
| `failure_class` | string | Conditional | Present when `status` is `failed`: the failure taxonomy classification, matching `schemas/failure-class.schema.json`. See the conditional rule below. |
| `provider` | string, non-empty | **Yes** | The provider that ultimately executed the run. |
| `attempts` | integer, minimum `1` | **Yes** | Number of attempts made during this run. |
| `summary` | string, non-empty | **Yes** | Human-readable summary of what happened. |
| `metadata` | object | No | Free-form, workflow-specific metadata. Open (`additionalProperties: true`). |

**The `failure_class` conditional rule, precisely:** the schema's `allOf`
clause states that whenever `status` equals `"failed"`, `failure_class` is
required. Whenever `status` is anything else (`succeeded`, `cancelled`, or
`handed_off`), `failure_class` is simply not required — the schema does not
forbid it from being present in that case, it only stops requiring it. In
practice a non-failed result should omit it, since there is nothing to
classify.

Note a real limitation of the schema as written: `failure_class`'s type is
plain `string` with no `enum` or `$ref` back to
`schemas/failure-class.schema.json`. This means `run-result.schema.json`, in
isolation, will accept `failure_class` set to any string when `status` is
`"failed"` — it enforces *presence*, not *validity against the taxonomy*.
The requirement that the value actually be one of the thirteen values in
section 4 is a documented contract today, checked by
`tests/test_task_event_schema_drift.py` (which loads both schemas and
validates the value against the taxonomy schema explicitly), not by
`run-result.schema.json` alone.

### 3.4 Handoff (`ai.handoff/v1`)

Structured data rendered into a run's `handoff.md` when a run is interrupted
or switched between providers. Blueprint 11.4 lists what a handoff must
summarize; each bullet maps to a field here:

| Blueprint 11.4 bullet | Field |
| --- | --- |
| Original task | `original_task` |
| Project and starting commit | `project_root`, `starting_commit` |
| Context already reviewed | `context_reviewed` |
| Work completed | `work_completed` |
| Files modified | `files_modified` |
| Commands run and results | `commands_run` |
| Failure or interruption reason | `failure_or_interruption_reason` |
| Remaining work | `remaining_work` |
| Security constraints and denied operations | `security_constraints` |
| Recommended next provider or workflow | `recommended_next` |

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `schema` | string, const `"ai.handoff/v1"` | **Yes** | Identifies the schema and version this document conforms to. |
| `run_id` | string, non-empty | **Yes** | Identifier of the run this handoff belongs to. |
| `original_task` | string, non-empty | **Yes** | The original task instruction the run was executing. |
| `project_root` | string, non-empty | **Yes** | The resolved path to the project this run operated on. |
| `starting_commit` | string, non-empty | **Yes** | The Git commit the run started from. |
| `context_reviewed` | array of strings | **Yes** | Context sources already reviewed before the handoff. May be empty. |
| `work_completed` | string | **Yes** | Summary of work completed before the handoff. May be an empty string. |
| `files_modified` | array of strings | **Yes** | Repository-relative paths modified during the run. May be empty. |
| `commands_run` | array of objects | **Yes** | Commands run during the interrupted attempt and their outcomes. Each entry is `{command, result}` (below). May be empty. |
| `failure_or_interruption_reason` | string, non-empty | **Yes** | Why the run was interrupted or handed off. |
| `remaining_work` | array of strings | **Yes** | Work items still outstanding. May be empty. |
| `security_constraints` | array of strings | **Yes** | Denied operations or capability constraints the next provider must respect. May be empty. |
| `recommended_next` | object | **Yes** | Recommended provider and workflow to resume the run: `{provider, workflow}` (below). |

`commands_run[]` entries:

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `command` | string, non-empty | **Yes** | The command that was run, as executed. |
| `result` | string | **Yes** | Outcome of the command. May be an empty string. |

`recommended_next`:

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `provider` | string | **Yes** | Recommended provider to resume the run, or an empty string if none. |
| `workflow` | string | **Yes** | Recommended workflow to resume the run, or an empty string if none. |

Several fields that are required by the schema (`work_completed`,
`recommended_next.provider`, `recommended_next.workflow`, and the `result`
inside a `commands_run` entry) accept an empty string — "required" here
means the key must be present, not that it must be non-blank. Only fields
documented above as "non-empty" additionally carry `minLength: 1`.

---

## 4. Failure taxonomy (`ai.failure_class/v1`)

A shared, thirteen-value closed enum (blueprint 8.3), reused by
`run_result.failure_class` and by provider `classify_failure()`
implementations (section 5 below) so that every provider adapter reports
failures in the same vocabulary.

| Value | Meaning |
| --- | --- |
| `usage_limit` | The account, plan, or token budget's usage limit was reached. |
| `authentication` | The provider rejected, or could not obtain, valid credentials. |
| `model_unavailable` | The requested model or provider is not currently reachable or offered. |
| `permission_or_approval` | The action required a permission or user approval that was not granted. |
| `network_transient` | A transient network failure (timeout, DNS, connection reset) interrupted the call. |
| `provider_transient` | A transient failure internal to the provider itself (for example an overload or 5xx response) occurred. |
| `cancelled` | The run was cancelled by the user or an external signal before completion. |
| `timeout` | The run, or a step within it, exceeded its allotted time budget. |
| `task_failure` | The provider ran to completion but did not accomplish the task — a normal failure, not an outage. |
| `invalid_output` | The provider produced output that failed structural or content validation. |
| `security_denied` | A capability or security gate denied the requested operation. |
| `workspace_unsafe` | The repository or working tree was not in a state safe to operate on (for example a dirty tree in edit mode). |
| `unknown` | The failure could not be classified into any of the above. |

**Fallback rule, restated precisely:** only the availability and
infrastructure classes — `model_unavailable`, `network_transient`,
`provider_transient`, and `timeout` — trigger automatic fallback to another
provider by default. Every other class, including `task_failure`, must not
be silently treated as a provider outage; a normal task failure staying
classified as a normal task failure is the entire point of having a
taxonomy rather than a single generic "failed" bucket.

### 4a. Orchestrator failure classes (`ProviderFailureClass`)

The enum above is the wire vocabulary for `run_result.failure_class`. The
orchestrator's own bounded-failover loop uses a separate, upper-case
taxonomy defined in `src/control_plane/resource_models.py`. The distinction
that matters most there is between a provider that could not be reached and
a provider this control plane stopped:

| Value | Meaning |
| --- | --- |
| `TRANSPORT_UNAVAILABLE` | The provider itself reported that it could not reach its service. Derived from the provider's own transcript. Marks the resource `UNREACHABLE` and starts the cooldown. |
| `EXECUTION_BUDGET_EXCEEDED` | *This control plane* killed the provider at its per-attempt wall-clock budget. Nothing was observed about reachability, so the resource is **not** marked unreachable and gets no cooldown; it is simply not reused within this task. |
| `EXECUTION_PERMISSION_REQUIRED` | The provider launched and reasoned but was denied a tool the task needed, producing no work. |
| `MISSING_EXECUTABLE` | The provider never launched. |
| `ENGINEERING_FAILURE` | The provider ran and failed at the task. Not an availability failure, and not failover-eligible. |

A locally enforced deadline is not evidence of a provider outage. Recording
one as `TRANSPORT_UNAVAILABLE` both misreports the cause and penalizes a
healthy resource across later tasks, which is what HOWLFRAM-SLOPFIX-05
demonstrated on two of its three attempts.

### 4b. Governing a budget-stopped candidate

A provider stopped at the execution budget never reports completion, but it
may already have written a real repository delta. Process outcome and
artifact quality are different questions, and only the second one matters.

When an `EXECUTION_BUDGET_EXCEEDED` attempt leaves a non-empty
task-attributable delta and no further failover is possible, the delta is
captured as a **candidate** and entered into the normal pipeline: independent
review, reconciliation, remediation, deterministic verification, authority.
Nothing is bypassed and nothing is auto-accepted.

Salvage is checked only at the end of the failover chain. While attempt
budget remains, a fresh provider may still produce a complete,
provider-attested result, which is strictly better than governing a fragment.

The candidate is represented truthfully and never as a success:

| Artifact | Contents |
| --- | --- |
| `implementation/result.json` | unchanged — `success: false` |
| `implementation/attempts/<NN>/candidate.json` | `provider_completion_claim: false`, `origin: timed_out_implementation_attempt`, `requires_governance: true` |
| `implementation/attempts/<NN>/candidate.patch` | the captured delta, replayable with `git apply` |
| `OrchestrationResult.implementation_completion_claim` | `false` |

Outcomes:

- **Review and verification pass** — the task may complete under normal authority.
- **Verification rejects it** — the task does not complete and the repository is restored to its pre-task baseline.
- **No independent reviewer available** — the task parks at `awaiting_human` rather than completing on a self-review by the resource that produced the candidate.
- **Zero delta** — there is no candidate; the attempt is an ordinary failure.

### 4c. Terminal state and routing evidence

Two invariants follow from the same run:

**Terminal rollback.** Every terminal implementation failure restores the
repository to its pre-task baseline and records the outcome in the attempt
record. Attempt evidence is written before the restore, so patches survive
it. Pre-existing user modifications and untracked files are preserved
byte-for-byte. Tasks parked at `awaiting_human` are the deliberate exception:
a person is being asked to look at exactly what is on disk.

**Routing evidence.** `initial_route.json` is immutable.
`effective_route.json` is rewritten at every real handoff — not only when a
failover eventually succeeds — and distinguishes each stage of the lifecycle:

| Field | Meaning |
| :--- | :--- |
| `initial_implementation_resource` | Who was routed first. |
| `current_attempt_resource` | Who is attempting right now. |
| `last_attempted_implementation_resource` | Who attempted most recently. |
| `candidate_resource` | Who produced work now under governance. |
| `accepted_implementation_resource` | Who produced work governance accepted. |

`accepted_implementation_resource`, `final_implementation_resource` and
`final_route.accepted` stay `null`/`false` until the single acceptance
boundary: **Stage 8, Governed Completion**, reached only after review,
reconciliation, deterministic verification and the human authority gate have
all passed. Implementation finishing is not acceptance.

Reviewer mappings are labelled `PROVISIONAL` while routing may still change,
`CANDIDATE_REVIEW` while a captured candidate is under review, and `CONFIRMED`
only at acceptance. SLOPFIX-06's evidence claimed an accepted implementer and
`CONFIRMED` mappings while the task was still `reviewing / in_progress`.

**Terminal attempt evidence.** Attempts that hand off record `rollback` and
`next_selection`. The attempt that exhausts the failover budget records them
too, explicitly — `rollback.status = PARKED_FOR_GOVERNANCE`,
`next_selection = null` with `next_selection_reason = MAX_ATTEMPTS_REACHED`,
the remaining otherwise-eligible resources, and
`transition = CANDIDATE_GOVERNANCE` — so "no provider remained" is never
encoded as a missing field.

**Provider scratch.** Provider scratch lives in
`.task_runs/<task>/provider_scratch/<NN-resource>/`, structurally separate
from `implementation/attempts/`, which is control-plane-owned. Providers run
in the repository without a filesystem sandbox, so the boundary is enforced
after execution: artifacts left at the evidence root, and directories invented
under `attempts/` that imitate a canonical attempt, are relocated into owned
scratch with a `_provenance.json` record. Nothing is deleted, and the empty
imitation shell is removed so the canonical attempt count stays truthful.

---

## 5. Provider interface

For context on why these schemas exist: blueprint 8.2 specifies that every
provider adapter should implement an equivalent set of operations —
`probe()`, `health()`, `capabilities()`, `prepare(request)`,
`execute(request)`, `normalize(output)`, `classify_failure()`, and
`redact()`. `prepare`/`execute` consume an `ai.task_request/v1` document;
`normalize` is what turns provider-specific output into `ai.run_event/v1`
and `ai.run_result/v1` documents; `classify_failure` is what assigns the
`ai.failure_class/v1` value described in section 4.

This interface, along with a Router that performs explainable provider
selection (blueprint 3.5 — recording the candidate providers, health
exclusions, and final selection for a route decision), lives in
`internal/provider`. `Router.Select(taskType, preferred)` builds a
`RouteDecision` listing every candidate considered (preferred names first, in
order, then any remaining registered providers by registration order),
records why each excluded candidate was skipped (not registered, a health
check error, or `Health.Available == false`), and returns the first healthy
provider along with the decision — or an error if none are healthy. No real
adapter (Claude Code, Codex, Gemini, Ollama) implements `Provider` yet; see
section 8 (Non-goals).

---

## 6. Validation rules

Rules 1 through 9 are enforced both by the JSON Schemas listed in the
counterparts table (verifiable with `tests/test_task_event_schema_drift.py`)
and, independently, by each type's `Validate() error` method in
`internal/runtime` (`TaskRequest.Validate`, `RunEvent.Validate`,
`RunResult.Validate`, `Handoff.Validate`), mirroring how
`internal/project/manifest.go`'s `validateManifest` mirrors
`schemas/ai-project.schema.json`. The two are hand-kept in sync rather than
one generating the other, the same pattern the project manifest uses — there
is no schema-drift test wired into the Go build the way
`tests/test_manifest_schema_drift.py` covers the manifest; today only the
Python test suite cross-checks the JSON Schemas and fixtures, while
`internal/runtime`'s own `_test.go` files cross-check the Go validators
against the same fixtures independently.

1. **`schema` must equal the exact constant for the artifact type**, for
   example `"ai.task_request/v1"`. Schema-enforced (`const`). A value from a
   different version, such as `"ai.task_request/v2"`, fails validation.
2. **All fields listed as required in section 3 must be present.** Schema-
   enforced (`required`).
3. **Unknown top-level fields are rejected.** Unlike the project manifest,
   all five schemas set `additionalProperties: false`. This is a deliberate
   difference from `PROJECT_MANIFEST_SPEC.md` section 3.1's forward-
   compatibility stance: these are closed, versioned contracts, and a field
   from a future schema revision must bump the `schema` constant rather than
   append silently onto `v1`. Schema-enforced.
4. **`run_event.type` must be one of the seven enumerated categories.**
   Schema-enforced (`enum`).
5. **`run_event.timestamp` must match the RFC 3339-shaped pattern.** Schema-
   enforced (`pattern`; see section 3.2 for why a pattern is used instead of
   the `format` keyword).
6. **`run_result.status` must be one of the four enumerated outcomes.**
   Schema-enforced (`enum`).
7. **`run_result.attempts` must be an integer no less than `1`.** Schema-
   enforced (`minimum`).
8. **`run_result.failure_class` is required exactly when `status` is
   `"failed"`.** Schema-enforced (`allOf`/`if`/`then`). As noted in section
   3.3, the schema enforces presence but not membership in the failure
   taxonomy enum — that cross-check is currently performed only by the test
   suite, not by `run-result.schema.json` in isolation.
9. **`handoff.commands_run` entries must be `{command, result}` objects with
   both keys present and no others.** Schema-enforced (`required`,
   `additionalProperties: false` on the item schema).
10. **Capability strings in `task_request.required_capabilities` must be
    known capabilities.** Not schema-enforced by `task-request.schema.json`
    itself — `required_capabilities` is typed as a plain array of strings
    with no `enum` or `$ref` to `schemas/capability.schema.json`. It is
    Go-enforced: `TaskRequest.Validate` calls `capability.IsKnown` (the same
    `internal/capability` package `internal/project/manifest.go` uses for
    `[security].capabilities`) on every entry. On the Python/schema side it
    is checked only where a consumer explicitly cross-validates each entry,
    as `tests/test_task_event_schema_drift.py` does for the example fixture.
11. **`run_result.failure_class`, when present, must be a member of the
    thirteen-value taxonomy.** Not schema-enforced by `run-result.schema.json`
    itself (see rule 8 and section 3.3). Go-enforced:
    `RunResult.Validate` calls `FailureClass.IsValid()` both when `status`
    is `"failed"` (where a valid value is required) and when it is not
    (where an invalid value, if present at all, is still rejected).
12. **`FailureClass.RetriableViaFallback()` encodes blueprint 8.3's fallback
    rule.** `internal/runtime.FailureClass` exposes this as a method
    returning `true` only for `model_unavailable`, `network_transient`,
    `provider_transient`, and `timeout` — the same four classes named in
    section 4's fallback rule above. This is Go-only; there is no schema
    keyword for "which enum values trigger fallback," since that is workflow
    behavior, not a data-shape constraint.

### 6.1 Running validation

There is no `ai run validate` CLI command yet (see section 8, Non-goals).
The two independent checks are:

```bash
# JSON Schema side: validate every example fixture in examples/runs/ against
# its schema, and assert that the schemas actually enforce their rules (not
# merely describe them) via a set of negative test cases.
pytest tests/test_task_event_schema_drift.py -v

# Go side: parse and Validate() the same example fixtures, plus table-driven
# negative cases per validation rule, for each of the four types.
go test ./internal/runtime/... ./internal/provider/... ./internal/capability/... -v
```

A schema and an arbitrary JSON document can also be checked ad hoc with the
same `jsonschema` library the test suite uses:

```bash
python -c "
import json
from jsonschema import validate
schema = json.load(open('schemas/run-result.schema.json'))
instance = json.load(open('path/to/candidate-result.json'))
validate(instance=instance, schema=schema)
"
```

---

## 7. Annotated examples

The following are `examples/runs/task_request.json`,
`examples/runs/run_event.json`, `examples/runs/run_result.json`, and
`examples/runs/handoff.json`, each with commentary. JSON has no comment
syntax, so annotations follow each block rather than appearing inline, as
they do in `PROJECT_MANIFEST_SPEC.md`'s TOML example.

### 7.1 Task Request

```json
{
  "schema": "ai.task_request/v1",
  "task": "Implement retry handling and add tests",
  "mode": "edit",
  "project_root": "/home/operator/projects/career-agent-core",
  "task_type": "implementation",
  "required_capabilities": ["filesystem:repository", "process:project_commands"],
  "preferred_providers": ["codex", "claude"],
  "metadata": {
    "requested_by": "work_next_item"
  }
}
```

- `mode` is `"edit"`, so this run is subject to edit-mode's clean-worktree
  requirement (blueprint 10.2).
- `required_capabilities` grants exactly two capabilities. Anything not
  listed — `network:*`, `git:commit`, `git:push`, `secrets:*` — is denied by
  default for this run regardless of what the target project's manifest
  would otherwise allow.
- `preferred_providers` prefers `codex`, falling back to `claude` if `codex`
  is unavailable.
- `metadata.requested_by` is free-form and specific to the `work_next_item`
  workflow; another workflow could put an entirely different shape here.

### 7.2 Run Event

```json
{
  "schema": "ai.run_event/v1",
  "run_id": "run_2026_02_14_0001",
  "attempt_id": "attempt_1",
  "timestamp": "2026-02-14T18:32:05Z",
  "source": "provider:codex",
  "type": "tool_call",
  "payload": {
    "tool": "shell",
    "command": ["go", "test", "./..."]
  }
}
```

- This is one line of `events.jsonl` for `run_2026_02_14_0001`.
- `source` names the provider that emitted it, prefixed by kind
  (`provider:codex`), matching the `source` convention described in
  section 3.2.
- `type` is `tool_call`, so `payload` carries the tool invocation shape
  (`tool`, `command`) rather than, say, the token counts a `metric` event's
  payload would carry.

### 7.3 Run Result

```json
{
  "schema": "ai.run_result/v1",
  "run_id": "run_2026_02_14_0001",
  "status": "succeeded",
  "provider": "codex",
  "attempts": 1,
  "summary": "Implemented retry handling with exponential backoff and added table-driven tests.",
  "metadata": {
    "duration_seconds": 184
  }
}
```

- `status` is `"succeeded"`, so `failure_class` is correctly omitted — it
  would only be required if `status` were `"failed"`.
- `attempts` is `1`: the task succeeded without needing a retried attempt.
- `metadata.duration_seconds` is workflow-specific and not part of the
  fixed contract.

### 7.4 Handoff

```json
{
  "schema": "ai.handoff/v1",
  "run_id": "run_2026_02_14_0002",
  "original_task": "Migrate the provider health checks to the shared failure taxonomy",
  "project_root": "/home/operator/projects/career-agent-core",
  "starting_commit": "9f2b1c4",
  "context_reviewed": [
    "documentation/AI_FRAMEWORK_BLUEPRINT.md",
    "src/core/provider_preflight.py"
  ],
  "work_completed": "Classified existing preflight checks against the failure taxonomy and drafted the new health() return type.",
  "files_modified": ["src/core/provider_preflight.py"],
  "commands_run": [
    {
      "command": "pytest tests/test_provider_preflight.py",
      "result": "3 passed"
    }
  ],
  "failure_or_interruption_reason": "Usage limit reached on the active provider before the adapter migration could be completed.",
  "remaining_work": [
    "Migrate transport_retry.py to return a FailureClass instead of a raw exception",
    "Wire the new health() return type into claude_code_backend.py"
  ],
  "security_constraints": [
    "process:project_commands only; no network:allowlist capability was granted for this run"
  ],
  "recommended_next": {
    "provider": "claude",
    "workflow": "resume_task"
  }
}
```

- `failure_or_interruption_reason` names `usage_limit` in prose; note that
  this field is free text, not itself constrained to the
  `ai.failure_class/v1` enum — that enum is reserved for
  `run_result.failure_class`, a structured field consumed by fallback logic,
  whereas this field is a human-readable explanation.
- `commands_run` has one entry, showing the interrupted attempt ran its test
  command once and it passed — the interruption was not caused by a test
  failure.
- `recommended_next.workflow` names `resume_task`, the prompt documented in
  `AGENTS.md`'s Prompt Library section for continuing an interrupted task
  from its journal.
- `security_constraints` carries forward what the run was *not* allowed to
  do, so the resuming provider does not have to rediscover that boundary.

---

## 8. Non-goals for v1

This specification covers the shape and validation rules of five run
artifacts. It deliberately does not cover:

- **`ai run` / `ai route` CLI commands.** No command exists yet to submit a
  task request, stream its events, or inspect its result. `internal/provider`
  now packages the router behind an interface (blueprint 17's ordered backlog
  item 4), but nothing in `cmd/ai` calls it yet.
- **Real provider adapters.** No adapter for Claude Code, Codex CLI,
  Gemini/Antigravity, or Ollama implements the `Provider` interface in
  `internal/provider` yet. The eight-operation contract in section 5
  describes what an adapter must implement, not a shipped implementation.
- **State-directory or SQLite persistence.** Blueprint 11.1's
  `~/.local/state/ai-framework/state.db` and `runs/` layout is the target
  location for these artifacts; no code in this repository writes or reads
  them from that location yet. `Load<Type>`/`Parse<Type>` in `internal/runtime`
  can read a document from any path or reader, but nothing wires that to the
  state-directory layout.
- **Semantic routing or health-history signals.** `Router.Select` in
  `internal/provider` only ever considers simple, current `Health.Available`
  status; it does not yet weigh historical health or user ratings
  (blueprint 18, Observability) the way a mature router eventually should.

These remain future work per the blueprint's ordered implementation backlog
(section 17); they do not change what a valid `v1` instance of any of the
five artifacts documented here looks like.
