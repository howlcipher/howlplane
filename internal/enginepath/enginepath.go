// Package enginepath locates the sibling Python control-plane engine
// (src/control_plane, distributed as the "howlplane-engine" component)
// that this Go CLI delegates every subcommand to except "project".
//
// Historically, bin/howlplane -- a bash launcher -- owned this dispatch:
// it ran the "project" family through a native Go binary and everything
// else through `python -m src.control_plane.launcher`. Once Howl installs
// the compiled howlplane binary directly onto PATH, that launcher script
// is bypassed, so this package reproduces its "find the engine" half so
// the Go binary can still reach it.
package enginepath

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// EngineEnvOverride is the environment variable an operator can set to
// point directly at a virtualenv containing an installed "howlplane"
// console script, bypassing every other resolution step. Intended for
// development and manual recovery, not normal use.
const EngineEnvOverride = "HOWLPLANE_ENGINE_VENV"

// Resolve locates the engine's runnable entry point, trying in order:
//
//  1. $HOWLPLANE_ENGINE_VENV -- an explicit virtualenv directory override.
//  2. Howl's own data-directory convention for an activated
//     "howlplane-engine" component: every Howl-installed component (by
//     any install method) is reachable at
//     "$XDG_DATA_HOME/howl/components/<name>/current/<name>" --
//     see internal/component/activation.go in the howl repo. This holds
//     regardless of which wheel version Howl actually activated, so it
//     never needs updating here as HowlPlane releases change.
//  3. A local source checkout's bin/howlplane launcher, via
//     $HOWLPLANE_HOME/$HOWLPLANE_DIR or a "howlplane" directory next to
//     the running executable -- the developer/dev-channel fallback.
//
// Resolve returns an actionable error, never a guess, when none of these
// exist.
func Resolve() (string, error) {
	if venv := os.Getenv(EngineEnvOverride); venv != "" {
		if p := venvConsoleScript(venv, "howlplane"); fileExists(p) {
			return p, nil
		}
		return "", fmt.Errorf("%s=%s does not contain an installed howlplane console script", EngineEnvOverride, venv)
	}

	if p := howlManagedEntrypoint(); p != "" {
		return p, nil
	}

	if p := devCheckoutLauncher(); p != "" {
		return p, nil
	}

	return "", fmt.Errorf(
		"could not locate the HowlPlane control-plane engine: install it via `howl install`, " +
			"set " + EngineEnvOverride + " to a virtualenv, or set HOWLPLANE_HOME to a source checkout",
	)
}

// howlManagedEntrypoint returns the path Howl activates the
// "howlplane-engine" component's wrapper script at, if it exists.
func howlManagedEntrypoint() string {
	dataHome := xdgDataHome()
	p := filepath.Join(dataHome, "howl", "components", "howlplane-engine", "current", exeName("howlplane-engine"))
	if fileExists(p) {
		return p
	}
	return ""
}

// devCheckoutLauncher mirrors bin/howlplane's own checkout-discovery
// heuristics, for a developer running against a source checkout with no
// Howl-managed install present.
func devCheckoutLauncher() string {
	candidates := []string{}
	if home := os.Getenv("HOWLPLANE_HOME"); home != "" {
		candidates = append(candidates, home)
	}
	if dir := os.Getenv("HOWLPLANE_DIR"); dir != "" {
		candidates = append(candidates, dir)
	}
	if exe, err := os.Executable(); err == nil {
		candidates = append(candidates, filepath.Dir(exe))
	}

	for _, dir := range candidates {
		launcher := filepath.Join(dir, "bin", "howlplane")
		if fileExists(launcher) {
			return launcher
		}
	}
	return ""
}

func venvConsoleScript(venvDir, name string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(venvDir, "Scripts", name+".exe")
	}
	return filepath.Join(venvDir, "bin", name)
}

func exeName(base string) string {
	if runtime.GOOS == "windows" {
		return base + ".exe"
	}
	return base
}

func xdgDataHome() string {
	if v := os.Getenv("XDG_DATA_HOME"); v != "" && filepath.IsAbs(v) {
		return v
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".local", "share")
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}
