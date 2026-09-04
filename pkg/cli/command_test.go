package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestNewRootCommandUsesHowlPlaneBranding(t *testing.T) {
	command := NewRootCommand()
	if command.Use != "howlplane" {
		t.Fatalf("root command use = %q, want howlplane", command.Use)
	}
	if !strings.Contains(command.Long, "HowlPlane") {
		t.Fatalf("root command description does not identify HowlPlane: %q", command.Long)
	}
	if command.CommandPath() != "howlplane" {
		t.Fatalf("root command path = %q, want howlplane", command.CommandPath())
	}
}

func TestLegacyCommandHasProjectValidationParityAndWritesWarningToStderr(t *testing.T) {
	projectRoot := createProject(t)

	canonicalStdout, canonicalStderr, canonicalErr := execute(t, NewRootCommand(), "project", "validate", projectRoot)
	legacyStdout, legacyStderr, legacyErr := execute(t, NewLegacyRootCommand(), "project", "validate", projectRoot)

	if canonicalErr != nil || legacyErr != nil {
		t.Fatalf("validation errors: canonical=%v legacy=%v", canonicalErr, legacyErr)
	}
	if canonicalStdout != legacyStdout {
		t.Fatalf("legacy output differs from canonical output:\ncanonical:\n%s\nlegacy:\n%s", canonicalStdout, legacyStdout)
	}
	if canonicalStderr != "" {
		t.Fatalf("canonical command wrote unexpected stderr: %q", canonicalStderr)
	}
	if !strings.Contains(legacyStderr, "'ai' is deprecated") || !strings.Contains(legacyStderr, "howlplane") {
		t.Fatalf("legacy deprecation warning missing migration guidance: %q", legacyStderr)
	}
	if strings.Contains(legacyStdout, "deprecated") {
		t.Fatalf("legacy warning contaminated command stdout: %q", legacyStdout)
	}
}

func TestPlaneCommandComposesBeneathUmbrellaParent(t *testing.T) {
	root := &cobra.Command{Use: "howl"}
	root.AddCommand(NewPlaneCommand())

	stdout, stderr, err := execute(t, root, "plane", "--help")
	if err != nil {
		t.Fatalf("composed help failed: %v", err)
	}
	if stderr != "" {
		t.Fatalf("composed help wrote unexpected stderr: %q", stderr)
	}
	if !strings.Contains(stdout, "howl plane") || !strings.Contains(stdout, "project") {
		t.Fatalf("composed help did not retain the plane command tree: %q", stdout)
	}
}

func TestCommandConstructionDoesNotDependOnRootExecutableName(t *testing.T) {
	projectRoot := createProject(t)
	parent := &cobra.Command{Use: "ecosystem"}
	parent.AddCommand(NewPlaneCommand())

	stdout, stderr, err := execute(t, parent, "plane", "project", "validate", projectRoot)
	if err != nil {
		t.Fatalf("composed validation failed: %v", err)
	}
	if stderr != "" {
		t.Fatalf("composed validation wrote unexpected stderr: %q", stderr)
	}
	if !strings.Contains(stdout, "Validation passed.") {
		t.Fatalf("composed validation output = %q", stdout)
	}
}

func createProject(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, ".git"), 0755); err != nil {
		t.Fatalf("create git marker: %v", err)
	}
	manifest := "schema_version = 1\nname = \"cli-fixture\"\n"
	if err := os.WriteFile(filepath.Join(root, ".ai-project.toml"), []byte(manifest), 0600); err != nil {
		t.Fatalf("write manifest: %v", err)
	}
	return root
}

func execute(t *testing.T, command *cobra.Command, args ...string) (string, string, error) {
	t.Helper()
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.SetOut(&stdout)
	command.SetErr(&stderr)
	command.SetArgs(args)
	err := command.Execute()
	return stdout.String(), stderr.String(), err
}
