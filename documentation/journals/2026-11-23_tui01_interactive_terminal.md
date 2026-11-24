# TUI-01: `howlplane tui`

Date: 2026-11-23. Implements ADR 0009 (accepted by the owner on this date) on
the evidence of `2026-11-22_tui00_feasibility_gate.md`.

## What was built

| Part | Location | Notes |
| --- | --- | --- |
| Reducer | `integrations/howlframe/tui/tui_reducer.howl`, `tui_core.howl` | `howlplane.tui_step/v1` in, `howlplane.tui_step_result/v1` out (`state`, `view`, `prompt`, `intents`); no capability grant |
| Artifact | `tui_reducer.hfbc`, packaged copy under `src/howlplane/control_plane/data/` | built by `scripts/build_tui_reducer.sh` with HowlFrame `41d56ce`; hashes in `integrations/howlframe/tui/ARTIFACT_PROVENANCE.json` |
| Reducer runner | `src/howlplane/control_plane/tui/reducer.py` | empty environment, `--max-instructions`, `--max-memory-bytes`, `--deadline`, host input cap, fail closed |
| Backend | `src/howlplane/control_plane/tui/backend.py` | pool read only with `probe_on_start=True`; run is `howlplane work --execute --json`; cancel through `HumanLifecycleManager.cancel` |
| Terminal | `src/howlplane/control_plane/tui/terminal.py` | raw mode, alternate screen, SIGWINCH, restore on exit, SIGTERM and SIGHUP; line mode without a TTY |
| Loop | `src/howlplane/control_plane/tui/app.py` | closed intent table: `inventory_refresh`, `route_preview`, `run_objective`, `cancel`, `quit` |
| Config | `TuiSettings` in `config_loader.py`, `[tui]` table | every limit finite and range checked |
| Events | `OperationalLog` options `schema`, `sequenced`, `private`, `filename` | `howlplane.tui_event/v1`; Factory events byte identical |

## Decisions made while building

* **Producer.** The published v0.1.0 compiler rejects dict literals whose
  values differ in type (`dict value 2 has type string, want bool`); the CI
  runtime pin `41d56ce` accepts them. The artifact is built with `41d56ce`,
  which CI already builds from source, and recorded in its own provenance file
  rather than changing the existing v0.1.0 provenance. Output was byte
  identical across builds and ran on `41d56ce`, `9c739c9` and the patched VM.
* **Run boundary.** A run is a child `howlplane work` with the operator's
  environment instead of an in-process orchestrator call: identical code path
  to the CLI, its stdout cannot corrupt the screen, and it can be interrupted.
  `--progress never` also disables `progress.json`, so the child runs with
  progress on and its stderr goes to an owner-only file.
* **Instruction budget.** The largest measured step (seven resources) needed
  about 610 instructions, so the default is 50000.
* **Notices** accumulate across the follow-up steps of one input and reset on
  the next input; a smoke run showed a provider warning being overwritten.

## Test Impact Assessment

* Behavior added: the `howlplane tui` command; `OperationalLog` keyword
  options; the `[tui]` configuration table. Existing behavior changed: none
  (Factory event shape is asserted byte for byte).
* New tests: `test_tui_reducer.py` (artifact provenance, rebuild equality, no
  grant, every interaction rule, budget and size refusals), `test_tui_app.py`
  (fake backend: preview never runs, typed confirmation, progress, outcome,
  cancel by Ctrl-C, `/cancel` and `/quit`, failures displayed, unknown intent
  refused, private sequenced log), `test_tui_backend.py` (real pool: readiness
  probed, missing executable BLOCKED, nothing launched, read only; run argv;
  result and error parsing; cancel escalation), `test_tui_terminal.py` (line
  mode; raw keys, UTF-8, Ctrl-C, Ctrl-D and restore in a real pty; restore on
  SIGTERM), `test_tui_session.py` (the real command in a pty: preview, decline,
  exit, nothing launched, no task created), `test_oplog_options.py`.
* Tiers: unit (terminal, oplog), contract (backend), integration (reducer, app,
  session; they need the real HowlFrame runtime).
* Two pty test races were found and fixed in the tests (typing before raw mode;
  reading `RESTORED` before ` True`); 40 consecutive runs were then clean.
* Not covered: a real governed run (it would call a provider), so the
  `start_run` to `result` path is proven with stand-in children and the real
  command line, not a provider.
