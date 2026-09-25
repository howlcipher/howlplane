# Workspace trust

Some agent CLIs refuse to work in a directory until someone trusts it. The refusal protects the operator: a repository can carry hooks, MCP servers, or instructions that the CLI would otherwise run. An unattended Factory session has no one to answer that prompt, so HowlPlane treats trust as a first-class, per-directory fact. It never answers a trust prompt itself.

## Workspace trust policy

One setting decides what an unattended invocation does about vendor workspace trust. Doctor, `orchestrate`, Factory, the provider pool, and agent execution all read it from one resolver (`workspace_trust.resolve_policy`), and the backend applies it when it builds the CLI argv.

| Policy | Meaning | Untrusted Cursor or Devin workspace |
| --- | --- | --- |
| `strict` | Respect vendor workspace trust. Nothing is ever bypassed. | Ineligible until the vendor itself trusts the directory. |
| `prepare` (built-in default) | Use vendor trust prepared ahead of time with `howlplane factory prepare`. This is the PR #116 behavior. | Ineligible until prepared. Cursor preparation is automatic; Devin needs the operator at a terminal once. |
| `bypass` | Pass each CLI's documented invocation option, so workspace trust never interrupts unattended work. | Eligible. No preparation is needed, even for a worktree no vendor has ever seen. |

Configure it once in `~/.config/howlplane/config.toml` (or the file named by `HOWLPLANE_LOCAL_CONFIG`):

```toml
[workspace_trust]
policy = "bypass"
```

Override it for one command with `--workspace-trust {strict,prepare,bypass}`. The option is accepted by `howlplane orchestrate` (new sessions and `resume`), `agents doctor`, and `factory run`, `run-once`, `start`, `canary`, `doctor`, and `prepare`. The Howl wrapper forwards it unchanged, for example `howl orchestrate "goal" --workspace-trust strict`.

Precedence, highest first:

1. the `--workspace-trust` option;
2. `HOWLPLANE_WORKSPACE_TRUST` in the environment (this is how an explicit option reaches the sessions and agents HowlPlane launches; the Factory user service receives it in argv);
3. `[workspace_trust] policy` in HowlPlane configuration;
4. the built-in default, `prepare`.

There is no repository-level or Factory-level trust setting, so there is nothing else to disagree with. An invalid value is an error, never a fallback. An orchestration session records the policy it resolved and re-resolves it on `resume`, so an operator can switch a session between `strict` and `bypass`.

### What `bypass` does per CLI

Each backend adapter in `workspace_trust.ADAPTERS` declares an audited mechanism. `bypass` uses only these mechanisms, and a CLI without one is never bypassed:

| Agent | Declared behavior | Under `bypass` | Vendor trust afterwards |
| --- | --- | --- | --- |
| Codex, Claude Code, AGY | Trust not enforced in noninteractive mode | Nothing is added | Reported as `NOT_ENFORCED` |
| Cursor | Supported persistent trust flag | `--trust` on the real assignment (not a separate probe) | Cursor records its own trust marker, so the vendor state can truthfully become `READY` |
| Devin | Supported invocation bypass | `--respect-workspace-trust false` as two discrete argv values | **Unchanged.** The flag skips Devin's check for that one invocation. It does not trust the folder, and nothing is added to Devin's trusted list |
| Any other CLI | Unsupported | Nothing is added | A trust refusal fails closed as `WORKSPACE_TRUST_REQUIRED` |

Cursor's `--trust` is separate from `--force`/`--yolo`, which approve commands and are never passed. Claude Code's workspace trust is unrelated to its tool permissions: the per-invocation permission profile is unchanged under every trust policy.

### Vendor state, policy, and effective readiness

Doctor and Factory report three separate values, and a bypass never rewrites the vendor's verdict:

```text
Devin
  Vendor trust:        TRUST REQUIRED — not inside any Devin-trusted workspace
  Trust policy:        BYPASS
  Effective workspace: READY — vendor trust not required for this invocation (check skipped, directory not trusted)
  Mechanism:           --respect-workspace-trust false
```

JSON carries the same values as `vendor_state`, `policy`, `effective_state`, and `mechanism` on each agent under `workspace.agents`. The report also carries the resolved `workspace_trust_policy` (`policy` and `source`). Routing, Factory preflight, and orchestration decide on `effective_state`. Under `bypass`, a worktree that no vendor has ever seen is `READY` for every otherwise-ready agent, and Factory is not `DEGRADED` because of vendor trust alone.

