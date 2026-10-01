// Package version defines release metadata for the HowlPlane Go CLI
// (cmd/howlplane, cmd/ai). Bumped alongside each tagged release and
// injected at build time via -ldflags; the zero-value default below is
// only ever seen in a `go run`/local build.
package version

import "runtime/debug"

// Version is the HowlPlane release this build belongs to, normally
// injected at build time with:
//
//	-ldflags "-X github.com/howlcipher/howlplane/internal/version.Version=$VERSION"
var Version = "0.0.0-dev"

func init() {
	if Version != "0.0.0-dev" {
		return
	}
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" && info.Main.Version != "(devel)" {
		Version = info.Main.Version
	}
}