* Tests run: see the final handoff of this task.

## TUI-02 follow-up (2026-11-23)

* **Governed run proven without a provider.** `tests/test_tui_governed_run.py`
  drives the real `howlplane tui` in a pseudo terminal through a real
  `howlplane work --execute` child, with only the agent executable replaced by
  a stand-in. A completing stand-in reaches the owner boundary
  (`awaiting_human`) and the screen shows the decision packet path and the
  `approve`, `resume` and `reject` commands; a hanging stand-in is cancelled and
  the ledger shows `implementation_started` then `task_cancelled`. Evidence goes
  to a temporary control plane root (`HOWLPLANE_HOME`); the checkout ledger is
  asserted untouched.
* **Approvals stay show-only** (owner decision): the TUI displays
  `next_action` from `howlplane.work_result/v1` (detail, command, then,
  alternatives) and never proposes an approve or reject intent; a reducer test
  pins that.
* **Defects found and fixed while building this:** cancel recorded no evidence
  (`ledger=None`), so the TUI now resolves the ledger exactly as
  `howlplane cancel` does; long lines were cut at the terminal width, hiding the
  decision packet path, so `RawTerminal` now wraps them.
* The reducer artifact is now built with the CI pin `928c005`.

### Runbook: one live acceptance run

`tests/test_tui_live_run.py` is the only test that reaches a real provider. It is
deselected unless `HOWLPLANE_LIVE_PROVIDERS` is set and skips when no configured
provider is `READY`. Run it once on a machine where a provider CLI is installed
and signed in:

```bash
HOWLFRAME_BIN=/path/to/howlframe HOWLPLANE_LIVE_PROVIDERS=1 \
  pytest tests/test_tui_live_run.py -p no:randomly -rs
```

It creates a scratch repository under the pytest temporary directory, asks for a
one-line file, confirms the run, and waits up to 30 minutes for an outcome. Keep
as evidence: the pytest output, the task's `.task_runs/<task_id>/` directory from
the temporary repository, and the session log under the temporary
`XDG_STATE_HOME`. It costs one provider session; it writes nothing to the
checkout's ledger.


## Pure HowlFrame engine (ADR 0010)

The owner asked for the TUI in pure HowlFrame. HowlFrame gained what the
Python host had been covering for (howlframe#311 to #314, merged as
`dffcb00`):
- the versioned `howlframe.runtime_error/v1`;
- `--max-exec-millis` and `--max-processes`;
- the runner-only `process:exec_inherit_env=` grant;
- `terminal_app`, a runtime-owned event loop with per-event budgets and a closed effect table.

`integrations/howlframe/tui/tui_app.howl` is now the whole TUI on that runtime.
`howlplane tui --engine howlframe` launches it with an exact grant profile.

### Issues found while porting
- **List mutation.** HowlFrame lists are values, so `append` inside a helper never reaches the caller. Effects and notices are now queued on the state dict, which helpers share.
- **Cancel argv.** The cancel reason had spaces, which a grant token cannot carry, so cancel was denied. A failed cancel also left the session unable to quit. The reason now comes from the launcher config (`cancel_reason`), and a failed cancel ends the cancel and still honors quit.
- **Symlinked interpreter.** A venv interpreter is a symlink, which `exec` refuses. The launcher writes a private 0700 entry script whose interpreter line keeps the venv.

### Test Impact Assessment
- **Behavior added:** a second TUI engine. The default engine is unchanged.
- **Tests that prove it:**
  - `tests/test_tui_governed_run.py` now runs both scenarios on both engines: the run reaching the owner boundary with the show-only commands, and cancel with ledger evidence.
  - `tests/test_tui_howlframe_engine.py`:
    - grant profile exactness and no coarse authority;
    - launch argv, limits, config, and the entry and log directory modes;
    - refusal of paths a grant cannot carry;
    - the committed artifact equals the source build and the packaged copy;
    - preview never runs, and a run needs typed confirmation;
    - command notices and a blocked route;
    - the session log is private, versioned and sequenced.
- **Existing tests:** they still express intended behavior. The governed-run tests were parametrized rather than duplicated.
- **Out of scope here:** the Python-host-only unit tests (`test_tui_app.py`, `test_tui_backend.py`) remain until the Python host is deleted. Their engine-level counterparts are the tests above.
- **Run:** the TUI suites on both engines, then the full gate at pin `dffcb00`.