A trust refusal is recorded with the policy and mechanism of the invocation that met it. A refusal met without the bypass flag does not bind a later invocation that carries the flag. A refusal met with the flag stays binding under every policy. A refusal is never agent-wide and never capacity evidence.

### Security boundary

Vendor workspace trust exists because repository content can steer an agent: rule and instruction files, hooks, MCP server definitions, and project settings. Under `bypass`, Cursor and Devin no longer ask a human before reading that content in a new directory. That friction is what the operator removes. Choose `strict` when a repository's content is not trusted, for example a fork or a third-party checkout.

`bypass` changes only the vendor's folder trust check. It does **not** bypass:

* repository allowlists, HowlPlane authorization records, or Factory scope;
* HowlFrame policies, risk gates, and authority profiles;
* merge policy, production and change approvals, or deployment permission;
* secret handling and destructive-operation protection;
* each CLI's own command and tool permissions (Claude Code's bounded profile, Devin's `--permission-mode`, Cursor's plan mode for read-only roles). Nothing adds `--force`, `--yolo`, or `bypassPermissions`;
* closed stdin. No prompt is ever answered, typed, or piped, and no terminal is simulated.

HowlPlane never writes a vendor trust store under any policy.

## Global readiness and workspace readiness

These answer different questions:

* **Global readiness** (`howlplane agents doctor`) asks whether the CLI is installed, logged in, able to run unattended, and has capacity. It is a fact about the agent.
* **Workspace readiness** (`howlplane agents doctor --repo PATH`) asks whether this CLI will run in this directory without a prompt. It is a fact about one directory.

An untrusted folder never disables an agent globally. The agent stays eligible everywhere else, and routing skips it only for the affected workspace.

```bash
howlplane agents doctor --repo /path/to/repo          # trust from the vendors' own files, no prompt, no network
howlplane agents doctor --repo /path/to/repo --live   # also a read-only smoke in that directory
howlplane factory doctor                              # Factory readiness for the campaign's own worktree
```

Workspace states are `READY`, `TRUST_REQUIRED`, `UNKNOWN`, `UNSUPPORTED`, and `ERROR`. Checking trust never sends a model prompt.

## Observed trust behavior

Observed on the installed CLIs on 2026-09-25:

| Agent | Noninteractive mode prompts for trust? | Vendor trust store | Scope | Preparation |
| --- | --- | --- | --- | --- |
| Codex 0.156.1 | No. `codex exec` ran in an untrusted directory. | `~/.codex/config.toml` `trust_level` (gates project config only) | not enforced | none needed |
| Claude Code 2.1.282 | No. `-p` skips the trust dialog (`claude --help`). | `~/.claude.json` | not enforced | none needed |
| AGY 1.2.10 | No. `agy -p` ran in an untrusted directory. | `~/.gemini/antigravity-cli/settings.json` | not enforced | none needed |
| Cursor 2026.09.23 | Yes. `-p` exits with "Workspace Trust Required ... Pass --trust". | `~/.cursor/projects/<slug>/.workspace-trusted` (or under `$CURSOR_DATA_DIR`) | The directory and its descendants. A marker on `$HOME`, anything above `$HOME`, or a path with fewer than three components is not inherited. | Automatic: the documented `--trust` flag |
| Devin 3000.11.3 | Yes. `-p` exits with "Refusing to run in an untrusted workspace". | `~/.local/share/devin/cli/trusted_workspaces.json` | The directory and its descendants (canonical paths) | One-time: the operator answers Devin's own prompt |

Devin also offers `--respect-workspace-trust false`, which skips the check instead of establishing trust. HowlPlane passes it only under the `bypass` policy (see above).

## Authorizing a repository

```bash
howlplane factory prepare --repo /path/to/repo
```

Running this command is the operator's authorization, and it is the only way HowlPlane prepares trust. Under `bypass` it still records the authorization and creates the worktree, but it runs no vendor CLI: Cursor and Devin need no preparation there. Otherwise it works in this order:

