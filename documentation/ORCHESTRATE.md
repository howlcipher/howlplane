# Session orchestration

`howlplane orchestrate "Goal"` starts a session in the current Git worktree. `howl orchestrate` forwards to the same command. With an interactive terminal and no goal, the command asks eight questions. With a goal, defaults are Auto orchestrator, installed agents on AUTO, BALANCED routing, AUTO models, AUTO REROUTE, and PLAN + EXECUTE + INDEPENDENT AUDIT.

```bash
howlplane orchestrate "Update the parser" --repo /path/to/repo --orchestrator codex --claude-code RESERVED --strategy BALANCED --verify go test ./...
howlplane orchestrate "Update the parser" --heartbeat 25
howlplane orchestrate inspect --repo /path/to/repo --json
howlplane orchestrate resume --repo /path/to/repo
howlplane orchestrate resume --repo /path/to/repo --execution-budget implementation=600
howlplane orchestrate discard --repo /path/to/repo
```

The installed CLI binaries determine agent readiness. Cursor uses the Cursor Agent CLI (`agent -p "<prompt>" --output-format text --workspace <repo>`). AUTO pins a listed model only for AGY (`agy models`) and Devin (`devin models list`). Codex, Claude, and Cursor run on their configured CLI default with model identity `UNKNOWN` unless a named model is supplied and confirmed by a prior successful invocation. `howlplane agents doctor` reports every CLI's advertised models without pinning them (see [AGENT_DOCTOR.md](AGENT_DOCTOR.md)). A named override has the form `--models implementation:cursor:model-id`. Ordered alternatives use `--fallbacks implementation:cursor:model-id,implementation:agy:model-id`. A RESERVED agent is never selected. Model exhaustion and agent unavailability are separate states. The session resolves the workspace trust policy (`--workspace-trust`, then `[workspace_trust] policy`, default `prepare`) and records it; under `bypass`, Cursor and Devin run in a never-seen worktree through their audited flags. An agent whose CLI would still meet a workspace trust prompt in the session's repository is skipped for that repository only, and a trust refusal during the session (`WORKSPACE_TRUST_REQUIRED`) checkpoints, reroutes, and bars that agent from the same workspace without marking it interactive-only. In `local_only` mode no hosted agent is dispatched. See [WORKSPACE_TRUST.md](WORKSPACE_TRUST.md).

AUTO routing also uses session-scoped capability evidence. An `EXECUTION_PERMISSION_REQUIRED` result marks only that backend's unattended-execution capability as unavailable for the current session, leaves unrelated capabilities unchanged, and excludes the backend from later unattended AUTO assignments. A new session starts with fresh capability evidence. Explicit orchestrator selection may override this AUTO exclusion with a terminal warning; RESERVED still takes precedence. Readiness checks use local CLI metadata and observed invocations rather than speculative model prompts. A session starts from the cached `agents doctor` evidence, if any, and never probes for it. Fresh evidence seeds unattended capability. A fresh "not logged in" result or an unexpired agent-wide quota or session limit makes the agent UNAVAILABLE. An unexpired model-scoped limit marks only that model EXHAUSTED. UNKNOWN never removes an agent. Real session outcomes are written back to that cache at the scope they prove, so a later session knows about them. Each assignment prints and records its observable selection evidence, for example `SELECT AGY for implementation: unattended verified · live smoke passed 8m ago · capacity UNKNOWN`.

Hard failures are recorded per agent and role before a replacement is chosen. `SESSION_LIMIT` marks that agent's capacity for the role `EXHAUSTED`; deterministic failures such as `ENGINEERING_FAILURE`, `NO_REPOSITORY_CHANGE`, or `EXECUTION_PERMISSION_REQUIRED` mark it `FAILED`. Either excludes the agent from that role for the rest of the session with every model, including configured fallbacks and an explicitly selected orchestrator, while leaving it eligible for other roles. Quota and rate limits still exhaust only the model that hit them, and a transient transport failure still gets one bounded retry. Candidates are recomputed after every failure, so the `REROUTE` target always names the next `ASSIGN`. When no eligible worker remains, the session checkpoints as `HANDOFF REQUIRED` (or `AUDIT BLOCKED` for review) and lists each excluded agent with its reason instead of cycling.

