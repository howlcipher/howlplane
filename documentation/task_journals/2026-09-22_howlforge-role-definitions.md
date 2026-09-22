# Task Journal: HowlPlane adopts HowlForge role definitions

## Summary

- **Task:** improvements.md item 70 — adopt HowlForge role definitions as the source of truth for role capability requirements, and fix the agent registry schema drift that motivates it
- **Status:** In progress
- **Started:** 2026-09-22
- **Agent and model:** Claude Code / Opus 5

## Pre-Flight Re-Evaluation

- **Model choice:** Claude Code / Opus 5, no delegation. The task turns on a governance
  judgment under `CONTROL_PLANE.md` §1.1 and on cross-repository contract design between
  howlplane and howlforge, which is exactly the kind of work the delegation policy keeps
  in the orchestrating model. The mechanical parts (schema edit, YAML vendoring) are too
  small to be worth a handoff on their own.
- **Skills routed:** `architectural_guardrails` (structural change to a frozen
  architecture), `quality_assurance` and `test_and_verify` (test design for a selection
  path), `technical_writing` (ADR 0007), `systems_logic` (role dependency mapping),
  `cyber_security` (vendored external data is untrusted input).
- **Free tools:** none needed. The vendored-contract pattern, the pinned-commit CI step
  and the `make_pool`/`CountingBackend` fixtures already exist in this repository. No
  installation required, nothing to ask approval for.

## Plan

- [ ] Step 1: add the 10 drifted fields to `schemas/agent-registry.schema.json` and a
      test asserting `AgentRegistry().to_dict()` validates against its own schema
- [ ] Step 2: vendor HowlForge role definitions and schemas into `contracts/howlforge/`,
      commit-pinned, with SOURCE.md
- [ ] Step 3: connect `AppSettings.roles` to `RoleDescriptor` via `RoleBindingRegistry`
- [ ] Step 4: make `_required_capabilities` descriptor-driven with a byte-identical
      fallback to today's behavior
- [ ] ADR 0007 recording the §1.1 reasoning and the counter-reading
- [ ] improvements.md row + detail section with the measured evidence
- [ ] README and change_log updates, full regression gate, commit, push

## Progress Log

- 2026-09-22 — Measured the evidence for the §1.1 claim before writing any code.
  `AgentRegistry.to_dict()` stamps `ai.agent_registry/v1` and emits 10 fields the schema
  forbids under `additionalProperties: false`, `roles` among them; 6 of 6 built-in agents
  fail validation. 78 role-string literal sites across 16 files in `src/`, 43 reviewer-id
  sites. `RoleDescriptor` is never constructed anywhere in `src/`; `AppSettings.roles` is
  read by nothing. Deliberately NOT claimed: the `from_dict` round-trip defect, because
  `AgentRegistry.from_dict` has no production caller, making it latent rather than live.
  Branch restarted from `origin/main` at effd43a.

## Next Step

Implement step 1: extend `schemas/agent-registry.schema.json` with the 10 drifted fields and add the self-validation test.
