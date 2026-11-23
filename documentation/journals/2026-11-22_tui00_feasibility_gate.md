# TUI-00: HowlPlane interactive terminal, architecture and feasibility gate

Date: 2026-11-22. Status: gate report, nothing implemented beyond probes.

## Scope and audited revisions

| Repository | Revision | Notes |
| --- | --- | --- |
| HowlPlane | `e175e67` (main) | Python control plane, `src/howlplane/control_plane/` |
| HowlFrame | `9c739c9` (main), HowlFrame 0.2.0, HFBC format 2 | runtime audited from source |
| HowlFrame | `41d56ce` (HowlPlane CI pin, `.github/workflows/test.yml:64`), HowlFrame 0.1.0, HFBC format 1 | every probe and test was rerun here |

Goal under evaluation: a lightweight interactive terminal application that
accepts a natural language objective, discovers eligible AI resources, supports
automatic or explicit selection, runs the objective through HowlPlane's governed
execution, and shows progress, structured errors, diagnostics and verified
outcomes. No AI provider, browser or desktop application was used. Every probe
ran serially with one worker under explicit time, instruction and memory limits.

Probes live in `integrations/howlframe/tui00/`; the reproducible checks are
`tests/test_tui00_feasibility.py` (integration tier).

## 1. Current architecture audit

### 1.1 HowlPlane interfaces a TUI would reuse

| Concern | Owner (source) | Contract and exact version | Surface | Observed |
| --- | --- | --- | --- | --- |
| Provider inventory | `ProviderPoolManager` (`synthesis/provider_pool.py`), `resource_cli.inventory_document` | `howlplane.ai_resources/v1` (`resource_models.py:11`) | `howlplane providers --json`, exit 0; in process `pool.inventory()` | Real output captured into `fixtures/ai_resources_*.json` |
| Readiness | `SubprocessAgentBackend.probe_readiness` (`agent_execution.py:619`) | `ReadinessStatus` enum (`resource_models.py:31`), unversioned | inside the inventory | `shutil.which` only; the agent is never launched (verified with a fake executable that records invocations) |
| Routing | `ProviderPoolManager.select_resource` | `howlplane.resource_selection/v1` (`resource_models.py:10`), `blocked_outcome()` | `howlplane route OBJECTIVE [--agent ID] [--role R] --json`, exit 0 selected, 3 blocked (`cli.py:758`) | SELECTED and BLOCKED captured |
| Governed execution | `GovernedTaskOrchestrator.run` (`orchestrator.py`), `cmd_work` (`cli.py:625`) | `howlplane.work_result/v1` (`presentation/work_result.py:24`), additive changes keep v1 | `howlplane work OBJECTIVE --execute --json --progress never`; without `--execute` it prepares a task and writes run artifacts | Not executed (would invoke AI); serializer exercised with a stub result |
| Errors | `cli._report_failure` (`cli.py:2878`), `presentation/errors.py` | `howlplane.error/v1`: `code, message, why, next_action, command`, optional `diagnostic_id, expected_recovery` | printed to **stderr** when `--json` is set, exit 1 | Captured `fixtures/error_not_a_repo.json` |
| Task progress | `TaskProgressTracker` (`progress.py`) | `howlplane.task_progress/v1` snapshot, atomically replaced at `.task_runs/<task_id>/progress.json` | plus unversioned `[HowlPlane] PHASE | ...` text lines on stderr | Source review only |
| Operator diagnostics | `doctor.py`, `doctor_report.py` | `howlplane.doctor/v1` | `howlplane doctor --json` | Not probed in this gate |
| Evidence | `EvidenceLedger` (`evidence_ledger.py`) | `ai.evidence_entry/v1`, `schemas/evidence-entry.schema.json`; required `schema, entry_id, task_id, agent_id, action, timestamp` | append only JSONL, flock, gzip rotation at 64 MiB, schema validated on append | Source review only |
| Recovery | `CrashRecoveryEngine` (`recovery.py`) | `RetryClassification`: `SAFE_TO_RETRY, RECONCILE_FIRST, NEVER_AUTO_RETRY, ALREADY_COMPLETE, HUMAN_DECISION_REQUIRED`, unversioned | `howlplane resume`, `cancel`, `status --json` | Source review only |
| Operational log | `factory/oplog.OperationalLog` | unversioned JSONL `ts, severity, code, message` plus context; redacted, 500 character cap, 10 MiB rotation keeping 5 | `howlplane logs --json` (Factory scoped) | Source review only |
| Redaction | `presentation/redact.redact_operator_text` | the documented single primitive for operator text | used by errors and oplog | A second, different primitive exists: `evidence_ledger.redact_sensitive_data` |
| Failure taxonomy | `ProviderFailureClass` (`resource_models.py:52`) | unversioned enum; `schemas/failure-class.schema.json` (`ai.failure_class/v1`) was not compared | | |
| Run events | `documentation/TASK_EVENT_SPEC.md` | `ai.run_event/v1` | **no Python producer**: the only references are the schema, the Go `internal/runtime` types and a drift test | Not reusable as a live stream today |
| Existing HowlFrame use | `howlframe_runner.py` | HowlPlane calls `howlframe run --max-instructions N artifact.hfbc JSON` with a 10 s timeout and normalizes the output | | The direction is Python host to HowlFrame program, which is the shape recommended below |

