// Package cli constructs the reusable HowlPlane Cobra command tree.
package cli

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/howlcipher/howlplane/internal/enginepath"
	"github.com/howlcipher/howlplane/internal/project"
	"github.com/howlcipher/howlplane/internal/version"
	"github.com/spf13/cobra"
)

const legacyDeprecationWarning = "'ai' is deprecated. Use the Howl ecosystem CLI when available, or 'howlplane' for direct HowlPlane access.\n"

// NewRootCommand constructs the standalone HowlPlane command. Process entry
// points own execution and exit handling so callers can construct this command
// repeatedly in tests or embed it in another program.
func NewRootCommand() *cobra.Command {
	command := newPlaneCommand("howlplane", "HowlPlane — AI Engineering Control Plane")
	command.Long = "HowlPlane — AI Engineering Control Plane. Direct component access for HowlPlane project integrations."
	return command
}

// NewPlaneCommand constructs the command that an ecosystem CLI can attach
// beneath its own root, for example: root.AddCommand(cli.NewPlaneCommand()).
// It contains the same command adapters as the standalone root and makes no
// assumptions about its eventual parent executable name.
func NewPlaneCommand() *cobra.Command {
	return newPlaneCommand("plane", "HowlPlane — AI Engineering Control Plane")
}

// NewLegacyRootCommand constructs the temporary ai compatibility command. The
// warning is written to stderr so command stdout remains safe for automation.
func NewLegacyRootCommand() *cobra.Command {
	command := newPlaneCommand("ai", "Deprecated compatibility command for HowlPlane")
	command.Long = "Deprecated compatibility command. Use howlplane for direct HowlPlane access."
	command.PersistentPreRun = func(cmd *cobra.Command, args []string) {
		fmt.Fprint(cmd.ErrOrStderr(), legacyDeprecationWarning)
	}
	return command
}

func newPlaneCommand(use, short string) *cobra.Command {
	command := &cobra.Command{
		Use:     use,
		Short:   short,
		Version: version.Version,
		// ArbitraryArgs lets an unrecognized subcommand fall through to
		// RunE below instead of Cobra rejecting it as "unknown command"
		// -- everything this tree doesn't natively implement (i.e.
		// everything except "project") is the Python control-plane
		// engine's, not an error. DisableFlagParsing is required for the
		// same reason: the engine's own subcommands have flags Cobra has
		// never heard of (e.g. "status --json"), and Cobra rejects any
		// unrecognized flag before RunE ever runs, and even with
		// FParseErrWhitelist.UnknownFlags silently drops it rather than
		// forwarding it -- neither is acceptable when every byte of argv
		// must reach the engine unchanged. This also turns off Cobra's
		// automatic --version/--help handling, which runEngineFallback
		// replaces below.
		Args:               cobra.ArbitraryArgs,
		DisableFlagParsing: true,
		RunE:               runEngineFallback,
		SilenceErrors:      true,
		SilenceUsage:       true,
	}
	command.AddCommand(newProjectCommand())
	return command
}

// EngineExitError reports the sibling Python engine's own exit code, so
// process entry points (cmd/howlplane, cmd/ai) can propagate it instead
// of collapsing every engine-side failure to a generic exit(1). The
// engine has already written its own error output to stderr by the time
// this is returned, so callers should exit with Code, not print Error().
type EngineExitError struct {
	Code int
}

func (e *EngineExitError) Error() string {
	return fmt.Sprintf("control-plane engine exited with status %d", e.Code)
}

// runEngineFallback delegates any subcommand this Go command tree doesn't
// natively implement to the sibling Python control-plane engine,
// preserving argv, stdio, and exit code -- the same delegation
// bin/howlplane performed before Howl could install this binary directly
// onto PATH.
//
// --version and -h/--help are handled here directly rather than
// forwarded. Cobra's own automatic handling for both requires flag
// parsing this command deliberately disables (see newPlaneCommand):
//   - delegating --version to the engine would report the Python
//     engine's version instead of this compiled Go binary's -- exactly
//     backwards for what Howl's installer uses this flag for: verifying
//     the artifact it just activated.
//   - delegating -h/--help would replace this command tree's own
//     discoverability (e.g. the "project" subcommand, and this tree
//     composed under another CLI's root via NewPlaneCommand) with the
//     engine's argparse help text, which knows nothing about "project".
func runEngineFallback(cmd *cobra.Command, args []string) error {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
		return cmd.Help()
	}
	if args[0] == "--version" {
		fmt.Fprintf(cmd.OutOrStdout(), "%s version %s\n", cmd.Name(), cmd.Version)
		return nil
	}

	entrypoint, err := enginepath.Resolve()
	if err != nil {
		return err
	}

	sub := exec.Command(entrypoint, args...)
	sub.Stdin = cmd.InOrStdin()
	sub.Stdout = cmd.OutOrStdout()
	sub.Stderr = cmd.ErrOrStderr()
	if err := sub.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			return &EngineExitError{Code: exitErr.ExitCode()}
		}
		return fmt.Errorf("failed to run the control-plane engine: %w", err)
	}
	return nil
}

func newProjectCommand() *cobra.Command {
	projectCommand := &cobra.Command{
		Use:   "project",
		Short: "Manage and inspect project integrations",
	}
	projectCommand.AddCommand(&cobra.Command{
		Use:   "validate [path]",
		Short: "Validate the project manifest and local integration",
		Args:  cobra.MaximumNArgs(1),
		RunE:  runProjectValidate,
	})
	return projectCommand
}

func runProjectValidate(command *cobra.Command, args []string) error {
	startDir := "."
	if len(args) == 1 {
		startDir = args[0]
	}

	root, err := project.DiscoverRoot(startDir)
	if err != nil {
		return fmt.Errorf("error discovering project root: %w", err)
	}

	manifestPath := filepath.Join(root, ".ai-project.toml")
	fmt.Fprintf(command.OutOrStdout(), "Validating manifest at %s\n", manifestPath)

	manifest, err := project.LoadManifest(manifestPath)
	if err != nil {
		if os.IsNotExist(err) {
			return fmt.Errorf("manifest file not found at project root: %s", manifestPath)
		}
		return fmt.Errorf("manifest validation failed:\n%w", err)
	}

	fmt.Fprintln(command.OutOrStdout(), "Manifest loaded successfully:")
	fmt.Fprintf(command.OutOrStdout(), "  Project Name: %s\n", manifest.Name)
	fmt.Fprintf(command.OutOrStdout(), "  Schema Version: %d\n", manifest.SchemaVersion)
	fmt.Fprintln(command.OutOrStdout(), "Validation passed.")
	return nil
}
