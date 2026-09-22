# ADR 0007 — HowlForge Role Definitions as the Source of Role Requirements

## Status

Proposed — admitted under `documentation/CONTROL_PLANE.md` §1.1 as a defect fix, not
under the §1.1.1 carve-out. The counter-reading is recorded below and is not dismissed;
a reviewer who finds it more persuasive should reject this ADR rather than the code.

## Context

HowlPlane decides which provider fills a role. It has never had a durable statement of
what a role *requires*. That statement lives in three places at once, none of them
authoritative:

- `ProviderPoolManager._required_capabilities` derives capabilities from a three-branch
  `if/elif` on the role string.
- `AgentProfile.roles` decides eligibility, via `ROLE_NOT_SUPPORTED` in
  `select_resource`, from a code-side default factory.
- Role names themselves are string literals scattered across `src/`.

HowlForge (https://github.com/howlcipher/howlforge) was built to be that statement. Its
premise is that roles are durable and models are replaceable: a role declares capability
requirements, and any runtime meeting them may fill it. Until HowlPlane reads those
declarations, HowlForge is parallel rather than load-bearing, and the two systems can
disagree about what a role needs with nothing to catch it.

## Evidence

Measured from this repository on 2026-09-22. This is the evidence §1.1 requires as the
price of admission, and the baseline against which this work is judged.

| Evidence | Measurement | Defect class |
| :--- | :--- | :--- |
| The canonical agent registry schema cannot validate the artifact that declares conformance to it | `AgentRegistry.to_dict()` stamps `ai.agent_registry/v1` and emits **10 fields the schema forbade** under `additionalProperties: false`: `roles`, `provider_id`, `interface_id`, `resource_id`, `model_id`, `locality`, `economic_class`, `supports_structured_output`, `supports_tool_calling`, `model_configurable`. **6 of 6** built-in agents failed validation | Verification — a schema that rejects its own artifact proves nothing about it |
| The field that gates every provider selection was absent from that schema | `select_resource` excludes candidates with `ROLE_NOT_SUPPORTED` on exact membership in `profile.roles`, yet `roles` was not a property of `ai.agent_registry/v1` at all | Verification |
| The drift was invisible to the test suite | `tests/test_control_plane_schemas.py` validated a hand-written example payload that conformed by construction, never the serializer's real output | Verification |
| Role identity is scattered string literals rather than data | **78** role-string literal sites across **16** files in `src/`; **43** reviewer-role-id literal sites | Severe usability |
| Roles cannot be declared by an operator at all | `ProviderResourceSettings` is `extra="forbid"` with no `roles` field, so an `[ai_resources.providers.*]` block rejects a `roles` key | Severe usability |
| The seam built for external role definitions was never connected | `AppSettings.roles` is an untyped dict that nothing in `src/` reads; `RoleBindingRegistry.load_from_config` is called only from tests; `RoleDescriptor` is never constructed anywhere | Severe usability |

### Deliberately not claimed

`AgentRegistry.from_dict` has no production caller. The round-trip defect — a reload
silently restoring `local_ollama`'s deliberately narrowed `["planning", "review",
"synthesis"]` to the four-role default, handing a local model the mutating roles it was
excluded from — is therefore **latent, not live**. It is real, and it is why `roles` is
now `required` in the schema, but §1.1 asks whether something blocks real engineering
work and a latent path is a weaker answer. The verification claims above stand without
it.

## Decision

Adopt HowlForge role definitions as the source of role capability requirements, at the
narrowest scope that makes them load-bearing.

1. **Fix the schema drift.** `schemas/agent-registry.schema.json` describes every field
   the registry serializes, `roles` is required, and three tests validate the serializer
   rather than a fixture.
2. **Vendor the definitions.** `contracts/howlforge/` holds the role YAML and contract
   schemas, pinned to an exact commit, following the `contracts/howl/` precedent.
3. **Derive capabilities from them.** `_required_capabilities` consults
   `src/control_plane/howlforge_roles.py` and falls back to the previous branches when
   no vendored definition covers a role.

### What this does not do

- **No delegation of selection.** `select_resource`'s filter pipeline, `_ranking_key`,
  `_select_reviewers` and `REVIEWER_ROLES` are untouched. Live capacity, economics,
  egress and authority are not modelled by HowlForge and must not be.
- **No runtime preferences.** HowlForge's `preferred_runtimes` ordering is read and
  discarded. A static preference list must not override live capacity ranking.
- **No dependency.** Nothing imports or executes HowlForge. The definitions are vendored
  data; there is no Go toolchain, binary, subprocess or network call.
- **No new authority mechanism.** `AuthorityProfile`, `AuthorityEnvelope` and
  `HumanBoundaryGate` remain the only authority paths.
- **No role renaming.** HowlForge role ids are mapped onto HowlPlane's existing lifecycle
  roles, not adopted verbatim. `select_resource` excludes on exact membership in
  `profile.roles`, so introducing `implementer` as a role name would make every provider
  ineligible for it.

## The §1.1 argument, and the argument against

**For admission.** The schema drift is a live defect in an admissible class, measured and
dated. The change removes framework logic rather than adding it: a three-branch `if/elif`
becomes a lookup over data, and the fallback path preserves the previous behavior exactly.
No new capability is created — the control plane does afterwards precisely what it did
before, sourced from a contract instead of from literals.

**Against admission.** Reading definitions from an external sibling component is a new
integration surface, and §1.1.1 explicitly declines to license "a package ecosystem". A
reviewer could reasonably hold that vendored contracts from a second repository are the
beginning of one, and that the schema fix alone discharges the defect.

That counter-argument is why this work is staged. The schema fix is a separate commit
that stands on its own and needs no interpretation of §1.1. If this ADR is rejected, that
commit survives and the adoption reverts cleanly.

## Consequences

- A role's capability floor is stated once, in a contract both repositories can read.
- Adding a role becomes a mapping entry plus a vendored file, not a new code branch.
- The vendored copy can go stale. Mitigated by the pinned commit, the documented
  re-vendor procedure in `contracts/howlforge/SOURCE.md`, and a test that validates every
  vendored role against the vendored schema so a bad re-vendor fails CI rather than
  silently misrouting work.
- HowlPlane now has an opinion about HowlForge's role vocabulary. The mapping in
  `LIFECYCLE_BY_HOWLFORGE_ROLE` is the single place that opinion lives, and it is
  asserted by test.

## Implementation note: an ordering bug this work surfaced

The first capability derivation tested `coding` before `review`. HowlForge's `reviewer`
role declares coding aptitude, because reading a diff well requires it, so that ordering
handed every reviewer candidate `file_editing` and `repository_access` and shrank the
reviewer pool.

That is the independent-review collapse already recorded as `issues.md` #13, where 13 of
20 reviewer attempts failed and review fell back onto the implementer. The ordering is now
review-first and `test_reviewer_never_requires_repository_write` pins it.
