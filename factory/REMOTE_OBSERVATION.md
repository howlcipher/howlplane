# Remote Factory observation and admission

Persistent roles that are not on the Factory host can read campaign state and
enqueue work for the **one** supervisor that is already running. This does not
install a second Factory and it does not require a shell on the host.

The live continuous-improvement campaign is `2026-09-27-continuous-improvement`
(`factory/CONTINUOUS_IMPROVEMENT.md`, workspace `factory/howl-workspace.yaml`).
Do not start `howlplane factory start` from anywhere else.

HowlFutureWorks `reports/work-items/HOWL-*.json` is org desired state. It is
not a Factory queue. The running supervisor does not discover those files.

## Status snapshot

From the Factory host checkout that should carry the snapshot:

```bash
howlplane factory status --publish
```

That reads the persisted supervisor (`reconcile_restart` stays off, same as
`factory status`) and writes `factory/status/remote-snapshot.json`. It does
not call `factory start` and it does not take the supervisor lock.
`--publish-path PATH` overrides the destination. The path must sit outside the
Factory state directory.

`--json` remains the local, unredacted status print. The file from `--publish`
is the redacted artifact. With both flags, JSON stays on stdout and the
snapshot path is printed on stderr.

Schema: `howlplane.factory.status/v1`.

| Field | Meaning |
| --- | --- |
| `campaign_id` | Factory campaign id when the host could resolve one, otherwise null |
| `mission_campaign_id` | `campaign_id` from `.dogfood/mission_state.json` in the checkout that holds the snapshot, when that id is a short token |
| `repository` | `owner/name` slug when the status project or remote identifies one |
| `state` | Supervisor state (`idle`, `dispatching`, `waiting_for_work`, ...) |
| `current_dispatch` | Active dispatch id, or the literal `idle` when none is recorded |
| `blockers` | Parked work and proposals. `class` is `OWNER_REQUIRED`, `BLOCKED`, or `DEFERRED` |
| `owner_required` | True when any blocker is `OWNER_REQUIRED`, including `waiting_for_authority` and a missing authority envelope |
| `last_tick_at` | Last recorded tick time |
| `last_error` | Last supervisor error after redaction, or null |

The writer does not copy workspace paths, worktree paths, process command
lines, provider inventory, or raw task output (`recent_completed`,
`recent_failed`, `recent_parked`). Free text that is copied (`last_error`,
blocker summaries, objective) is scanned for bearer tokens, GitHub tokens,
`sk-` keys, JWTs, cookies, `token` / `password` / `api_key` assignments, and
URL userinfo. Absolute paths become the repository slug when the final path
component is that repository, and `[path]` otherwise.

Commit and push `factory/status/remote-snapshot.json` from the host. Remote
readers use Git. Until the first publish, the file is absent and status is
unknown.

### Periodic refresh

```bash
howlplane factory status --publish --arm-periodic
```

`--arm-periodic` writes `status_publish.path` in the state directory (an
absolute snapshot path). A supervisor process **running this code** refreshes
the snapshot after each loop tick and after `run-once`. Arming does not
restart or start a campaign. An already-running process keeps its old code
until the host restarts it onto a build that contains this hook. One-shot
`--publish` works against the existing state files without that restart.

Publishing fails closed if the destination is inside the state directory or
would replace `factory_supervisor.json`, `process.json`, or `campaign.json`.
A publish error is logged and does not stop the supervisor.

## How remote work is admitted

The executing path is a ranked backlog row the existing `BacklogSource`
already reads. No new queue is introduced.

### Ranked backlog `Pending` row

Put a row under the first `## Ranked Backlog` heading in `issues.md`,
`bugs.md`, or `improvements.md` of a repository the campaign already watches.
`BacklogSource` (`src/howlplane/control_plane/backlog_source.py`) reads those
three names, in that bug-then-improvement order: `bugs.md` and `issues.md`
are bug ledgers (`issues.md` is the ledger in this checkout; `bugs.md` is the
HowlFrame-style name), then `improvements.md`. Within a file, row order is
kept.

The status cell must be exactly `Pending`. Values such as
`Pending — blocked on #88` are not eligible. Rows below the ROI floor are not
eligible. Eligible rows are admitted as `existing_backlog`
(`WorkItemOrigin.EXISTING_BACKLOG` in `factory/work_item.py`) and become
`ready` under the campaign's current authority envelope. Authority, approvals,
and HowlFrame boundaries are unchanged.

A remote role opens a pull request that adds that row in a watched portfolio
repository. The Factory discovers it on a later tick only after the host
checkout named by `factory/howl-workspace.yaml` contains the commit. The pull
request alone is not a queue.

See `factory/examples/ranked-backlog-row.md`. Factory does not discover that
example. Discovery reads only `bugs.md`, `issues.md`, and `improvements.md`
at the repository root.

### Surfaces that are not the remote admit path

`owner_direction` is a work-item origin. The supervisor dispatches it only
when discovery supplies `trusted_provenance`. Untrusted owner direction parks
in `awaiting_owner` (`OWNER_REQUIRED`). A Git commit cannot mark itself
trusted, so this is not the path a remote role uses to enqueue executable
work.

`howlplane factory queue QUEUE.json` is the finite host-side queue in
`documentation/FACTORY_QUEUE.md`. Schema: `howlplane.factory_queue/v1`. Only
`APPROVED` tasks run. The persistent supervisor does not discover this file. A
host operator runs the command. `factory/examples/factory-queue.example.json`
is a `PROPOSED` example, so the queue skips it.

```bash
howlplane factory queue path/to/QUEUE.json --dry-run
```

## What comes back

Git commits and pull requests on the target repository are the ship evidence
remote roles can read. The redacted snapshot reports supervisor state. It is
not the evidence ledger and it does not grant authority.
