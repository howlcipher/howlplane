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
	"strings"
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
		if !filepath.IsAbs(venv) {
			return "", fmt.Errorf("%s=%s is not an absolute path", EngineEnvOverride, venv)
		}
		cleanVenv := filepath.Clean(venv)
		if p := venvConsoleScript(cleanVenv, "howlplane"); isSecureExecutable(cleanVenv, p) {
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
	if dataHome == "" {
		return ""
	}
	componentRoot := filepath.Join(dataHome, "howl", "components", "howlplane-engine")
	p := filepath.Join(componentRoot, "current", exeName("howlplane-engine"))
	cleanRoot := filepath.Clean(componentRoot)
	cleanP := filepath.Clean(p)
	if isSecureExecutable(cleanRoot, cleanP) {
		return cleanP
	}
	return ""
}

// devCheckoutLauncher mirrors bin/howlplane's own checkout-discovery
// heuristics, for a developer running against a source checkout with no
// Howl-managed install present.
func devCheckoutLauncher() string {
	candidates := []string{}
	if home := os.Getenv("HOWLPLANE_HOME"); home != "" && filepath.IsAbs(home) {
		candidates = append(candidates, filepath.Clean(home))
	}
	if dir := os.Getenv("HOWLPLANE_DIR"); dir != "" && filepath.IsAbs(dir) {
		candidates = append(candidates, filepath.Clean(dir))
	}
	if exe, err := os.Executable(); err == nil && filepath.IsAbs(exe) {
		candidates = append(candidates, filepath.Clean(filepath.Dir(exe)))
	}

	for _, dir := range candidates {
		launcher := filepath.Join(dir, "bin", "howlplane")
		cleanLauncher := filepath.Clean(launcher)
		if isSecureExecutable(dir, cleanLauncher) {
			return cleanLauncher
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
		return filepath.Clean(v)
	}
	home, err := os.UserHomeDir()
	if err != nil || !filepath.IsAbs(home) {
		return ""
	}
	return filepath.Join(filepath.Clean(home), ".local", "share")
}

// isSecureExecutable verifies that target is an absolute path contained beneath
// root, exists as a regular executable file, and neither the target nor any
// directory from target's parent up to root is world-writable without the
// sticky bit set.
func isSecureExecutable(root, target string) bool {
	if !filepath.IsAbs(root) || !filepath.IsAbs(target) {
		return false
	}
	cleanRoot := filepath.Clean(root)
	cleanTarget := filepath.Clean(target)

	rel, err := filepath.Rel(cleanRoot, cleanTarget)
	if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}

	info, err := os.Stat(cleanTarget) // #nosec G703 -- cleanTarget is verified absolute and contained beneath cleanRoot
	if err != nil || info.IsDir() {
		return false
	}

	// World-writable executable files can be overwritten by any local user.
	if info.Mode().Perm()&0o002 != 0 {
		return false
	}

	// On non-Windows platforms, ensure the file is executable.
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o111 == 0 {
		return false
	}

	// Verify that neither the file's parent nor any ancestor directory up to
	// cleanRoot is world-writable unless it has the sticky bit set (like /tmp).
	dir := filepath.Dir(cleanTarget)
	for {
		dirInfo, err := os.Stat(dir) // #nosec G703 -- dir is a parent directory of cleanTarget bounded by cleanRoot
		if err != nil {
			return false
		}
		if dirInfo.Mode().Perm()&0o002 != 0 && dirInfo.Mode()&os.ModeSticky == 0 {
			return false
		}
		if dir == cleanRoot || dir == filepath.Dir(dir) {
			break
		}
		dir = filepath.Dir(dir)
	}

	return true
}