Architectural guardrails already recorded: ADR 0007
(`documentation/adr/0007_terminal_presentation_layer.md`) makes
`presentation/style.py` the single color and glyph policy (TTY only, `NO_COLOR`,
`TERM=dumb`), forbids escape codes in handlers, and keeps rich UI in HowlBoard.
`documentation/USER_GUIDE.md` section 3 states HowlPlane ships no dashboard.

### 1.2 HowlFrame runtime facts (bytecode VM, `internal/vm/vm.go`)

* I/O opcodes: `PRINT` (always appends a newline), `STDERR`, `READ_LINE`
  (line buffered, trims `\n` and `\r`), `READ_STDIN` (whole input, byte exact),
  `CLI_ARGS`, `ENV` (needs `environment`). There is no opcode for raw mode,
  single key reads, terminal size or signals (`docs/reference/bytecode_reference.md`).
* `EXEC` (`vm.go:2840`): exact argv allow-list (`process:exec=PATH pattern...`),
  no shell, no PATH lookup, symlink-free absolute program, children start with an
  empty environment and `process:env=` refuses `PATH`, `HOME` and other code
  loading names (`docs/reference/process_authority.md`). The child runs under a
  supervisor that ends its process group (`exec_contain_linux.go:60`). Stdout and
  stderr share one buffer bounded by `--max-exec-output-bytes`; stdin is not
  connected; the call blocks until exit and returns bytes.
* Limits (`internal/vm/error.go:170`): instructions 100000, memory 64 MiB
  cumulative allocation, call depth 1000, exec output 10 MiB, optional wall
  clock `--deadline`. Zero or unlimited values are refused.
* `SPAWN` (`vm.go:2926`) starts a goroutine with a copy of variable bindings that
  still shares lists and dicts, discards every failure (`recover()`), and builds
  its child VM without `executed`, `spawnDepth` or `callDepth`. `SPAWN_AGENT`
  (`vm.go:4046`) is synchronous and does carry `executed` back.
* Modules: `(use "file.howl" as alias)` with exported `defun`; nested module
  imports are rejected (`docs/module_system_design.md:71`).
* Discovery: `howlframe agent capabilities` emits `howlframe.agent/v1`
  (188 constructs, arity only).

## 2. Feasibility matrix

Classification uses only what was observed in this gate. Documented behavior
that was not exercised is UNKNOWN. Counts and timings were measured on
`9c739c9`; pass or fail outcomes were confirmed on both revisions by the test
module.

### 2.1 HowlFrame runtime capabilities

