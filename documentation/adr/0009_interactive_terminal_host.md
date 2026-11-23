# ADR 0009: Interactive terminal host with a HowlFrame reducer

## Status
Proposed (TUI-00 gate). Not accepted; nothing in this ADR is implemented
beyond the probes in `integrations/howlframe/tui00/`.

## Context
The goal is a lightweight interactive terminal for one objective at a time,
preferring HowlFrame for presentation, validation, state and control. The
TUI-00 audit (`documentation/journals/2026-11-22_tui00_feasibility_gate.md`)
observed that the HowlFrame VM has no raw mode, key events or resize; that exec
is blocking and merges streams; and that exec children cannot receive `PATH` or
`HOME`, so HowlPlane run as an exec child reports installed providers as
`MISSING_EXECUTABLE` and providers could not authenticate (HP-F1). ADR 0007
keeps rich UI in HowlBoard and makes `presentation/style.py` the single color
policy.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Pure HowlFrame TUI calling the CLI through exec | most `.howl`; argv grants govern the boundary | no raw mode or resize; HP-F1 blocks discovery and execution unless `process:env` is widened past its code loading protections; no live progress |
| HowlPlane host plus a zero grant HowlFrame reducer per event (proposed) | HowlFrame decides with no authority; host already holds the operator's authority; reuses pool and orchestrator in process; bounded per event | small Python host to maintain; one VM process per event |
| Go host embedding the VM | one binary | VM is in HowlFrame `internal/`; needs a new public API |
| Python only | simplest | abandons the HowlFrame preference |

## Decision (proposed)
`howlplane tui` would be a thin host in `src/howlplane/control_plane/tui/`. It
owns the terminal and maps a closed set of reducer intents
(`inventory_refresh`, `route_preview`, `run_objective` after explicit
confirmation, `cancel`) to existing HowlPlane APIs. Each event runs the reducer
bytecode with no capability grant and finite instruction, memory and wall clock
limits from configuration, over the `howlplane.tui_step/v0` protocol. The host
never adds provider configuration, routing, approval or evidence writing of its
own. Operational events extend `OperationalLog`; evidence stays with the
orchestrator.

## Consequences
The adapter holds exactly the authority of an operator running `howlplane`.
HowlBoard keeps dashboards and multiple sessions. Acceptance requires owner
approval; implementation is TUI-01.
