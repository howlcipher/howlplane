# CLI Composition

## Ownership

HowlPlane owns the direct `howlplane` command. The separate
`howlcipher/howl` repository owns the future `howl` ecosystem command. This
repository must not create or distribute an executable named `howl`.

`ai` remains a compatibility command during migration. It prints its
deprecation notice to stderr, then uses the same command implementation and
preserves arguments, exit status, and stdout.

## Go Command API

The reusable Go command package is `github.com/howlcipher/howlplane/pkg/cli`.

`cli.NewRootCommand()` creates the standalone `howlplane` command. Process
entry points in `cmd/howlplane` and `cmd/ai` contain only startup and error
handling.

`cli.NewPlaneCommand()` creates the attachable `plane` command. It remains
available to Go programs, but the `howl` installer no longer mounts it: Howl's
scope review (`docs/SCOPE.md` in `howlcipher/howl`) removed the `howl plane`
command tree, because it duplicated HowlPlane's own CLI and made Howl depend on
HowlPlane's source.

## Integration Choices

Other Howl components run HowlPlane as a process and never import it (HowlWriter
is the one in-process exception, through `howlplane.control_plane.role_binding`).
The `howl` installer forwards exactly three commands to the installed
`howlplane` (`orchestrate`, `agents doctor`, `factory prepare`), passing
arguments, stdin, stdout, stderr, and exit status through unchanged. A process
adapter must not parse human-readable output or infer authorization from it.

All of this follows the Howl interop contract
(`docs/INTEROP.md` in `howlcipher/howl`):

- `howlplane interop --json` prints the `howl.interop/v1` descriptor: every
  command and flag, the exit codes, and every command HowlPlane runs of another
  component, which `howl doctor` checks against the installed producers.
- HowlPlane finds other components with
  `howlplane.control_plane.component_bin.resolve` (`HOWL<NAME>_BIN`, `PATH`, the
  Howl-managed release, `~/.local/bin`) and reads their results as JSON:
  HowlChangeOps through its `howl.result/v1` envelopes (`--json`), HowlInstinct
  through `howlinstinct.decide_output/v1`, HowlDream and HowlCreate through the
  `howl.*/v1` documents.

Neither approach grants a caller extra authority. Each component continues to
enforce its own validation, policy, evidence, and human authority boundaries.

## Installer Scope

HowlPlane's installer installs the direct `howlplane` command and keeps `ai` as
a compatibility link. Installing an ecosystem-wide `howl` command is explicitly
out of scope here and belongs to the root `howlcipher/howl` repository.
