# Remote Factory observation and admission

Persistent roles that are not on the Factory host can read campaign state and
enqueue bounded work for the **one** supervisor that is already running. This
does not install a second Factory and it does not require a shell on the host.

The live continuous-improvement campaign is `2026-09-27-continuous-improvement`
(`factory/CONTINUOUS_IMPROVEMENT.md`, workspace `factory/howl-workspace.yaml`).
Workspace paths in that file are host-local. Do not copy them into status
snapshots, and do not start `howlplane factory start` from anywhere else.

## Status snapshot

From the Factory host checkout that should carry the snapshot:

```bash
howlplane factory status --publish
```

That reads the persisted supervisor (`reconcile_restart` stays off, same as
`factory status`) and writes `.dogfood/factory-status.json`. It does not call
`factory start` and it does not take the supervisor lock. `--publish-path PATH`
overrides the destination. The path must sit outside the Factory state
directory.

`--json` remains the local, unredacted status print. The file from `--publish`
is the redacted artifact. With both flags, JSON stays on stdout and the
snapshot path is printed on stderr.

Schema: `howlplane.factory.status/v1`.

| Field | Meaning |
| --- | --- |
| `campaign_id` | Factory campaign id when the host could resolve one, otherwise null |
| `mission_campaign_id` | `campaign_id` from a sibling `.dogfood/mission_state.json` when that id is a short token |
| `repository` | `owner/name` slug when the status project or remote identifies one |
| `state` | Supervisor state (`idle`, `dispatching`, `waiting_for_work`, ...) |
| `current_dispatch` | Active dispatch id, or the literal `idle` when none is recorded |
| `blockers` | Parked work and proposals. `class` is `OWNER_REQUIRED`, `BLOCKED`, or `DEFERRED` |
| `owner_required` | True when any blocker is `OWNER_REQUIRED`, including `waiting_for_authority` and a missing authority envelope |
| `last_tick_at` | Last recorded tick time |
| `last_error` | Last supervisor error after redaction, or null |

The writer drops workspace paths, worktree paths, process command lines, and
provider inventory. Free text is scanned for bearer tokens, GitHub tokens,
`sk-` keys, JWTs, cookies, `token` / `password` / `api_key` assignments, and
URL userinfo. Absolute paths become the repository slug when the final path
component is that repository, and `[path]` otherwise.

Commit and push `.dogfood/factory-status.json` from the host. Remote readers
use Git. Until the first publish, the file is absent and status is unknown.

### Periodic refresh

```bash
howlplane factory status --publish --arm-periodic
```

`--arm-periodic` writes `status_publish.path` in the state directory (an
absolute snapshot path). A supervisor process **running this code** refreshes
the snapshot after each loop tick and after `run-once`. Arming does not restart
or start a campaign. An already-running process keeps its old code until the
host restarts it onto a build that contains this hook. One-shot `--publish`
works against the existing state files without that restart.

Publishing fails closed if the destination is inside the state directory or
would replace `factory_supervisor.json`, `process.json`, or `campaign.json`.
A publish error is logged and does not stop the supervisor.

## How the running Factory admits work

Discovery runs inside the existing supervisor tick. It does not start another
campaign. The host checkout (or the workspace paths the campaign already
uses) must contain the files. Opening a pull request is not enough until that
checkout has the commit.

### 1. Ranked backlog `Pending` row (executed)

This is the path that becomes dispatchable work.

Put a row under the first `## Ranked Backlog` heading in `bugs.md` or
`improvements.md`. Bugs are read before improvements. Within a file, row order
is kept. The status cell must be exactly `Pending`. Values such as
`Pending — blocked on #88` are not eligible. Rows below the ROI floor are not
eligible. `BacklogSource` admits eligible rows as `existing_backlog`, which the
portfolio moves to `ready` under the campaign's current authority envelope.
Authority, approvals, and HowlFrame boundaries are unchanged.

See `factory/examples/ranked-backlog-row.md`. That file is an example only.
Factory does not discover it, because discovery reads `bugs.md` and
`improvements.md` at the repository root.

### 2. Owner direction file (parked)

Copy `factory/examples/owner_direction.example.json` to
`factory/owner_direction/<id>.json` in a discovered repository. Drop the
`.example` suffix. Schema: `howlplane.factory.owner_direction/v1`.

| Field | Required | Rule |
| --- | --- | --- |
| `schema` | yes | Exactly `howlplane.factory.owner_direction/v1` |
| `id` | yes | `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `title` | yes | Non-empty |
| `goal` | yes | Non-empty |
| `constraints` | no | Up to 20 non-empty strings |
| `repository` | no | When set, must match the checkout slug or its name |

The supervisor loads these files on each tick from the target repository, or
from every repository in `workspace_file` when that field is set. A file
cannot set `trusted_provenance`. Admission always uses untrusted owner
direction, so the item parks in `awaiting_owner`. The redacted snapshot
reports that as `OWNER_REQUIRED`. The item is not dispatched and does not
preempt the portfolio.

Malformed files, symlinks, files larger than 64KiB, and files whose
`repository` does not match the checkout are skipped.

### 3. Finite queue (host operator, separate command)

`howlplane factory queue QUEUE.json` is the finite queue documented in
`documentation/FACTORY_QUEUE.md`. Schema: `howlplane.factory_queue/v1`. Only
`APPROVED` tasks run. The persistent supervisor does **not** discover this
file. A host operator runs the command explicitly. It is not `factory start`
and it is a second executor if it is pointed at a repository the live
campaign is already dispatching, so prefer a backlog row for work the running
campaign should pick up.

`factory/examples/factory-queue.example.json` is a `PROPOSED` example. The
queue skips `PROPOSED` tasks.

```bash
howlplane factory queue path/to/QUEUE.json --dry-run
```

## What comes back

Git commits and pull requests on the target repository are the ship evidence
remote roles can read. The redacted snapshot reports supervisor state. It is
not the evidence ledger and it does not grant authority.
