# Agent doctor

`howlplane agents doctor` reports, for every supported agent CLI, what HowlPlane can verify about it. It keeps two questions apart:

* **Capability:** is the CLI installed and logged in, and can HowlPlane drive it unattended?
* **Capacity:** does the account have allowance left, where the CLI actually says so?

A CLI can be fully capable with unknown capacity, or have allowance left but be interactive-only.

```bash
howlplane agents doctor                  # level 1 only: free, no prompt is sent
howlplane agents doctor --json           # machine-readable, schema howlplane.agent_readiness/v1
howlplane agents doctor --live           # also one tiny smoke prompt per eligible agent
howlplane agents doctor --agent agy --refresh
howlplane agents doctor --repo /path/to/repo [--live]   # adds WORKSPACE READINESS for that directory
howlplane factory doctor [--live] [--json]
howlplane factory prepare --repo /path/to/repo           # authorize and prepare workspace trust
```

Global readiness (below) is about the agent. Workspace readiness is about one directory: whether the CLI will run there without a trust prompt. See [WORKSPACE_TRUST.md](WORKSPACE_TRUST.md).

## Evidence levels

| Level | What runs | Cost |
| --- | --- | --- |
| 1: local | Executable lookup, version, auth status, advertised model list | None. No prompt is sent. |
| 2: live smoke (`--live`) | One `Respond exactly: HOWL_READY` prompt per agent that is installed and not known to be logged out. It goes through HowlPlane's own backend invocation in the read-only review role, in an empty temporary directory (never a repository), with a 60-second limit (`--smoke-timeout`). | One very small provider call per agent |
| 3: capacity | Structured usage fields a CLI emits (such as a `rate_limits` event), and quota, session, or rate limits proven by a smoke or a real orchestration outcome | Nothing extra |

Level 1 commands, as observed on the installed CLIs:

| Agent | Binary | Auth | Models | AUTO pins a listed model |
| --- | --- | --- | --- | --- |
| Codex | `codex` | `codex login status` | `codex debug models`; default from `$CODEX_HOME/config.toml` | No: CLI default |
| Claude Code | `claude` | `claude auth status` (JSON) | Not exposed | No: CLI default |
| Cursor | `agent` (the Cursor Agent CLI; `cursor` is only an IDE shim and is never probed) | `agent status` | `agent --list-models` | No: `auto` |
| AGY | `agy` | No status command. A fetched `agy models` list is taken as an active login. | `agy models` | Yes |
| Devin | `devin` | `devin auth status` | `devin models list` | Yes |

A smoke that goes through HowlPlane's real invocation shows more than "the CLI works in a terminal". It shows the CLI completes a non-interactive request with HowlPlane's own permission profile. `Edits allowed` is reported separately, from the backend's local profile, because a text-only smoke does not exercise file edits.

In `local_only` operating mode the doctor makes no hosted request. Only executable lookup and `--version` run. Auth and models are reported as not probed, and `--live` is refused with a notice.

## States

Each dimension is reported independently. Nothing is collapsed into one AVAILABLE flag:

* **CLI:** `AVAILABLE` or `NOT INSTALLED`.
* **Auth:** `READY`, `NOT LOGGED IN`, or `UNKNOWN`.
* **Unattended:** `YES`, `INTERACTIVE_ONLY` (a smoke or session hit a permission prompt), or `UNVERIFIED`.
* **Live smoke:** `PASS`, `FAIL`, `BLOCKED_PERMISSION`, `WORKSPACE_TRUST_REQUIRED`, `TIMED_OUT`, `STALE`, or `NOT_RUN`.

`WORKSPACE_TRUST_REQUIRED` is scoped to a directory and is its own failure class. Cursor's `agent` and Devin refuse any directory that has not been trusted, and the smoke's fresh temporary directory is always one. The refusal therefore leaves Unattended as `UNVERIFIED` rather than `INTERACTIVE_ONLY`, and it is never written back as an agent-wide capability, whether it came from a smoke or from orchestration. Use `--repo` to check a real workspace, and `howlplane factory prepare` to authorize and prepare one ([WORKSPACE_TRUST.md](WORKSPACE_TRUST.md)).
* **Workspace** (with `--repo`): `READY`, `TRUST REQUIRED`, `UNKNOWN`, `UNSUPPORTED`, or `ERROR`, read from each vendor's own trust store without any prompt.
* **Capacity:** provider-pool states (`AVAILABLE`, `RATE_LIMITED`, `SESSION_EXHAUSTED`, `QUOTA_EXHAUSTED`, `UNKNOWN`), with a `scope` of `model` or `agent`, a `source`, and an expiry.

