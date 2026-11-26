# ADR 0010: Pure HowlFrame TUI

## Status
Accepted (2026-11-23, owner); implemented. This ADR supersedes the host half of ADR 0009. The show-only approval decision in ADR 0009 still holds. The Python host was deleted on 2026-11-24 once both engines had passed the same governed-run tests on CI (howlplane#224), so `howlplane tui` is now only this engine.

## Context
ADR 0009 split `howlplane tui` in two. HowlFrame decides state, view and intents in about 370 lines of `.howl`. About 830 lines of Python own everything else:

| Python piece | Role |
| --- | --- |
| `terminal.py` | raw mode, keys, resize, restore, paint, wrapping |
| `reducer.py` | one VM process per event, with limits |
| `app.py` | event loop, closed intent table, session log |
| `backend.py` | inventory, route, run, progress, result, cancel |
| `__init__.py` | wiring |

The split existed because HowlFrame lacked five things the TUI-00 audit recorded (HF-G1 to HF-G7, HP-F1):
- terminal input;
- a way to watch a child process without blocking;
- separate stderr;
- per-event budgets;
- any way for `howlplane`, run as an exec child, to receive `PATH` and `HOME`.

The owner asked for a pure HowlFrame TUI. HowlFrame now provides what was missing:

| Need | HowlFrame change |
| --- | --- |
| Versioned runtime errors | `howlframe.runtime_error/v1` (#112) |
| Bounds on exec children | `--max-exec-millis` and `--max-processes` (#114, #115) |
| The operator's environment for one trusted program | the runner-only `process:exec_inherit_env=` grant (#116) |
| Terminal input, timers, async children, per-event budgets | `terminal_app`: the runtime owns the terminal, timers and children; each event gets a fresh budget, and the program acts through a closed effect table (#117, #113) |

## Options and owner decisions
1. **Giving `howlplane` the operator's environment.**
   - **Inherit grant (chosen).** The runner names one absolute program that receives its environment. `process:env=` keeps refusing `PATH` and `HOME`. Only the runner can hold the grant, no child VM can request it, and the program text cannot alter the environment.
   - HowlPlane rebuilding its own environment from configuration: rejected. Provider CLIs still miss whatever the operator's shell sets.
   - A thin host just for exec: rejected, because it is not pure.
2. **Hosting a long-running interactive program.**
   - **Event-loop app kind (chosen).** It mirrors the ADR 0009 reducer, so the decision code carries over. Budgets are per event, as in HTTP handlers. Terminal restore and sanitizing live in the runtime.
   - Imperative builtins plus a budget-scope construct: rejected. Restoring the terminal, scoping budgets and reclaiming memory would become every program's problem.
3. **Rollout: parity, then delete (chosen, done).** `--engine howlframe` shipped next to the Python host (#224). Both engines pass the same governed-run tests. The Python host is deleted in a later change once that parity holds on CI.

## Decision
- **The program.** `integrations/howlframe/tui/tui_app.howl` is the whole TUI. It reuses `tui_core.howl` unchanged.
  - Its handler turns operator lines and runtime events into effects:
    - `exec_start` of `howlplane providers`, `route --probe`, `work --execute` and `cancel`;
    - `exec_signal` to interrupt the run, with a kill after the grace period;
    - `timer` and `tail` to read the `howlplane.task_progress_event/v1` stream;
    - `append_log` for `howlplane.tui_event/v1` session events;
    - `quit`.
  - Because lists are values in HowlFrame, helpers queue effects and notices on the state dict.
- **The launcher.** `howlplane tui` (`tui/howlframe_engine.py`) holds no TUI logic.
  - It writes a private entry script (0700). HowlFrame refuses to exec a symlink, and a venv interpreter usually is one.
  - It resolves canonical paths and refuses any containing whitespace or a comma, which a grant token cannot carry.
  - It then `execv`s `howlframe run` with exactly these grants:
    - `process:exec_inherit_env=<entry>`;
    - seven exact-argv `process:exec=<entry> ...` grants: providers, two route variants, two work variants and cancel. Each puts free text after `--`;
    - `filesystem:read=<repo>/.task_runs`;
    - `filesystem:write=<state>/logs`.
  - It passes the `[tui]` limits as `--max-event-instructions`, `--max-event-memory-bytes` and `--max-state-bytes`.
- **Unchanged.** Evidence, routing, approval and provider configuration stay with HowlPlane commands; cancel goes through `howlplane cancel`, so it records evidence exactly as the CLI does. Approvals stay show-only.

## Consequences
- **Authority.** It is the operator's authority, limited to seven argv shapes of one program plus two directories, which is narrower than the Python host.
- **Cost.** Every inventory and route preview starts a `howlplane` process, about a second each, where the Python host stayed in process. Since 2026-11-26 the two run at the same time (see the amendment below).
- **Paths.** Paths with whitespace or commas are refused until grants can quote them. The Python engine that once covered them was removed.
- **HowlFrame runtime.** The engine needs a HowlFrame runtime with `terminal_app`, so HowlPlane's CI pin moves to that commit.
- **What remains non-HowlFrame:** `howlplane` itself, as the exec child, and the launcher.
- **Building with HowlFrame alone.** `scripts/build_tui_app.sh` runs `howlframe build`, then `scripts/tui_provenance.howl`. That program hashes the sources and the artifact into `ARTIFACT_PROVENANCE.json` and writes the packaged copy, holding only read and write grants on those two directories. The TUI is now written, built and run with HowlFrame; outside it are `howlplane`, which the TUI drives, and the HowlFrame runtime.
- **Running without the launcher.** A HowlFrame program cannot grant itself authority, so someone outside it has to choose the grants. `howlplane tui --print-command` prints the launcher's exact `howlframe run` line, without a fixed session id, so the operator can review it and run the TUI on HowlFrame alone. The tests run that printed line directly.

## Amendment (2026-11-26): operator improvements

The owner chose six improvements. HowlFrame gained what the runtime has to own (howlframe#318, #319, merged as `e64256e`):
- `--replay`, which runs a `terminal_app` against recorded events with no effect executed;
- a line editor with in-memory history;
- bracketed paste, with `pasted` on every `line` event;
- view entries styled from a fixed palette, under `--color`;
- scrolling.

The TUI uses them as follows.

- **Paste-safe confirmation.** A pasted `y` or `yes` at the run confirmation is refused with a notice, and the prompt stays open. A typed answer is needed.
  - Considered: refusing every pasted line in confirm mode. Rejected: a pasted `n` or `/quit` is harmless.
  - The objective itself may be pasted.
- **Faster preview.** The inventory and the route are probed concurrently. Each probe's process id carries a generation (`inventory-N`, `route-N`), so a reply from a replaced probe is ignored, and a new preview never waits for an old one. Options:
  - **Generation ids, stale replies ignored (chosen).** Pro: the latest objective is probed at once, and nothing is killed. Con: a superseded probe runs to completion. It is bounded by typing speed and the runtime's `--max-processes`.
  - **One probe per kind, coalescing newer requests.** Pro: at most two probes run. Con: the latest objective waits for the stale probe to finish.
  - **Terminating the superseded probe.** Pro: no wasted work. Con: it signals a HowlPlane child mid-way, for little gain. Rejected while probes stay about a second.
  - Because the route can arrive before the inventory, the preview keeps the route document and re-checks it against the inventory. Once both are in, a selection the inventory does not report as usable gets the notice the TUI-00 reducer had. That notice was lost in the port.
  - If the inventory clears an explicit provider choice, the route is asked for again.
- **Colour.** `tui_core.render` returns `{"text", "style"}` entries:
  - failures are `error`;
  - a selected route and an outcome that exited 0 are `ok`;
  - a blocked route and any other outcome are `warning`;
  - notices are `info`.

  The launcher passes HowlPlane's colour policy (the global `--color`, then `HOWLPLANE_COLOR`) as the runtime's `--color`. The runtime keeps the `NO_COLOR` and `TERM=dumb` rules.
- **Runtime check.** The launcher reads the runtime's `howl.interop/v1` descriptor. It refuses to start unless `run` offers every flag it passes. `--color` stands for paste-marked line events, which arrived in the same change. The error says what to install.
- **Tests without authority.** `tests/test_tui_logic.py` drives the committed artifact through `--replay` with the TUI-00 fixtures. That restores the decision-level coverage the ADR 0009 reducer tests gave. The tests need no grant, terminal or child.
- **Fixed while testing.** An unreadable child reply suggested `howlplane status run`, the process id, and told the operator to reconcile a read-only probe. It now names the task for a run or cancel, and `howlplane doctor` with a safe retry for a probe.

