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
| `last_successful_tick_at` | Last tick that completed work, or null |
| `last_error` | Last supervisor error after redaction, or null |
| `current_work_item_id` | Work item being processed, or null |
| `failure_count` | Consecutive failures recorded by the supervisor |
| `stopped_reason` | Why the supervisor stopped (for example `operator_stop`), or null |
| `objective`, `target_mode`, `run_mode` | Campaign objective, target mode and run mode, redacted |
| `authority` | Authority profile name or `not configured`; null if not a known value |
| `published_at` | When the snapshot was written. The freshness clock |
| `redacted` | Always `true`; a reader must reject a snapshot without it |
| `operator` | Stable summary shared with the CLI: `label`, `severity`, `reason_code`, `owner_required`. No free text |

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

### Read contract: fresh, stale, absent

Schema version is the `schema` field (`howlplane.factory.status/v1`). A reader
must reject any other value. `howlplane factory snapshot [--json]` (library:
`status_publish.read_snapshot`) is the read-only reference implementation. It
never writes, never takes the supervisor lock and never starts a campaign.

| `freshness` | Meaning | Reader must |
| --- | --- | --- |
| `FRESH` | Valid v1 snapshot, `published_at` within the fresh window (default 1800 s; the reader may pass its own) | Show state normally |
| `STALE` | Valid, but older than the window | Show the last state labelled stale; do not claim the Factory is running |
| `SNAPSHOT_ABSENT` | No file at the path | Show "status unknown", not healthy |
| `SNAPSHOT_INVALID` | Unparseable, wrong `schema`, no `published_at`, or dated in the future beyond 5 minutes of skew | Reject; treat as unknown |

A file cannot contain the SHA of the commit that stores it, so the reader
derives identity from Git: `path` (repository-relative), `tip_sha` (the commit
that last touched the snapshot) and `head_sha`. Board shows `tip_sha` to prove
it is projecting the Git tip and not a side channel. These values are not
fields in the v1 document.

### Pending row identity and validation evidence

`howlplane factory pending [--json]` projects the ranked `Pending` rows the
existing `BacklogSource` admit path already reads: `item_id` plus
`source_file` is the row identity, `task_id` is what admission derives from it.
`howlplane factory pending --validate ROW_ID --recorded-by NAME --json` emits
`howlplane.backlog.validation/v1` evidence: row id, task id, whether admission
would accept it, the detail-section hash, who recorded it and the repository
HEAD. Committing that output gives a record rebuildable from Git. These
commands are read-only: there is still one admit path, no second queue, no
supervisor lock, and no trusted `owner_direction`.

### Ownership: Plane, Board, Factory

- **Plane** is the governed engine and control plane. It supplies the Git
  snapshot, the read contract above and the single admit path.
- **Board** is the rich human work surface. It renders the campaign, blockers
  (`OWNER_REQUIRED`, `BLOCKED`, `DEFERRED`) and the step from a Pending row to
  a mission. No such UI lives in this repository.
- **Factory** is the persistent engineering loop that Plane runs.

### Publish smoke (operator checklist)

1. On the Factory host checkout: `howlplane factory status --publish`.
2. Commit and push `factory/status/remote-snapshot.json`.
3. From any clone: `howlplane factory snapshot --json`. Expect `FRESH`, a
   `tip_sha` equal to the commit from step 2, and a `published_at` that moved.
4. Delete or rename the file in a scratch clone and confirm `SNAPSHOT_ABSENT`.
5. Optional cadence: `howlplane factory status --publish --arm-periodic`, only
   after the host supervisor runs a build that contains the periodic hook.
   Arming neither starts a campaign nor takes the lock, so do not run
   `factory start` to "help".

The live before/after tip proof in step 3 needs the Factory host and is an
Owner action.

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

## Org record

HowlFutureWorks tracks this as HOWL-011. The org docs change is
https://github.com/howlcipher/HowlFutureWorks/pull/16. That record still has a
Plane PR URL placeholder. The follow-up that fills it should use:

- Plane PR: https://github.com/howlcipher/howlplane/pull/123
- Command: `howlplane factory status --publish`
- Artifact: `factory/status/remote-snapshot.json`
- Admit path: an exact `Pending` row in `issues.md`, `bugs.md`, or `improvements.md`

The host operator merges the Plane PR and publishes from the Factory host.
As of 2026-09-30 `factory/status/remote-snapshot.json` is committed on `main`
(commit 6276da3, a snapshot of a stopped campaign). Whether it was produced on
the tallgeese host is not verifiable from this repository.

## Follow-ons filed 2026-09-29

The seat-poll sequence after this contract is
`documentation/journals/2026-09-29_team_howlplane_backlog.md`, and Pending
rows 70 through 74 in `improvements.md`. Those rows do not change this
contract. They record host dogfood, a locked snapshot schema, admit evidence,
Board as the consumer of that schema, and a freshness proof. The unanimous
constraints (no second Factory, bots do not take the supervisor lock or start
a campaign or write trusted `owner_direction`, leave pull request #122 alone,
no parallel admit path) stay constraints.
