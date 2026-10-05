# run-035 independent verification (operator, 2026-10-04)
Target dogfood-missions/run-035/ini2json (greenfield; empty commit 3504fd3). New: Cargo.toml (no dependencies), Cargo.lock, src/, tests/, README.md, .gitignore (/target/).
- `cargo clean && cargo build --release` offline OK; `cargo test`: 14 tests pass (8 unit, 6 end-to-end).
- Independent oracle written from the spec before reading the code (operator_oracle_cases.py, 19 cases): basic, key order, dotted nesting, comments (incl. `u;not` literal), quoted values with escapes, --typed, untyped, last-wins, --strict duplicate key/section/clash (line numbers), unterminated quote, missing separator, empty key, empty section, Unicode, empty input, empty value, CRLF: 19/19 pass. Errors leave stdout empty and start with `ini2json: line N`.
- --typed: true/false -> booleans, 42/-3.5 -> numbers, quoted "7" -> string; `1e3` and `yes` stay strings (documented number grammar).
- JSON escaping: \x01 -> \u0001 (valid JSON), DEL passed through (valid), non-ASCII raw UTF-8.
- Exit codes: missing file 1, directory 1, two FILEs 2 + usage, unknown option 2 + usage, `-` reads stdin.
- README examples run and match byte for byte (example.ini --typed, stdin, error example).
- Leak scan: none.
Verdict: CLEAN.