| Capability | Class | Evidence |
| --- | --- | --- |
| Terminal output, ANSI escapes | VERIFIED_SUPPORTED | `\u001b`, `\x1b`, `\033` emitted verbatim; pty session showed colored lines |
| Line input | VERIFIED_SUPPORTED | `read_line` loop under a real pty accepted three lines and `quit` |
| Interactive keyboard events | UNSUPPORTED | no opcode; `read_line` waits for Enter |
| Raw terminal mode | UNSUPPORTED | no opcode; `exec /usr/bin/stty -a` from inside a pty fails with `Inappropriate ioctl for device` because exec children get no terminal |
| Screen redraw | PARTIALLY_SUPPORTED | clear and cursor escapes work; every `print` appends a newline; reading `TERM` or `NO_COLOR` needs the `environment` grant |
| Terminal resize | UNSUPPORTED | no signal or window size access |
| Dynamic variables and collections | VERIFIED_SUPPORTED | nested dict and list state carried across reducer steps; `append` and `map_set` need a variable target; there is no `nil` literal (`undefined reference to "nil"`), `(map_get (dict) "none")` works |
| JSON processing | VERIFIED_SUPPORTED | real HowlPlane documents parsed; `encode_json` sorts keys and keeps `null`; malformed input is a catchable `RUNTIME_ERROR`. See HF-D4 |
| Safe subprocess invocation | VERIFIED_SUPPORTED | shell metacharacters crossed verbatim and were not evaluated; an objective starting with `-` and every ungranted argv shape were denied before the boundary ran (its invocation log stayed empty) |
| Environment for real HowlPlane | PARTIALLY_SUPPORTED | application names pass with `process:env=`; `PATH` and `HOME` cannot, see HP-F1 |
| Live subprocess output | UNSUPPORTED | exec returns only after the child exits |
| Separate stdout and stderr | UNSUPPORTED | one combined buffer; a stderr progress line turned a valid JSON reply into `MALFORMED_OUTPUT` |
| Per call timeout | UNSUPPORTED | only the global `--deadline`, which is not catchable and ends the program; it did kill the hung child within 2.02 s |
| Concurrency | PARTIALLY_SUPPORTED, unsafe | `spawn` works but see HF-D1, HF-D2; no join or result channel; child failures are silent |
| Error handling | VERIFIED_SUPPORTED | `try_let` caught `CAPABILITY_DENIED`, `IO_ERROR` (with `exit_code` and `output`), exec output `LIMIT_EXCEEDED` and parse errors. Not catchable: instruction limit, wall clock deadline |
| Structured diagnostics | PARTIALLY_SUPPORTED | stderr JSON `phase, code, function, instruction, opcode, message`, no schema version field; HF-D2 bypasses it with a Go stack trace |
| Instruction bound | VERIFIED_SUPPORTED per VM | fails closed at the budget (reducer step needs 419, fails at 418); cumulative per run; bypassed by `spawn` (HF-D1) |
| Memory bound | PARTIALLY_SUPPORTED | `READ_STDIN` charged; `READ_LINE` (HF-D3) and decoded JSON trees (HF-D4) are not |
| Process count bound | UNSUPPORTED | 20 `spawn` bodies produced 20 concurrent boundary children (41 processes with supervisors) |
| Process cleanup | VERIFIED_SUPPORTED | no boundary child survived VM SIGINT (rc 130), SIGTERM (143), SIGKILL (137), the deadline, or main exit |
| Call depth bound | UNKNOWN | documented and tested upstream, not exercised here |
| Modules and reusable abstractions | PARTIALLY_SUPPORTED | `use` plus exported `defun` returning dicts and lists works; nested imports are rejected; untyped parameters can be inferred wrongly, so `type_hints` were required |
| OOP style polymorphism | UNKNOWN | `struct` exists; not needed by the probes |

### 2.2 Proposed user experience

| Step | Pure HowlFrame (VM owns terminal, exec to `howlplane`) | Recommended split (HowlPlane host plus zero grant HowlFrame reducer) |
| --- | --- | --- |
| Type an objective | line mode only | VERIFIED (reducer trims, bounds and proposes a `route_preview` intent) |
| Live key handling, resize | UNSUPPORTED | host owns raw mode and resize (standard library `termios`, `SIGWINCH`); not yet built |
| Discover resources | wrong answer, HP-F1 | VERIFIED in process: `pool.inventory()` with the operator environment |
| Provider becomes unavailable | VERIFIED in reducer | VERIFIED: explicit choice cleared, notice raised, unusable choice refused |
| Routing preview without AI | misleading, HP-F2 | VERIFIED: in process `from_config(read_only=True, probe_on_start=True)` BLOCKED with `MISSING_EXECUTABLE` exclusions, wrote no capacity file and launched nothing |
| Explicit provider | VERIFIED via `--agent` argv shape | VERIFIED: `route --agent ID` honors the eligible set; unknown and unconfigured IDs are `BLOCKED` |
| Run the objective | UNSUPPORTED: providers lose `HOME` and `PATH` | UNKNOWN until TUI-01; same code path as `work --execute` |
| Progress | UNSUPPORTED (blocking exec) | PARTIALLY: `task_progress/v1` snapshots exist; no sequenced event stream |
| Structured errors | VERIFIED | VERIFIED; retry safety is not in `error/v1` and is shown as `UNKNOWN`, never guessed |
| Verified outcome | VERIFIED parsing | VERIFIED parsing of `work_result/v1`; verification status reported verbatim |
| Bounded resources | per run budgets end long sessions | VERIFIED per step on `9c739c9`: median 5.2 ms, p95 7.4 ms over 30 runs, 14.2 MiB peak RSS, 419 instructions for an inventory step |

