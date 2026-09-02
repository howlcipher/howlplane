// Package cli constructs the reusable HowlPlane Cobra command tree.
package cli

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/howlcipher/howlplane/internal/project"
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
		Use:           use,
		Short:         short,
		SilenceErrors: true,
		SilenceUsage:  true,
	}
	command.AddCommand(newProjectCommand())
	return command
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
