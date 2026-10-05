# Mission selection rationale (run-035, adaptive tranche 1, Mission E)
Recent: 030 Go (existing), 031 Python (existing, HTTP), 032/033 Node (existing, dirty tree), 034 Python (test-only).
Greenfield work has only ever been the Python household CLI (001-029).
This mission instead tests:
- greenfield in a fourth language, Rust (cargo project creation, cargo test discovery, Claude/Codex grants for cargo)
- file transformation / parsing with a precise grammar and many edge cases (quoting, escapes, comments, nesting, duplicates)
- correct output encoding (JSON escaping of control and non-ASCII characters, key order)
- error semantics (line-numbered errors, no partial output, distinct exit codes)
- offline constraint (std only)
Second mission on engine 175b57a; with run-034 clean, a clean run here would complete the tranche's final clean pair.