## 3. Reusable HowlPlane integration contracts

The TUI consumes these versions and fails closed on any other (tested for
inventory and routing):

1. `howlplane.ai_resources/v1` for discovery.
2. `howlplane.resource_selection/v1` for preview, including `exclusions[].stage`.
3. `howlplane.work_result/v1` for outcomes; `next_action.command` is the only
   command the TUI may suggest.
4. `howlplane.error/v1` for failures.
5. `howlplane.task_progress/v1` for progress snapshots.
6. `ai.evidence_entry/v1`, read only, for independently recorded evidence.
7. `RetryClassification` names from `recovery.py` for retry safety.

Contract gaps, all additive:

* HP-C1: `howlplane.error/v1` has no retry field. Add optional `retry`
  carrying a `RetryClassification` value where the raiser knows it. Additive, so
  v1 stays. **Resolved 2026-11-23** as the optional `retry_safe` field
  (`presentation/errors.py`), set only for failures raised before anything ran
  or that need an owner decision.
* HP-C2: progress has no sequenced event stream. Either give the
  existing stderr lines a JSON mode or add a progress callback on
  `TaskProgressTracker`; `ai.run_event/v1` is specified but has no producer.
* HP-C3: `route` does not probe readiness (`cli.py:762`), so its preview can
  select a resource the inventory reports as `MISSING_EXECUTABLE`. A TUI host
  calling the pool in process needs no change; the CLI would need a `--probe` flag.
  **Resolved 2026-11-23**: `howlplane route --probe` (opt in; the default is
  unchanged).

## 4. Logging and error architecture

### 4.1 One lifecycle event shape

Proposed `howlplane.tui_event/v1`, one JSON object per line:

| Field | Meaning |
| --- | --- |
| `schema` | `howlplane.tui_event/v1` |
| `session_id` | correlation id for one TUI session (random, not derived from user data) |
| `task_id` | HowlPlane task id once known; joins progress, run directory and evidence |
| `seq` | per session monotonically increasing integer, assigned by the single writer |
| `ts` | UTC ISO 8601 with offset |
| `severity` | the oplog set `DEBUG, INFO, WARNING, ERROR, CRITICAL` |
| `source` | `tui.host`, `tui.reducer`, `howlplane.pool`, `howlplane.orchestrator`, `howlframe.vm` |
| `code` | stable code (`OBJECTIVE_EMPTY`, `NO_ELIGIBLE_AI_RESOURCE`, `LIMIT_EXCEEDED`, ...) |
| `message` | redacted, at most 500 characters |
| `retry` | a `RetryClassification` value or `UNKNOWN` |
| `diagnostic` | sanitized object: what failed, why, next action, command |
| `refs` | run directory, evidence `entry_id`, error `diagnostic_id` |

### 4.2 Rules

* **No second pipeline.** Generalize `factory/oplog.OperationalLog` (it already
  has severity, codes, redaction, truncation and rotation) with the additive
  `schema`, `seq` and correlation fields, and point the TUI at its own state
  directory under `$XDG_STATE_HOME/howlplane/tui/`.
* **Operational log versus evidence.** The TUI writes operational events only.
  It never appends to the evidence ledger; evidence comes from the orchestrator
  as `ai.evidence_entry/v1`. The view labels a result as verified only from
  `work_result/v1` verification status and ledger entries for the task, and
  labels everything else as reported.
* **Actionable errors.** Every displayed failure carries what failed, why,
  retry safety and the next action (implemented by `explain_failure` in the
  probe). Retry safety is `UNKNOWN` unless a source states it.
* **Redaction.** One primitive, `redact_operator_text`. Library update
  candidate: `evidence_ledger.redact_sensitive_data` is a second, different
  primitive and should be consolidated.
* **Permissions.** Log directory 0700 and files 0600, using the existing
  `atomic_io.write_private_json` convention. Gap: `OperationalLog` and the
  evidence ledger (`evidence_ledger.py:274`, mode 0666 before umask) create files
  with umask dependent modes.
