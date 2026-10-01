# ADR 0007: Terminal presentation layer without a UI dependency

## Status
Accepted.

## Context
The CLI needs strong visual hierarchy (state, meaning, next action), color that
degrades cleanly, and tables. HowlBoard remains the rich graphical surface, so
this must stay a plain CLI that also serves SSH, scripts, CI and other agents.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Rich | Tables, spinners, wide terminal handling | New base dependency (the `ui` extra is deliberately empty), import cost on every command, harder deterministic tests, styling leaks into handlers |
| Textual / Streamlit | Full dashboards | Competes with HowlBoard; heavy; not scriptable |
| Small stdlib module (chosen) | No dependency, no startup cost, one policy point, trivially testable | We maintain about 150 lines (table, badge, duration) |

## Decision
`presentation/style.py` resolves a `Style` per stream: color only on a TTY, not
under `NO_COLOR`, `TERM=dumb` or `--color never`; Unicode glyphs only on a UTF-8
TTY; transient phase lines only on an interactive non-CI stderr. Handlers call
helpers (`header`, `kv`, `table`, `command_block`) and never embed escape codes.
Meaning is always carried by words; stripping ANSI from styled output yields the
plain output exactly (tested). JSON paths never touch the module.

## Consequences
Revisit only if tables need features the module cannot provide cheaply. Any
change must keep the plain-text output complete.