### Execution budget is not provider capacity

Every assignment runs under HowlPlane's own execution budget. That is a per-role wall-clock deadline: 300 seconds by default for planning, review, and acceptance, and 600 seconds by default for implementation (justified by dogfooding across multi-file implementation tasks), with a hard maximum of 1800. AGY's `--print-timeout` is derived from the same deadline minus 15 seconds of headroom. Set the budget with `--execution-budget ROLE=SECONDS` or `--execution-budget SECONDS` (all roles), on a new session or on `resume`. Values outside 1 to 1800 are refused, never clamped. Raising every budget is not the default fix: prefer decomposing a goal that does not fit.

An `EXECUTION_BUDGET_EXCEEDED` result means only that this assignment did not finish in time. It is not quota, session, rate, or model exhaustion:

| Evidence | Recorded as | Scope |
| --- | --- | --- |
| Assignment hit the execution budget (harness kill or AGY's budget-derived print timeout) | Attempt `TIMED_OUT`, with `timeout_source`, `budget_derived`, and `partial_changes` | That exact assignment only |
| `QUOTA_EXHAUSTED`, `RATE_LIMITED` | Model `EXHAUSTED` | The model that hit it; the agent's other models stay eligible |
| `SESSION_LIMIT` | Model `EXHAUSTED` and role capacity `EXHAUSTED` | That agent for that role this session |
| Any limit with an `UNKNOWN` model | Agent `UNAVAILABLE` | The agent this session |
| `MISSING_EXECUTABLE`, `AUTHENTICATION_REQUIRED`, `PROVIDER_UNAVAILABLE` | Agent `UNAVAILABLE` | The agent this session |
| `EXECUTION_PERMISSION_REQUIRED` | Unattended execution unavailable (interactive-only) | The agent this session |

After a timeout, the repository is checkpointed. Partial changes are recorded, and the next worker is told to inspect and continue them. Another worker is preferred for the same role. The timed-out agent stays eligible for other roles and other models, just behind other workers for this role. The same goal, constraints, role, agent, model, and budget never run again unchanged, including after `resume`. A retry needs a meaningful change: a different agent or model, a changed goal or constraints, a changed budget (`resume --execution-budget implementation=600`), or an explicit operator retry (`resume --retry-timeouts`, which clears the timeout ledger once and records that it did). When a stage runs out of workers after timeouts, the handoff and report include timeout guidance instead of claiming anything was exhausted.

### Session lifecycle and resumability

Orchestration sessions classify states into distinct lifecycle categories:

- **Successfully terminal:** `COMPLETE`, `COMPLETE WITH WARNINGS`. These represent finished sessions and cannot resume.
- **Paused / resumable:** `HANDOFF REQUIRED`, `INTERRUPTED`, and recoverable `BLOCKED` states (such as exhausted independent reviewers, worker timeouts, rate limits, or pending workspace trust repair). `HANDOFF REQUIRED` signifies that autonomous progress stopped safely because external intervention, repository repairs, or changed execution parameters are required. It does NOT mean the session record is dead.
- **Superseded:** `SUPERSEDED`. `orchestration.supersede` retires one resumable session in favor of replacement work, for example when a Factory queue task is edited into a new revision (see `FACTORY_QUEUE.md`). Unlike `discard`, it keeps the manifest and records the reason, the replacement, and the previous status. The session is no longer resumable and no longer owns its worktree. A session with a live coordinator lease is refused.
- **Truly terminal failures:** `SESSION_STATE_INVALID`, operator discard, or unrecoverable `BLOCKED` states (such as a read-only review or acceptance role mutating the repository). These fail closed and cannot resume.

Resuming a paused or handoff session re-acquires a coordinator lease, reconciles repository state against checkpoint evidence, applies updated execution parameters, and continues without restarting planning unless reconciliation demonstrates planning is invalidated:

```bash
howlplane orchestrate resume --repo /path/to/repo
howlplane orchestrate resume --repo /path/to/repo --execution-budget implementation=600
```

#### External repair and reconciliation

When a previous session paused at `HANDOFF REQUIRED` due to verification failures, a developer or external process may fix the repository directly. On resume:

1. HowlPlane detects the external changes since the checkpoint.
2. If the repository now passes configured deterministic verification, the session does not force an unneeded implementation worker to produce a fake change.
3. The session advances directly to independent audit, orchestrator acceptance, and completion.

#### Existing-WIP mode and NO_CHANGE_REQUIRED

When an orchestration goal explicitly targets existing uncommitted changes (such as `"Finalize and validate existing uncommitted work"`), an implementation worker that inspects the repository and verifies the existing code is correct may report `IMPLEMENTATION_STATUS: NO_CHANGE_REQUIRED`.

When backed by objective evidence (uncommitted repository changes exist and deterministic validation passes), HowlPlane records the outcome as `NO_CHANGE_REQUIRED` and advances to independent audit rather than cycling through other providers with `NO_REPOSITORY_CHANGE` failures. Ordinary requests on clean repositories fail closed.

#### Inspection and guidance

Both `howlplane orchestrate report` and `howlplane orchestrate inspect` explicitly report `Resumable: yes` or `Resumable: no`. Machine-readable `inspect --json` provides `"resumable": true` or `false` on each session object. CLI progress output never displays `RESUME howlplane orchestrate resume ...` unless the session is in a verified resumable state.

Session manifests live in `$XDG_STATE_HOME/howlplane/orchestrate` (or `~/.local/state/howlplane/orchestrate`). Files are mode 0600 in a mode 0700 directory and use atomic replacement. The manifest records a lease fence, assignments, failures, model states, capability and capacity evidence, Git evidence hashes, and validation results. It carries a `schema_version`. On resume, the manifest is normalized before use: a version 1 manifest (created before capability evidence existed) gains UNKNOWN capabilities and replays its recorded hard failures, never positive availability, and an agent it never recorded is UNAVAILABLE. A version 2 manifest recorded execution-budget stops as role capacity `EXHAUSTED`. Version 3 converts those into timed-out assignments at the 300-second budget every earlier session used. It then restores an agent that nothing else had degraded, and keeps all other capacity and capability evidence. A manifest that cannot be normalized without guessing is left untouched and reported as `SESSION_STATE_INVALID`. On resume, Git evidence is read again and overrides stale manifest claims. The same worktree cannot have overlapping active sessions; use a distinct Git worktree for another session. `inspect --json` includes retained terminal sessions.

The coordinator uses the existing HowlPlane execution backends and failure classifier. It asks independent reviewers to inspect without editing, checks `git diff --check`, and runs an explicit `--verify` command when supplied. The report records the verification actually run. If every worker fails, it returns `HANDOFF REQUIRED`; if independent review cannot finish, it returns `BLOCKED` with `AUDIT BLOCKED` evidence. Complete sessions remove runtime state unless `--retain-report` is requested.

During orchestration, HowlPlane prints a session summary, task-level phase and worker events, validation boundaries, failover decisions, and completion state to stderr. During otherwise silent work, the local supervisor prints a deterministic heartbeat every 30 seconds using known session state. Heartbeats never invoke an agent or model and consume no model quota. After five minutes without a worker state change, the output warns that the active session may be stalled without killing it.

Use `--heartbeat SECONDS` to change the quiet interval. Use `--no-progress` to suppress the session progress stream, or `--quiet` to print only the final report and errors. `inspect --json` remains machine-readable on stdout and does not include human progress chatter. Progress is line-based and has the same information in TTYs, redirected output, and ANSI-disabled terminals; color is not required. On Ctrl-C, the coordinator acknowledges the interruption, checkpoints state, and prints the supported resume command before returning status 130.
Recovered or partially changed work requires an explicit `--verify` command before the session can complete. A successful implementation also needs a clean independent audit and acceptance from the session orchestrator under the default policy.

## Agent invocation instructions

The same command can be invoked from Claude Code, Codex, or Cursor. In each agent, ask it to run `howlplane orchestrate "<goal>"` from the target Git repository, or use `howl orchestrate "<goal>"` when Howl is installed. Codex supports explicit `$skill-name` invocation and Cursor supports `/skill-name` for installed skills; these are agent interface conveniences, not separate orchestration cores. AGY and Devin can run the command through their supported CLI execution facilities. No slash command is required for them.
