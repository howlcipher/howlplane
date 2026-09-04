// Package version defines release metadata for the HowlPlane Go CLI
// (cmd/howlplane, cmd/ai). Bumped alongside each tagged release and
// injected at build time via -ldflags; the zero-value default below is
// only ever seen in a `go run`/local build.
package version

// Version is the HowlPlane release this build belongs to, normally
// injected at build time with:
//
//	-ldflags "-X github.com/howlcipher/howlplane/internal/version.Version=$VERSION"
var Version = "0.0.0-dev"
