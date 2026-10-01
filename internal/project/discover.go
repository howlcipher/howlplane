package project

import (
	"fmt"
	"os"
	"path/filepath"
)

// DiscoverRoot traverses upwards from the given starting directory
// to find the root of the project, identified by the presence of .ai-project.toml
// If that is not found, it falls back to the presence of a .git directory or file (worktrees and submodules).
func DiscoverRoot(startDir string) (string, error) {
	absStart, err := filepath.Abs(startDir)
	if err != nil {
		return "", err
	}

	current := absStart
	for {
		manifestPath := filepath.Join(current, ".ai-project.toml")
		if _, err := os.Stat(manifestPath); err == nil {
			return current, nil
		}

		// Fallback to git root
		gitPath := filepath.Join(current, ".git")
		if info, err := os.Stat(gitPath); err == nil && (info.IsDir() || info.Mode().IsRegular()) {
			return current, nil
		}

		parent := filepath.Dir(current)
		if parent == current {
			// reached the root of the filesystem without finding it
			return "", fmt.Errorf("could not discover project root from %s", startDir)
		}
		current = parent
	}
}
