# Frame artifact provenance and runtime compatibility

These artifacts have distinct producer contracts. A runtime version label alone
is insufficient: both the published release and older unreleased compilers have
reported0.1.0 despite differing emitted envelope formats and opcode tables.

## Candidate assessment

Canonical source: integrations/howlframe/candidate_evaluator.howl. The three
boolean equality expressions preserve behavior on the published compiler, which
otherwise compiles bare true as an undefined variable. No authority or schema
rule changes. The canonical/package artifact targets HFBC1 and is compiled with
the actual v0.1.0 Linux amd64 release compiler. Download the archive plus SHA256SUMS
from GitHub release v0.1.0, verify the archive checksum, then run:

```
howlframe -compile-bc integrations/howlframe/candidate_evaluator.howl -o integrations/howlframe/candidate_evaluator.hfbc
cp integrations/howlframe/candidate_evaluator.hfbc src/howlplane/control_plane/data/integrations/howlframe/candidate_evaluator.hfbc
```

The dedicated published-frame CI job downloads that exact release and runs the
committed artifact, not a recompiled substitute. The ordinary suite executes it
with the pinned runtime41d56cecd9e8f936cf9da35240331ab7da0244cd. Both paths require
actual Frame output; Python fallback is made a test failure. Status/authority
parity, filesystem denial and instruction-limit denial are covered. This is
advisory assessment, never authorization or an owner-authenticated action.

The former CI pin7631ce5 wrote HFBC2 (rejected by published v0.1.0) and misread
published HFBC1 numeric opcode ordinals. Existing upstream correction41d56cec
resolves stable opcode names before unchanged structural/capability checks. This
consumer uses that correction; no new Frame decoder or release is required for
the published-runtime assessment path. Source interpretation was considered but
both runtimes reject evaluator try_let under -run, so that boundary is not used.

## Project-context audit

integrations/howlframe/project_context_audit.hfbc also targets HFBC1 and is
compiled by the same published v0.1.0 producer; its source needs no modification.
It executes under both published v0.1.0 and runtime41d56cec. Rebuild with the
published compiler:

```
howlframe build integrations/howlframe/project_context_audit.howl -o integrations/howlframe/project_context_audit.hfbc
cp integrations/howlframe/project_context_audit.hfbc src/howlplane/control_plane/data/integrations/howlframe/project_context_audit.hfbc
```

The existing exact-byte deterministic rebuild assertion is retained and now
requires HOWLFRAME_ARTIFACT_COMPILER_BIN explicitly. HOWLFRAME_BIN selects the
runtime separately. Missing/invalid artifact-compiler overrides fail, rather
than silently selecting whichever runtime is on PATH. scripts/check_test_tools.py
reports this full-tier dependency. Generated applications still compile/run
against the ordinary pinned runtime; these two packaged artifacts have their
own declared producer.

## Exact provenance and provisioning

[integrations/howlframe/ARTIFACT_PROVENANCE.json](../integrations/howlframe/ARTIFACT_PROVENANCE.json)
records the producer release asset/binary and source/artifact SHA256 hashes.
The actual Linux amd64 release archive SHA256 is
`d44006c23cb9c152c815b5acc8762a53f06a93777877b86bfea57d173cc9d907`;
the executable SHA256 is
`3396379ffdbe7823f47e9fd161d11239e886b3b656757203d3063f54a87b882c`.
The provisioning helper checks both pinned hashes and the release checksum file.
It writes only to its caller-owned destination. For example:

```
bash scripts/fetch_frame_artifact_compiler.sh /tmp/frame-v010
export HOWLFRAME_ARTIFACT_COMPILER_BIN=/tmp/frame-v010/howlframe
```

This helper targets the Linux amd64 CI producer only; it is not a general system
installer and does not modify owner-installed tools. Other producer platforms
need independent byte-for-byte verification before substituting them. Source
wheels remain unreleased until owner-authorized publication.
