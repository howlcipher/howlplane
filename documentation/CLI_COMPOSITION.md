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

`cli.NewPlaneCommand()` creates the attachable `plane` command. An umbrella
program can compose it without copying validation logic:

```go
root := &cobra.Command{Use: "howl"}
root.AddCommand(cli.NewPlaneCommand())
```

This exposes the native component adapter as `howl plane project validate`.
The command receives its parent from Cobra at execution time, so it does not
depend on the literal name of the outer executable.

## Integration Choices

Direct Go composition is the preferred path for the native project integration
commands. It keeps one command implementation, supports isolated command
construction in tests, and lets the ecosystem CLI present a cohesive help tree.
Its limitation is that it applies only to the Cobra command adapters provided by
this module.

The established Python control plane command set remains available through the
direct `howlplane` launcher. Until those adapters are native Go commands, an
umbrella CLI should use a process adapter that forwards arguments, stdin,
stdout, stderr, and exit status to `howlplane`. This preserves HowlPlane policy
and avoids copying orchestration, validation, or authority logic. The process
adapter must not parse human-readable output or infer authorization from it.

Neither approach grants the umbrella repository extra authority. The component
continues to enforce its own validation, policy, evidence, and human authority
boundaries.

## Installer Scope

HowlPlane's installer installs the direct `howlplane` command and keeps `ai` as
a compatibility link. Installing an ecosystem-wide `howl` command is explicitly
out of scope here and belongs to the root `howlcipher/howl` repository.
