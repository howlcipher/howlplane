package main

import (
	"errors"
	"fmt"
	"os"

	"github.com/howlcipher/howlplane/pkg/cli"
)

func main() {
	if err := cli.NewLegacyRootCommand().Execute(); err != nil {
		var engineErr *cli.EngineExitError
		if errors.As(err, &engineErr) {
			// The engine already reported its own error to stderr.
			os.Exit(engineErr.Code)
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
