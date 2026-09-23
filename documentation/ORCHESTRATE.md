# Session orchestration

`howlplane orchestrate "Goal"` starts a session in the current Git worktree. `howl orchestrate` forwards to the same command. With an interactive terminal and no goal, the command asks eight questions. With a goal, defaults are Auto orchestrator, installed agents on AUTO, BALANCED routing, AUTO models, AUTO REROUTE, and PLAN + EXECUTE + INDEPENDENT AUDIT.

```bash
howlplane orchestrate "Update the parser" --repo /path/to/repo --orchestrator codex --claude-code RESERVED --strategy BALANCED --verify go test ./...
howlplane orchestrate inspect --repo /path/to/repo --json
howlplane orchestrate resume --repo /path/to/repo
howlplane orchestrate discard --repo /path/to/repo
```

The installed CLI binaries determine agent readiness. Cursor uses `cursor-agent --print` and `cursor-agent --list-models`; AGY uses `agy models`; Devin uses `devin models list`. Codex and Claude use their configured CLI default with model identity `UNKNOWN` unless a named model is supplied and confirmed by a prior successful invocation. A named override has the form `--models implementation:cursor:model-id`. Ordered alternatives use `--fallbacks implementation:cursor:model-id,implementation:agy:model-id`. A RESERVED agent is never selected. Model exhaustion and agent unavailability are separate states.

Session manifests live in `$XDG_STATE_HOME/howlplane/orchestrate` (or `~/.local/state/howlplane/orchestrate`). Files are mode 0600 in a mode 0700 directory and use atomic replacement. The manifest records a lease fence, assignments, failures, model states, Git evidence hashes, and validation results. On resume, Git evidence is read again and overrides stale manifest claims. The same worktree cannot have overlapping active sessions; use a distinct Git worktree for another session. `inspect --json` includes retained terminal sessions.

The coordinator uses the existing HowlPlane execution backends and failure classifier. It asks independent reviewers to inspect without editing, checks `git diff --check`, and runs an explicit `--verify` command when supplied. The report records the verification actually run. If every worker fails, it returns `HANDOFF REQUIRED`; if independent review cannot finish, it returns `BLOCKED` with `AUDIT BLOCKED` evidence. Complete sessions remove runtime state unless `--retain-report` is requested.
Recovered or partially changed work requires an explicit `--verify` command before the session can complete. A successful implementation also needs a clean independent audit and acceptance from the session orchestrator under the default policy.

## Agent invocation instructions

The same command can be invoked from Claude Code, Codex, or Cursor. In each agent, ask it to run `howlplane orchestrate "<goal>"` from the target Git repository, or use `howl orchestrate "<goal>"` when Howl is installed. Codex supports explicit `$skill-name` invocation and Cursor supports `/skill-name` for installed skills; these are agent interface conveniences, not separate orchestration cores. AGY and Devin can run the command through their supported CLI execution facilities. No slash command is required for them.
