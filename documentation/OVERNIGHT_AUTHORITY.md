# Real Autonomous Integration & Delegated Overnight Authority — Milestone #59

HowlPlane's marathon dogfooding campaign (`howlplane dogfood`) can integrate its own
evidence-backed engineering fixes for real: real branch, real commit, real
push, real GitHub PR, real observed CI, real merge only when required checks
are green, independently reconciled against remote `main`. This never
happened before #59 — the git/PR/CI/merge fields were fabricated. Every field
in `GitIntegrationRecord` now defaults to "not yet observed" and only becomes
true immediately after the corresponding real, independently-verified event.

Real, autonomous merges only ever happen inside an explicit, durable,
tamper-evident, expiring **authorization envelope** the operator binds at
campaign start. The model never grants itself authority, never expands an
envelope, and never merges because it *claims* success — deterministic
evidence is the only thing that can satisfy the merge gate.

## The command

```bash
howlplane dogfood --until-providers-exhausted --authority-profile overnight-safe
```

Selecting `overnight-safe` at campaign start is the explicit operator
authorization for exactly the actions that profile encodes — nothing more.
Without `--authority-profile`, the campaign still proposes, implements,
reviews, and verifies real fixes, but every git/GitHub integration step and
every consequential action parks for a human decision instead of proceeding,
since there is no envelope to satisfy `evaluate_action_against_envelope()`.

## Exact automatic permissions (`overnight-safe`)

- TTL: **12 hours** from campaign creation.
- Max autonomous merges: **10** per campaign.
- External spend budget: **$0** (no paid API/service usage).
- Authorized repositories: **`howlcipher/howlplane` only.** No other
  repository — including `Career_Agent_Core` — is ever in scope, and the
  campaign cannot discover or add repositories on its own.
- Local RAM launch threshold: **9 GiB** available (vs. 8 GiB interactive
  default) — see [`LOCAL_MODEL.md`](LOCAL_MODEL.md).
- Local `keep_alive`: **`0`** — model unloads immediately after each local
  inference.
- Local-only continuation budget: **10** consecutive iterations after all
  cloud providers are exhausted (fixed; the envelope can never raise it).

Allowed action classes (each independently evaluated per real git/GitHub
step, not just once for the whole task):

`create_task_branch`, `commit_task_changes`, `push_task_branch`,
`create_pull_request`, `inspect_ci`, `repair_ci_bounded`,
`merge_pull_request`, `sync_local_main`, `run_build_test_lint_scan`,
`invoke_configured_ai_provider`, `switch_ai_provider`,
`use_local_ollama_tier3`, `update_task_journal`,
`select_next_evidence_backed_task`, `park_and_continue`.

Run `howlplane authority show overnight-safe` for the live, canonical list — this
document can drift; that command cannot (it prints the actual code-defined
profile, invokes no AI, and performs no writes).

## Never auto-authorized overnight (denied — always requires a human)

`force_push`, `history_rewrite`, `bypass_required_checks`,
`branch_protection_weakening`, `production_deployment`,
`infrastructure_apply`, `destructive_database_change`,
`credential_provisioning`, `package_publishing`, `external_messaging`,
`job_submission`, `paid_service_usage`, `external_dependency_addition`,
`security_policy_exception`, `hygiene_policy_weakening`,
`slop_debt_acceptance`, `authority_profile_modification`,
`authority_enforcement_modification`.

These are checked **before** any envelope lookup and an envelope can never
override them — denies always win. Any diff that touches
`authority_profile.py`, `authority_envelope.py`, `human_boundary.py`, or
`executor.py` is force-routed to a human decision, regardless of what the
envelope otherwise permits: the system cannot rewrite the rules controlling
its own authority and use the new rules in the same unattended campaign.

## How authority is enforced (not just documented)

- `AuthorityProfile` instances (`strict`, `overnight-safe`) are frozen
  dataclasses that exist only as two module-level constants
  (`src/control_plane/authority_profile.py`) — no runtime code constructs a
  third profile or mutates one.