`UNKNOWN` is a real answer, not a failure. If a CLI does not expose remaining allowance, the doctor prints `Capacity: UNKNOWN — CLI does not expose remaining allowance`. Percentages such as `five_hour_remaining_percent` and `weekly_remaining_percent` are filled in only from structured CLI output, never estimated from elapsed time or inferred from a failure. None of the currently installed CLIs exposes its allowance through a free command.

A smoke that exceeds its own limit is `TIMED_OUT` and changes no capacity. For the same reason, an orchestration `EXECUTION_BUDGET_EXCEEDED` is never recorded here (see [ORCHESTRATE.md](ORCHESTRATE.md#execution-budget-is-not-provider-capacity)).

## What the doctor cannot know

* Remaining allowance for any CLI that does not print it. It stays UNKNOWN.
* Whether a model that works for a tiny prompt will finish a large task within its execution budget.
* Whether a limit reported as agent-wide actually covers the whole provider account. A limit with no model identity is recorded at `agent` scope, never wider.

## Cache

Evidence is stored in `$XDG_STATE_HOME/howlplane/agent_readiness.json` (or `~/.local/state/howlplane/agent_readiness.json`). The file is mode 0600 and written atomically, and `HOWLPLANE_AGENT_READINESS_FILE` overrides the path. Auth output is parsed into fields and never stored raw. Emails, names, organization IDs, and tokens are redacted.

| Evidence | Lifetime |
| --- | --- |
| Version, models, installation | 24h, and re-probed at once if the executable on `PATH` changes |
| Auth | 1h |
| Unattended verdict | 24h |
| Live smoke | 30 minutes (then `STALE`) |
| Structured capacity | 10 minutes |
| Quota, session, and rate limits | Until the reported reset, else the provider pool's cooldown (quota 6h, session 4h, rate 5m) |

A fresh `PASS` satisfies `--live` without another call. Use `--refresh` to probe again. Stale evidence reverts to UNKNOWN, so one old failure never blacklists an agent permanently.

## How routing uses it

`howlplane orchestrate` reads the cache when a session starts. It never runs a probe or a smoke per assignment. Before unattended work is assigned, an agent needs to be installed, not known to be logged out, not known to be interactive-only, not under an unexpired agent-wide limit, and trusted in the session's workspace. The Factory provider pool applies the same workspace check per task repository. In `local_only` mode no hosted agent is dispatched or has its models listed, on any path. A missing or stale smoke does not block assignment; it only shows as `unverified` in the selection evidence. Real orchestration outcomes feed back into the cache at the scope they prove: a success confirms unattended execution, a permission prompt marks the agent interactive-only, a trust refusal is recorded against that workspace only, and a quota, session, or rate limit is recorded at model scope when the model is known.

## Factory preflight

`howlplane factory doctor` reports readiness for a long unattended campaign:

* agents that can implement, and agents that can review
* whether an independent auditor exists alongside the implementer
* interactive-only, unavailable, and capacity-unknown agents
* known limits, and agents that passed a smoke
* the default execution budgets
* the Factory worktree, whether it is authorized, and which agents are workspace trust gated there

The overall result is `READY` when every usable agent is verified and an independent auditor exists. It is `DEGRADED` when work can run but some evidence is unverified or no independent audit is possible. It is `BLOCKED`, with exit code 1, when no autonomous implementation worker is usable. UNKNOWN capacity is listed but never counted as unavailable.

Trust-gated agents are not workers in the Factory worktree. If some are gated and others can still implement and review, the campaign is `DEGRADED`. If every implementation-capable agent is gated, it is `BLOCKED`.

`howlplane factory run` and `factory start` accept `--preflight {off,warn,require}`, default `off`. With `warn` or `require`, workspace trust for the campaign's worktree is checked before any worker is dispatched. `warn` prints the readiness report when it is not READY. `require` also refuses to start a BLOCKED campaign.
