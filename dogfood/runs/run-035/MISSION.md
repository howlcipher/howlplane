Build a small command-line tool in Rust, `ini2json`, that converts INI configuration files to JSON. Use only the Rust standard library (no external crates), so it builds offline with `cargo build`.

Behaviour:
- `ini2json [FILE]` reads FILE, or standard input when no file is given or FILE is `-`, and writes one JSON object to standard output.
- Keys before any section header go in the top level; `[section]` creates a nested object. Dotted section names such as `[server.tls]` nest further (`{"server": {"tls": {...}}}`).
- `key = value` and `key: value` are both accepted. Whitespace around keys and values is trimmed. Lines starting with `;` or `#` are comments, as are inline comments that follow whitespace and `;` or `#` in unquoted values. Values wrapped in double quotes keep their inner text exactly, including `;`, `#`, and spaces, and support the escapes `\"`, `\\`, `\n` and `\t`.
- All values are JSON strings, except with `--typed`, which turns `true`/`false` into booleans and integer or decimal literals into numbers. Quoted values always stay strings.
- If a key repeats within the same section, the last value wins by default. With `--strict`, a duplicate key, a duplicate section header, or a key that clashes with a nested section name is an error.
- Errors: a malformed line (for example an unterminated quote, a line with no `=` or `:`, or an empty key or section name) must exit with code 1 and print `ini2json: line N: <reason>` to standard error, with no partial output on standard output. An unreadable file exits 1 with a clear message; invalid arguments exit 2 with usage.
- The output JSON must be valid, with correctly escaped strings (including control characters and non-ASCII text), and keys in the order they first appear in the file.

Include unit and end-to-end tests run by `cargo test`, and a README with usage and examples.
