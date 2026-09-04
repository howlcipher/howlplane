package enginepath

import (
	"os"
	"path/filepath"
	"testing"
)

func TestResolveUsesEnvOverride(t *testing.T) {
	venv := t.TempDir()
	binDir := filepath.Join(venv, "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(binDir, "howlplane")
	if err := os.WriteFile(script, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EngineEnvOverride, venv)

	got, err := Resolve()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != script {
		t.Errorf("expected %s, got %s", script, got)
	}
}

func TestResolveEnvOverrideMissingScriptErrors(t *testing.T) {
	t.Setenv(EngineEnvOverride, t.TempDir())
	if _, err := Resolve(); err == nil {
		t.Fatal("expected an error when the override venv has no installed console script")
	}
}

func TestResolveFindsHowlManagedEntrypoint(t *testing.T) {
	t.Setenv(EngineEnvOverride, "")
	dataHome := t.TempDir()
	t.Setenv("XDG_DATA_HOME", dataHome)
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	wrapperDir := filepath.Join(dataHome, "howl", "components", "howlplane-engine", "current")
	if err := os.MkdirAll(wrapperDir, 0o755); err != nil {
		t.Fatal(err)
	}
	wrapper := filepath.Join(wrapperDir, "howlplane-engine")
	if err := os.WriteFile(wrapper, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	got, err := Resolve()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != wrapper {
		t.Errorf("expected %s, got %s", wrapper, got)
	}
}

func TestResolveFallsBackToDevCheckoutLauncher(t *testing.T) {
	t.Setenv(EngineEnvOverride, "")
	t.Setenv("XDG_DATA_HOME", t.TempDir()) // nothing Howl-managed here
	checkout := t.TempDir()
	binDir := filepath.Join(checkout, "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	launcher := filepath.Join(binDir, "howlplane")
	if err := os.WriteFile(launcher, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOWLPLANE_HOME", checkout)

	got, err := Resolve()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != launcher {
		t.Errorf("expected %s, got %s", launcher, got)
	}
}

func TestResolveErrorsWhenNothingFound(t *testing.T) {
	t.Setenv(EngineEnvOverride, "")
	t.Setenv("XDG_DATA_HOME", t.TempDir())
	t.Setenv("HOWLPLANE_HOME", "")
	t.Setenv("HOWLPLANE_DIR", "")

	if _, err := Resolve(); err == nil {
		t.Fatal("expected an actionable error when no engine can be located")
	}
}
