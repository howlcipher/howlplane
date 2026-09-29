# Example ranked backlog row

This file is not `bugs.md`, `issues.md`, or `improvements.md`, so Factory
discovery does not read it. Copy the row into the live `## Ranked Backlog`
table of one of those files in a repository the campaign already watches.
The status cell must be exactly `Pending`.

## Ranked Backlog

| # | Title | Status | Score | Rationale |
| --- | --- | --- | --- | --- |
| 91011 | [Publish redacted factory status](#91011-publish-redacted-factory-status) | Pending | 2.0 (4x1/2) | Remote operators cannot see the live campaign. |

### 91011. Publish redacted factory status

Symptom: operators who are not on the Factory host cannot see campaign state.

Deterministic acceptance: `factory/status/remote-snapshot.json` contains `campaign_id`,
`state`, `current_dispatch`, blockers, `last_tick_at`, and `last_error`, and
the file contains no tokens or absolute host home paths.