* **Rotation.** Reuse oplog rotation (10 MiB, keep 5) for operational events;
  evidence keeps its own gzip rotation.
* **Interrupted writes and malformed events.** One `write` per line on an
  append only file; readers skip an unterminated or unparseable line and report
  it, as `EvidenceLedger.read_entries` does with `LedgerDiagnostic`. The reducer
  rejects unknown step and document versions; the host keeps the previous state
  when a reducer step fails, so a step is applied entirely or not at all.
* **Termination and recovery.** The host restores the terminal on every exit
  path and signal. HowlFrame steps are short and their children are reaped on
  SIGINT, SIGTERM and SIGKILL (verified). An interrupted governed run is
  recovered by the existing `CrashRecoveryEngine` through `howlplane resume` or
  `cancel`; the TUI shows the stage's `RetryClassification` and never retries a
  `RECONCILE_FIRST` or `NEVER_AUTO_RETRY` stage itself.

## 5. Proposed modules and ownership boundaries

| Module | Language | Owns | Authority |
| --- | --- | --- | --- |
| `tui_core.howl` | HowlFrame | validation, inventory and route summaries, failure explanation, view lines | none |
| `tui_reducer.howl` | HowlFrame | `(state, event) -> (state, view, intents)` | none (no grant) |
| `control_plane/tui/host.py` | Python | terminal raw mode, key decoding, resize, painting through `presentation/style.py`, running reducer steps | the operator's, same as the CLI |
| `control_plane/tui/intents.py` | Python | closed allow-list mapping intents to existing APIs: `inventory_refresh`, `route_preview`, `run_objective` (explicit operator confirmation), `cancel` | delegates to HowlPlane; adds none |
| existing `ProviderPoolManager`, `GovernedTaskOrchestrator`, `EvidenceLedger`, `CrashRecoveryEngine`, `OperationalLog` | Python | provider configuration, routing, execution, evidence, recovery, logging | unchanged |
| HowlBoard | separate | rich graphical surface, dashboards, multiple sessions, Factory views | unchanged; the TUI is one objective at a time |

OOP is warranted only for the host (one class owning terminal state with a
guaranteed restore) and an intent dispatcher table; the reducer stays
functions over plain data.

## 6. HowlFrame gaps and narrowly scoped fixes

### 6.1 Defects (minimal reproducers in `integrations/howlframe/tui00/reproducers/`)

Each has a strict `xfail` test in `tests/test_tui00_feasibility.py` that asserts
the correct behavior and was observed failing on both `9c739c9` and `41d56ce`.

| ID | Defect | Reproducer | Narrow fix | Regression test proposal (HowlFrame `internal/vm`) |
| --- | --- | --- | --- | --- |
| HF-D1 | `spawn` children start with `executed = 0` and do not inherit `spawnDepth` or `callDepth`, so one run can execute far more than `--max-instructions`. The journal `docs/journals/2026-10-06_c4a_max_call_depth.md` says spawn work "remains bounded by the shared instruction budget"; live behavior contradicts it | `hf_d1_spawn_instruction_budget.howl`: five children finish 500 loop iterations under a 1500 budget; the sequential equivalent fails | share one atomic instruction counter across the run's VMs and pass `spawnDepth + 1` and `callDepth` to the child, as `SPAWN_AGENT` does | sum of child and parent work bounded by `MaxInstructions`; nested spawn depth bounded by `MaxCallDepth` |
| HF-D2 | a spawn body and its parent writing one captured dict crash the process with Go `fatal error: concurrent map writes` (5 of 5 runs), exit 2, no structured error, no receipt | `hf_d2_spawn_shared_map_race.howl` | deep copy captured mutable collections at `SPAWN` (or guard VM collections with a lock) | concurrent writes yield either a structured error or a consistent result, never a fatal runtime error |
| HF-D3 | `READ_LINE` buffers an entire line without charging the memory budget | `hf_d3_read_line_unbounded.howl`: a 32 MiB line accepted under 1 MiB | read through a reader bounded by the remaining budget and charge the bytes, as `READ_STDIN` does | a line larger than `--max-memory-bytes` fails with `LIMIT_EXCEEDED` at `READ_LINE` |
| HF-D4 | `PARSE_JSON` does not charge the decoded tree | `hf_d4_parse_json_uncharged.howl`: 4.5 MB input under an 8 MiB budget reached 225 MiB RSS | charge decoded nodes (for example a fixed cost per value plus string bytes) during decoding | a many small objects document exceeding the budget fails with `LIMIT_EXCEEDED` at `PARSE_JSON` |

