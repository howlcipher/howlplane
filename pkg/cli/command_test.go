package cli

import (
	"bytes"
	"errors"
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

func TestUnknownSubcommandDelegatesToEngineEntrypoint(t *testing.T) {
	dataHome := t.TempDir()
	t.Setenv("XDG_DATA_HOME", dataHome)
	t.Setenv("HOWLPLANE_ENGINE_VENV", "")
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	wrapperDir := filepath.Join(dataHome, "howl", "components", "howlplane-engine", "current")
	if err := os.MkdirAll(wrapperDir, 0o755); err != nil {
		t.Fatal(err)
	}
	fakeEngine := filepath.Join(wrapperDir, "howlplane-engine")
	script := "#!/bin/sh\necho \"engine got: $@\"\nexit 0\n"
	if err := os.WriteFile(fakeEngine, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}

	stdout, stderr, err := execute(t, NewRootCommand(), "status", "--json")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr=%q)", err, stderr)
	}
	if !strings.Contains(stdout, "engine got: status --json") {
		t.Fatalf("expected the fake engine to receive the forwarded args, got stdout=%q", stdout)
	}
}

func TestUnknownSubcommandPropagatesEngineExitCode(t *testing.T) {
	dataHome := t.TempDir()
	t.Setenv("XDG_DATA_HOME", dataHome)
	t.Setenv("HOWLPLANE_ENGINE_VENV", "")
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	wrapperDir := filepath.Join(dataHome, "howl", "components", "howlplane-engine", "current")
	if err := os.MkdirAll(wrapperDir, 0o755); err != nil {
		t.Fatal(err)
	}
	fakeEngine := filepath.Join(wrapperDir, "howlplane-engine")
	script := "#!/bin/sh\necho \"simulated engine failure\" >&2\nexit 7\n"
	if err := os.WriteFile(fakeEngine, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}

	_, _, err := execute(t, NewRootCommand(), "status")
	var exitErr *EngineExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("expected an *EngineExitError, got %v (%T)", err, err)
	}
	if exitErr.Code != 7 {
		t.Errorf("expected exit code 7, got %d", exitErr.Code)
	}
}

func TestNoArgsPrintsHelpInsteadOfDelegating(t *testing.T) {
	t.Setenv("XDG_DATA_HOME", t.TempDir())
	t.Setenv("HOWLPLANE_ENGINE_VENV", "")
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	stdout, _, err := execute(t, NewRootCommand())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "project") {
		t.Fatalf("expected help output listing the project subcommand, got %q", stdout)
	}
}

func TestVersionFlagReportsGoBinaryVersionWithoutDelegating(t *testing.T) {
	// No engine entrypoint is configured to be reachable at all -- if
	// --version were forwarded instead of handled directly, this would
	// fail with "could not locate the HowlPlane control-plane engine"
	// rather than reporting a version.
	t.Setenv("XDG_DATA_HOME", t.TempDir())
	t.Setenv("HOWLPLANE_ENGINE_VENV", "")
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	stdout, _, err := execute(t, NewRootCommand(), "--version")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "howlplane version") {
		t.Fatalf("expected a version line, got %q", stdout)
	}
}

func TestHelpFlagsShowGoCommandTreeWithoutDelegating(t *testing.T) {
	t.Setenv("XDG_DATA_HOME", t.TempDir())
	t.Setenv("HOWLPLANE_ENGINE_VENV", "")
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	for _, flag := range []string{"-h", "--help"} {
		stdout, _, err := execute(t, NewRootCommand(), flag)
		if err != nil {
			t.Fatalf("%s: unexpected error: %v", flag, err)
		}
		if !strings.Contains(stdout, "project") {
			t.Fatalf("%s: expected help output listing the project subcommand, got %q", flag, stdout)
		}
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
	if args == nil {
		// A variadic call with zero arguments passes a nil slice, and
		// cobra.Command.Execute() treats a nil c.args as "use os.Args
		// instead" -- which would leak this test binary's own flags
		// (-test.run, -test.v, ...) into the command under test.
		args = []string{}
	}
	command.SetArgs(args)
	err := command.Execute()
	return stdout.String(), stderr.String(), err
}
