# Vendored HowlForge role definitions and contract schemas

This is a vendored, byte-for-byte copy of HowlForge's role definitions and the
JSON Schemas that describe them. HowlPlane reads these to derive a role's
capability requirements instead of deriving them from hard-coded branches in
`ProviderPoolManager._required_capabilities`.

**Source repository:** https://github.com/howlcipher/howlforge
**Pinned commit:** `c08b888def3081136bbb645c4278c0f942050b13`
**Source paths:** `config/roles/*.yaml`, `schemas/role.schema.json`,
`schemas/capability-level.schema.json`, `schemas/runtime-ref.schema.json`
**Vendored:** 2026-09-22

## Why vendored rather than imported

HowlForge is a Go library and CLI. It follows the same distribution model this
repository already uses for `howldream` and the `howl.exploration_result/v1`
contract: an optional sibling component pinned to an exact commit, never a hard
dependency. HowlPlane must keep working with no HowlForge binary present, no
network, and no Go toolchain.

Concretely, that means:

- Nothing in `src/` shells out to `howlforge`. These files are data.
- If this directory is absent or unreadable, role capability derivation falls
  back to the previous hard-coded behavior, byte for byte. That fallback is
  pinned by test, not assumed.
- Bumping the pin is a visible diff, never a moving `@main`, so a passing run
  always means the same HowlForge definitions were tested.

## What HowlPlane takes from these files, and what it ignores

Taken:

- `id` and `required_capabilities` — the durable statement of what a role needs.
- `verification.verifier_role` and `verification.independent_runtime` — recorded
  as role metadata.

Deliberately ignored:

- `preferred_runtimes` and `fallback_runtimes`. HowlForge's runtime preference
  order is not HowlPlane's selection order. `select_resource` ranks on live
  capacity, economics and egress policy, which HowlForge does not model, and
  those must not be overridden by a static preference list.
- `handoff_required`. HowlRelay and the checkpoint machinery own that.

## Role name mapping

HowlForge role ids are not HowlPlane lifecycle roles. `select_resource` excludes
a candidate with `ROLE_NOT_SUPPORTED` unless the requested role is an exact
member of `profile.roles`, so HowlForge names are mapped onto the existing
lifecycle roles rather than adopted verbatim. The mapping lives in
`src/control_plane/howlforge_roles.py` and is asserted by test.

## How to re-vendor

```sh
cp /path/to/howlforge/config/roles/*.yaml contracts/howlforge/roles/
cp /path/to/howlforge/schemas/role.schema.json contracts/howlforge/howlforge.role.v1.schema.json
cp /path/to/howlforge/schemas/capability-level.schema.json contracts/howlforge/howlforge.capability_level.v1.schema.json
cp /path/to/howlforge/schemas/runtime-ref.schema.json contracts/howlforge/howlforge.runtime_ref.v1.schema.json
```

Update the pinned commit above in the same change. `tests/test_howlforge_roles.py`
validates every vendored role file against the vendored schema, so a re-vendor
that breaks the contract fails CI rather than silently misrouting work.