1. **Show the scope.** It prints exactly what will be authorized: the repository checkout, the repository's Factory workspace root (`$XDG_DATA_HOME/howlplane/worktrees/<campaign-id>/`), and the Factory worktree. For each agent it also prints whether trust covers new worktrees under the root.
2. **Confirm.** It asks for confirmation, typed `yes` at a terminal, or `--yes`. Without it, nothing is changed and the command exits with status 2.
3. **Record the authorization.** It writes HowlPlane's own authorization record.
4. **Create the worktree.** It creates or validates the stable Factory worktree.
5. **Prepare trust.** It prepares vendor trust for the authorized directories only:
   * **Cursor:** `agent -p noop --trust --workspace <dir> --model <sentinel>`. The documented flag writes Cursor's trust marker, and the deliberately invalid model makes Cursor stop before any inference.
   * **Devin:** Devin is started in the directory on the operator's terminal, so its own trust prompt appears. HowlPlane sends no input. It watches Devin's trust store, then ends the session once trust is recorded. Without a terminal this step is skipped and reported as `TRUST_REQUIRED`.
6. **Re-verify.** It re-verifies trust by asking the CLI itself (the same sentinel-model check, again without inference).
7. **Report.** It prints workspace and Factory readiness. `--live` adds a read-only smoke in the Factory worktree.

`--agent` limits preparation to specific agents. In `local_only` mode no vendor is contacted, and preparation is reported as not attempted.

## Factory workspace strategy

Every Factory worktree for a repository lives under one stable root:

```
$XDG_DATA_HOME/howlplane/worktrees/<campaign-id>/target                 # the continuous campaign
$XDG_DATA_HOME/howlplane/worktrees/<campaign-id>/canaries/<id>/target   # bounded canary runs
```

Both CLIs that enforce trust inherit it from a trusted ancestor. Trusting the root once therefore covers the stable worktree and every future canary without further preparation. Each worktree is still a separate `git worktree`, so Git isolation is unchanged. A stable pool of pre-trusted slots is not needed, because no supported CLI in use trusts only exact paths. A custom `--target-repo` outside the root is authorized and prepared as an exact path.

## HowlPlane authorization is not vendor trust

The authorization record lives at `$XDG_STATE_HOME/howlplane/workspace_authorizations.json`, mode 0600 in a 0700 directory. `HOWLPLANE_WORKSPACE_AUTHORIZATION_FILE` overrides the path. It stores:

* the repository root and Git common directory
* the Factory root and the authorized workspaces
* when and how the scope was authorized
* the workspace strategy
* per-agent preparation results and when they were last verified

It holds no secrets, output, or transcripts.

The record means only "HowlPlane may prepare and use this scope". Covered paths are the authorized checkout, the listed workspaces, and worktrees of the same repository inside the Factory root (checked by Git common directory). An arbitrary directory is never covered. Vendor trust is always verified separately.

To revoke:

```bash
howlplane factory prepare --repo /path/to/repo --revoke
```

This removes only HowlPlane's record. Vendor trust is left where each vendor keeps it, and the command prints those locations. HowlPlane never edits a vendor trust store.

## When a trust prompt appears anyway

Agent CLIs always run with stdin closed, so a prompt can never wait for terminal input. If a CLI refuses a workspace during unattended work:

* The attempt is classified `WORKSPACE_TRUST_REQUIRED`, never `ENGINEERING_FAILURE`, permission failure, or capacity. The refusal must be the CLI's own, positively identified, at the start of its output. Trust prose later in a transcript does not count.
* The session is checkpointed, partial changes are kept, and the agent, workspace, role, source, and time are recorded.
* The agent is excluded for that workspace only and the task reroutes to another eligible agent. It is not retried in the same workspace, and it stays eligible elsewhere.
* A refusal that was observed outranks the vendor store until `factory prepare` re-verifies the workspace or a workspace smoke passes.

A session stopped at our own execution budget is still `EXECUTION_BUDGET_EXCEEDED` unless its output opens with a trust refusal.

## Factory preflight

`factory run` and `factory start` with `--preflight warn|require`, as well as `factory doctor`, check trust for the actual Factory worktree before any worker is dispatched. Agents that would meet a trust prompt there are not counted as workers:

* If some agents are trust-gated but others can implement and review, the result is `DEGRADED`, and the gated agents are listed.
* If every implementation-capable agent is trust-gated, the result is `BLOCKED`, and `--preflight require` refuses to start.
