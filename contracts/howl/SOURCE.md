# Vendored `howl.exploration_result/v1` contract schema

This is a vendored, byte-for-byte copy of the generated JSON Schema published
by HowlDream, used only to assert that `NativeHowlDreamProvider`'s real
exploration output actually conforms to the ecosystem envelope shape (see
`tests/test_howldream_live_integration.py`). It is not used to avoid
importing `howldream` — the whole point of that test is exercising the real
import — it just gives the assertion a canonical, independently-generated
shape to check against instead of hand-rolled field checks.

**Source repository:** https://github.com/howlcipher/howldream
**Pinned commit:** `bd10b18b7182e5216b494d728d015dbbba9b32d0`
**Source path:** `schemas/howl.exploration_result.v1.schema.json`
**Vendored:** 2026-09-12

See `howldream`'s `schemas/README.md` for the full distribution-model
rationale (vendored copies pinned to a commit) and `AUTHORITY_INVARIANT.md`
for what schema validation does and doesn't prove.

## How to re-vendor

```sh
cp /path/to/howldream/schemas/howl.exploration_result.v1.schema.json contracts/howl/
```

Update the pinned commit above and the pinned `howldream` git ref in
`.github/workflows/test.yml`'s `nightly-python` job together — they should
always point at the same commit.
