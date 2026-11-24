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
- **Cost.** Every inventory and route preview starts a `howlplane` process, about a second each, where the Python host stayed in process.
- **Paths.** Paths with whitespace or commas need the Python engine until grants can quote.
- **HowlFrame runtime.** The engine needs a HowlFrame runtime with `terminal_app`, so HowlPlane's CI pin moves to that commit.
- **What remains non-HowlFrame:** `howlplane` itself, as the exec child, and the launcher.
- **Running without the launcher.** A HowlFrame program cannot grant itself authority, so someone outside it has to choose the grants. `howlplane tui --print-command` prints the launcher's exact `howlframe run` line, without a fixed session id, so the operator can review it and run the TUI on HowlFrame alone. The tests run that printed line directly.