### 6.2 Gaps (by design or missing; none blocks the recommended split)

| ID | Gap | Smallest safe improvement, if ever needed |
| --- | --- | --- |
| HF-G1 | no raw mode, key events or resize | host owned; a runtime change would add terminal authority to the VM and is not recommended |
| HF-G2 | exec is blocking with no streaming | not needed when the host owns execution |
| HF-G3 | exec merges stdout and stderr | optional separate `stderr` field on the exec result |
| HF-G4 | no per call exec timeout; `--deadline` is global and uncatchable | optional `timeout_ms` grant pattern on `process:exec=` |
| HF-G5 | no limit on concurrent exec children | a `--max-processes` runner limit |
| HF-G6 | runtime error JSON has no schema version | add `schema` to the error object |
| HF-G7 | instruction and memory budgets are cumulative per run, so a long lived interactive VM eventually fails | per step VMs, as recommended; no runtime change |
| HF-G8 | no `nil` literal; exec returns bytes that `print` renders as numbers | ergonomic, workarounds verified (`map_get` on an empty dict, `bytes_to_string`) |

### 6.3 HowlPlane findings

* HP-F1: run as a HowlFrame exec child, `howlplane providers --json` reported
  `codex` `MISSING_EXECUTABLE` while the same command from the operator shell
  reported `READY`. Children cannot receive `PATH` or `HOME` (refused by design),
  so readiness is wrong and providers could not authenticate. This is the
  decisive reason the governed run cannot be driven from inside the VM.
  Covered by `test_exec_child_cannot_observe_operator_path`.
* HP-F2: `howlplane route` selected `claude_code` (`--agent`) and `codex` (automatic)
  while neither executable existed (see HP-C3).
* HP-F3: `ai.run_event/v1` is documented as the run event contract but has
  no Python producer. Library update candidate for `TASK_EVENT_SPEC.md`.

## 7. Resource safe implementation plan for TUI-01

1. Record ADR 0009 (proposed in `documentation/adr/0009_interactive_terminal_host.md`)
   and obtain owner approval for the native host before writing it.
2. HowlFrame upstream: file HF-D1..HF-D4 with these reproducers. TUI-01 does not
   depend on the fixes because the reducer never uses `spawn` and the host caps
   input size, but the strict `xfail` tests will report when they land.
3. Add `src/howlplane/control_plane/tui/` with the host and intent table. Every
   reducer step runs `howlframe run` with no `--allow-caps`, `--max-instructions`,
   `--max-memory-bytes` and `--deadline` taken from configuration (defaults
   derived from the measured step cost with headroom, never unlimited), and a
   host enforced cap on step input bytes.
4. Ship the reducer bytecode the way `howlframe_runner.py` ships its artifacts:
   a committed `.hfbc` plus provenance hashes, verified against the pinned
   runtime.
5. Inventory and preview through the pool API in process with
   `probe_on_start=True` and `read_only=True`; execution through the same
   orchestrator path as `work --execute`, only after an explicit confirmation
   key; cancel through the existing cancel path.
6. Progress through `task_progress/v1` snapshots first; add HP-C2 only if the
   snapshot cadence proves insufficient.
7. Logging per section 4 by extending `OperationalLog`, not a new writer.
8. Tests: reducer replay tests from fixtures (unit tier, real HowlFrame
   required), host tests with a pseudo terminal and a fake backend that records
   invocations, a test that no provider executable is launched during preview,
   terminal restore after SIGINT and SIGTERM, and contract version refusal
   tests. Run serially; no live providers.
9. Out of scope: chat, multiple sessions, dashboards, Factory management.

## 8. Recommendation: pure HowlFrame versus a minimal native adapter

