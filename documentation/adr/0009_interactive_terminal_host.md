# ADR 0009: Interactive terminal host with a HowlFrame reducer

## Status
Accepted (2026-11-23, owner). Implemented in TUI-01 as `howlplane tui`.

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
| HowlPlane host plus a zero grant HowlFrame reducer per event (chosen) | HowlFrame decides with no authority; host already holds the operator's authority; reuses pool and orchestrator in process; bounded per event | small Python host to maintain; one VM process per event |
| Go host embedding the VM | one binary | VM is in HowlFrame `internal/`; needs a new public API |
| Python only | simplest | abandons the HowlFrame preference |

## Decision
`howlplane tui` is a thin host in `src/howlplane/control_plane/tui/`. It
owns the terminal and maps a closed set of reducer intents
(`inventory_refresh`, `route_preview`, `run_objective` after explicit
confirmation, `cancel`, `quit`) to existing HowlPlane APIs. Each event runs the reducer
bytecode with no capability grant and finite instruction, memory and wall clock
limits from configuration, over the `howlplane.tui_step/v1` protocol. The host
never adds provider configuration, routing, approval or evidence writing of its
own. Operational events extend `OperationalLog`; evidence stays with the
orchestrator.

## Consequences
The adapter holds exactly the authority of an operator running `howlplane`.
HowlBoard keeps dashboards and multiple sessions.

## Implementation (TUI-01)
* Reducer: `integrations/howlframe/tui/tui_reducer.howl` with `tui_core.howl`,
  committed as `tui_reducer.hfbc` and packaged under
  `src/howlplane/control_plane/data/`. It is built by
  `scripts/build_tui_reducer.sh` with HowlFrame at the CI runtime pin (`928c005` since 2026-11-23, `41d56ce` before);
  the published v0.1.0 producer rejects dict literals with mixed value types.
  `ARTIFACT_PROVENANCE.json` beside it holds the hashes, and a test rebuilds the
  artifact and compares bytes.
* Host: `src/howlplane/control_plane/tui/` (`reducer.py` runs a step,
  `backend.py` is the only path into HowlPlane, `terminal.py` owns the TTY,
  `app.py` holds the closed intent table).
* Runs are `howlplane work OBJECTIVE --execute --json` children with the
  operator's environment; progress is the `howlplane.task_progress_event/v1`
  stream (the `howlplane.task_progress/v1` snapshot for older runs); cancel
  interrupts the child, then calls `HumanLifecycleManager.cancel` with the
  control plane ledger, as `howlplane cancel` does.
* **Approvals are show-only (owner decision, 2026-11-23).** At the owner
  boundary the TUI shows the decision packet path and the `approve`, `resume`
  and `reject` commands from `howlplane.work_result/v1`, and the intent table
  has no approve or reject intent. An approval stays a deliberate command the
  owner types, outside the interactive loop.
* Limits are the `[tui]` table of the operator TOML (`TuiSettings`). A step
  measured at most about 610 instructions, so the default budget is 50000.
* Events are `howlplane.tui_event/v1` lines written by `OperationalLog` with
  `schema`, `seq`, `session_id`, directory 0700 and file 0600, under
  `$XDG_STATE_HOME/howlplane/tui/logs/tui_events.jsonl`.
