invoicegen currently reads its settings straight from `process.env` in several places. We want to deploy it for multiple clients, and environment variables alone are getting unwieldy. Please refactor configuration handling:

1. Move all configuration loading into a single module so the rest of the code receives a plain config object instead of reading `process.env` directly.
2. Support a JSON config file. Use the file given by a new `--config PATH` option; when that option is absent, use `.invoicegen.json` in the current working directory if it exists. Keys: `company`, `currency`, `taxRate`, `outDir`.
3. Precedence, highest first: environment variables, then the config file, then built-in defaults. A relative `outDir` in a config file is resolved relative to that file's directory.
4. Validate the final configuration: `taxRate` must be a number from 0 to 1, `currency` must be three uppercase letters, and `company` must be a non-empty string. Invalid configuration, an unreadable or malformed `--config` file, or an unknown key in the file must exit with code 2 and a message naming the problem, before any output is written. (A missing auto-discovered `.invoicegen.json` is not an error.)

Compatibility: users who configure only through the existing environment variables must see exactly the same output and behaviour as today, including the current defaults. Keep the existing tests passing, add tests for the new behaviour (precedence, discovery, relative outDir, each validation error), and update the README.