- `create_envelope()` — the sole constructor for a durable
  `AuthorityEnvelope` — is only ever called from the CLI's
  `--authority-profile` dispatch, driven by a value that arrives from the
  operator's shell invocation, never from AI-generated text.
- `save_envelope()` is write-once: it refuses to overwrite an existing
  envelope file. No campaign-internal code calls it a second time mid-run.
- Every consumer (`HumanBoundaryGate.evaluate_with_delegated_authority`,
  `GitIntegrationExecutor.evaluate`, the marathon loop) calls
  `load_envelope()` fresh from disk and re-verifies its SHA-256 policy digest
  on every single call — never a long-lived in-process object an agent's
  output could have mutated. Any drift (bug, tampering, a stray `echo >
  authority_envelope.json`) fails closed (`TamperedEnvelopeError`).

## Parking, not freezing

When a proposed action falls outside the bound envelope (or hits one of the
never-delegatable boundaries above), the task **parks** instead of the whole
campaign halting:

1. The task transitions to a distinct `parked_awaiting_human` state.
2. A `ParkedTaskRecord` is appended to durable campaign state — objective,
   boundary type, evidence, risks, and (optionally) a zero-authority AI
   recommendation (`APPROVE`/`REJECT`/`REVIEW`) for the operator's morning
   context. The recommendation is stored as data only; it is structurally
   impossible for it to reach `evaluate_action_against_envelope()`.
3. The marathon loop moves on to the next independent, evidence-backed
   benchmark/gap rather than waiting.
4. The campaign only stops with `AWAITING_HUMAN` when a park turns out to
   block every remaining meaningful objective — no other pending framework
   gap and no remaining unattempted benchmark
   (`decision_queue.compute_blocks_other_work`).
5. Already-parked tasks are never re-selected, across restarts and resumes.

## Morning status — zero provider cost

```bash
howlplane dogfood --status <campaign-id>
```

Loads durable state directly from disk. Never constructs a provider pool,
never probes agent binaries/services, never invokes a provider, never
mutates campaign scope or authority. The rendered report includes a
**Pending Human Decisions** section: task ID, objective, boundary type,
requested action, repository, evidence, risks, verification state,
recommended action (and its provider), why delegated authority didn't apply,
and whether it blocks other work.

```bash
howlplane authority show overnight-safe   # or: strict
```

Read-only preview of a canonical profile's exact permissions, TTL, budgets,
and local-resource limits. No AI invoked, no writes.

## Resume and expiration

`howlplane dogfood --resume <campaign-id>` preserves the persisted benchmark scope
(#58) and handles authority explicitly:

- If the existing envelope is still valid (TTL not expired, digest verifies,
  policy/repository facts still match), resume reuses it automatically —
  no flag needed.
- If it has **expired**, authority is **not** silently renewed. The campaign
  proceeds read/propose-only: real implementation and review can still
  happen, but every envelope-gated action parks.
- Passing `--authority-profile overnight-safe` again on resume is an
  explicit operator reauthorization — it creates a fresh envelope with a new
  12-hour TTL and a reset merge budget for this resumed run.
- Previously exhausted cloud providers are always re-probed on resume (#58),
  independent of authority state.

## Stop conditions

A campaign stops (rather than parking one task and continuing) when:

- All benchmarks in scope are attempted and no work remains
  (`completed_all_benchmarks`).
- The safety iteration ceiling is reached (`campaign_safety_ceiling_reached`).
- All cloud providers are exhausted **and** the bounded local-only
  continuation budget (10 consecutive iterations) is also exhausted
  (`local_only_budget_reached`) or local RAM stays below the overnight
  threshold after unload (`local_resource_constrained`).
- A parked task blocks every remaining meaningful objective
  (`AWAITING_HUMAN`).

## Non-goals (explicitly out of scope, by design)

No blanket auto-approval. No AI-driven expansion of its own authority. No
unattended production deployments, credential changes, or security-policy
weakening. No force push, no CI bypass, no trusting simulated git evidence.
Local Ollama output is never treated as truth — deterministic verification,
independent review, and (for merges) real, independently-observed GitHub CI
remain mandatory regardless of what any provider — local or cloud — claims.
