package project

import (
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"strings"

	"github.com/howlcipher/howlplane/internal/capability"
	"github.com/pelletier/go-toml/v2"
)

// Manifest represents the structure of .ai-project.toml
type Manifest struct {
	SchemaVersion int                 `toml:"schema_version"`
	Name          string              `toml:"name"`
	ProjectType   []string            `toml:"project_type,omitempty"`
	Skills        []string            `toml:"skills,omitempty"`
	Context       ContextBlock        `toml:"context,omitempty"`
	Commands      map[string][]string `toml:"commands,omitempty"`
	Security      SecurityBlock       `toml:"security,omitempty"`
	Routing       map[string][]string `toml:"routing,omitempty"`
}

type ContextBlock struct {
	Include []string `toml:"include,omitempty"`
	Exclude []string `toml:"exclude,omitempty"`
}

type SecurityBlock struct {
	Capabilities []string `toml:"capabilities,omitempty"`
}

// LoadManifest reads and parses a project manifest from the given path.
func LoadManifest(path string) (*Manifest, error) {
	file, err := os.Open(path) // #nosec G304 -- caller-supplied path to a local manifest file, not untrusted network input
	if err != nil {
		return nil, fmt.Errorf("failed to open manifest: %w", err)
	}
	defer file.Close()
	return parseManifest(file)
}

func parseManifest(r io.Reader) (*Manifest, error) {
	// Unknown fields are deliberately tolerated (forward-compatible manifests).
	decoder := toml.NewDecoder(r)

	var m Manifest
	if err := decoder.Decode(&m); err != nil {
		return nil, fmt.Errorf("failed to parse manifest: %w", err)
	}

	if err := validateManifest(&m); err != nil {
		return nil, err
	}

	return &m, nil
}

func validateManifest(m *Manifest) error {
	if m.SchemaVersion != 1 {
		return fmt.Errorf("invalid schema_version: expected 1, got %d", m.SchemaVersion)
	}
	if m.Name == "" {
		return fmt.Errorf("manifest is missing required field 'name'")
	}

	// Validate capabilities against the shared enum in internal/capability,
	// which mirrors schemas/capability.schema.json.
	for _, cap := range m.Security.Capabilities {
		if !capability.IsKnown(cap) {
			return fmt.Errorf("unknown capability: %s", cap)
		}
	}

	// Section 7.1: "Paths are relative to the repository root."
	if err := validateContextPaths("include", m.Context.Include); err != nil {
		return err
	}
	if err := validateContextPaths("exclude", m.Context.Exclude); err != nil {
		return err
	}

	// Section 7.1: "Commands are argument arrays, not shell strings."
	for name, args := range m.Commands {
		if err := validateCommandArgs(name, args); err != nil {
			return err
		}
	}

	return nil
}

// windowsDriveLetterPattern matches a leading Windows drive letter such as
// "C:\" or "C:/" so that Windows-style absolute paths are recognized even
// when validation runs on a non-Windows OS (manifests are portable and may
// be authored on a different platform than the one validating them).
var windowsDriveLetterPattern = regexp.MustCompile(`^[A-Za-z]:[\\/]`)

// isAbsoluteOrRootedPath reports whether p looks like an absolute (or
// otherwise repository-root-relative-violating) path on either Unix or
// Windows, regardless of the host OS. It deliberately does not rely on
// filepath.IsAbs, since that only understands the current OS's conventions
// and a manifest authored on Linux may contain a Windows absolute path
// (or vice versa).
func isAbsoluteOrRootedPath(p string) bool {
	if p == "" {
		return false
	}
	if strings.HasPrefix(p, "/") || strings.HasPrefix(p, "\\") {
		return true
	}
	if strings.HasPrefix(p, "~") {
		return true
	}
	if windowsDriveLetterPattern.MatchString(p) {
		return true
	}
	return false
}

// escapesRepositoryRoot reports whether the cleaned, slash-normalized form
// of p would resolve outside the repository root via ".." traversal.
func escapesRepositoryRoot(p string) bool {
	normalized := strings.ReplaceAll(p, "\\", "/")
	cleaned := path.Clean(normalized)
	return cleaned == ".." || strings.HasPrefix(cleaned, "../")
}

// validateContextPaths enforces that every glob pattern in a
// context.include/context.exclude list is relative to the repository root:
// not absolute (Unix or Windows style) and not escaping via "..".
func validateContextPaths(field string, patterns []string) error {
	for _, p := range patterns {
		if isAbsoluteOrRootedPath(p) {
			return fmt.Errorf("context.%s path '%s' must be relative to the repository root (absolute paths are not allowed)", field, p)
		}
		if escapesRepositoryRoot(p) {
			return fmt.Errorf("context.%s path '%s' escapes repository root", field, p)
		}
	}
	return nil
}

// disallowedCommandSubstrings are shell metacharacter sequences that
// indicate a command array element was actually a shell string, in
// violation of "Commands are argument arrays, not shell strings." Longer,
// more specific sequences are checked before their shorter substrings
// (e.g. "&&" before a lone "&") so error messages report the most useful
// match.
var disallowedCommandSubstrings = []string{"&&", "||", ";", "|", "`", "$("}

// validateCommandArgs enforces that a manifest command entry is a non-empty
// argument array with no shell-string-shaped elements.
func validateCommandArgs(name string, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("command '%s' has an empty argument array", name)
	}
	for _, arg := range args {
		if arg == "" {
			return fmt.Errorf("command '%s' contains an empty argument", name)
		}
		for _, bad := range disallowedCommandSubstrings {
			if strings.Contains(arg, bad) {
				return fmt.Errorf("command '%s' argument '%s' contains disallowed shell metacharacter '%s'", name, arg, bad)
			}
		}
	}
	return nil
}