| Option | Pros | Cons |
| --- | --- | --- |
| A. Pure HowlFrame TUI calling the `howlplane` CLI through exec | maximal `.howl`; the boundary is governed by exact argv grants (verified) | no raw mode, keys or resize; HP-F1 makes discovery wrong and execution impossible without widening `process:env`, which would defeat the code loading protections; no live progress; merged streams; global deadline; cumulative budgets end long sessions; concurrency hits HF-D1 and HF-D2 |
| B. HowlFrame line mode REPL (no raw mode) | runs today in a pty | same HP-F1 blocker; no live progress |
| C. **HowlPlane native host plus zero grant HowlFrame reducer per event (recommended)** | HowlFrame owns validation, state, view and control decisions with no authority ("intent is not authority"); the host already holds the operator's environment and authority, so nothing is widened; reuses the pool and orchestrator in process with no new routing or configuration; bounded per event (5.2 ms median, 419 instructions); deterministic replay tests; avoids HF-D1..HF-D4 | a small amount of Python to maintain; one process per event; state must stay bounded because it is serialized each step |
| D. Go host embedding the VM | one binary | HowlFrame's VM is under `internal/` and cannot be imported; would need a new public HowlFrame API |
| E. Python only TUI | simplest | abandons the HowlFrame preference |

**Recommendation: option C.** A thin native adapter is required, and the
reason is authority, not convenience: the only safe way to give providers the
operator's environment is to not run them under the HowlFrame VM. The adapter's
proposed contract is the step protocol below plus the closed intent table in
section 5; it holds exactly the authority of an operator running `howlplane`
and grants the HowlFrame program none. It must not be implemented until ADR 0009
is accepted.

Step protocol (implemented by the probe, version 0 until ADR acceptance):

* stdin `howlplane.tui_step/v0`: `{schema, state, event, config}`; config supplies
  limits such as `max_objective_chars`, so the program hardcodes none.
* stdout `howlplane.tui_step_result/v0`: `{schema, ok, state, view, intents}`;
  failure `{schema, ok: false, code, diagnostic}` with exit 2.
* Events: `objective_submitted`, `provider_selected`, `inventory`,
  `route_preview`, `failure`, `work_result`; unknown types are reported, not fatal.

## 9. Evidence: mandatory feasibility proofs

| # | Proof | Test (`tests/test_tui00_feasibility.py`) | Result |
| --- | --- | --- | --- |
| 1 | dynamic prompt | `test_dynamic_objective_is_trimmed_and_proposed_as_route_intent`, `test_invalid_objective_fails_before_any_intent`, `test_malformed_step_is_a_structured_refusal` | pass |
| 2 | parse existing inventory | `test_reads_real_inventory_contract`, `test_unknown_inventory_version_fails_closed` | pass |
| 3 | provider becomes unavailable | `test_provider_loss_clears_explicit_choice`, `test_selecting_an_unusable_provider_is_refused`, `test_boundary_blocked_exit_code_keeps_document` | pass |
| 4 | routing preview without AI | `test_route_preview_flags_selection_unconfirmed_by_inventory`, `test_blocked_route_lists_exclusions`; in process probe in section 2.2 | pass |
| 5 | structured failure | `test_howlplane_error_is_rendered_without_guessing_retry_safety`, `test_unrecognized_failure_document_is_explicit`, `test_work_result_reports_verification_status_verbatim` | pass |
| 6 | fake execution boundary | `test_boundary_success_and_verbatim_argv`, `test_boundary_howlplane_error_on_stderr`, `test_boundary_unparseable_output_is_never_success`, `test_ungranted_argv_is_denied_before_the_boundary_runs` | pass |
| 7 | bounded resources | `test_instruction_budget_fails_closed`, `test_hung_boundary_is_killed_at_deadline`, `test_output_flood_is_bounded`; HF-D1..HF-D4 strict `xfail` | pass, 4 xfailed as expected |
| | real HowlPlane through exec | `test_exec_child_cannot_observe_operator_path` | pass (documents HP-F1) |

Command: `HOWLFRAME_BIN=<howlframe> pytest tests/test_tui00_feasibility.py -p no:randomly`.
Result on both runtimes: 25 passed, 4 xfailed.

## Test Impact Assessment

* Behavior changed: none in HowlPlane runtime code. Added probes, fixtures, one
  test module, one proposed ADR and documentation.
* Existing tests proving it: not applicable; the new module is the only coverage.
* Missing coverage acknowledged: call depth, `doctor --json`, real governed
  execution and HowlBoard interplay were not exercised (UNKNOWN above).
* Tier: integration (real HowlFrame binary, real HowlPlane CLI for HP-F1).
* Obsolete or duplicate contracts: none removed. Duplicate redaction primitive
  flagged, not changed.
* Tests run: listed in the final handoff of this task.
