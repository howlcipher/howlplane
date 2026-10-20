# ADR 0008 — `howlplane write`: HowlWriter as a governed tool

## Status
Accepted — carve-out authorized by the repository owner on 2026-10-19
(`documentation/CONTROL_PLANE.md` §1.1.2).

## Context

HowlWriter is the ecosystem's writing system: grounded project drafts, humanizing,
voice profiles, and validation that says whether a draft is ready for human review.
HowlPlane could reach it in one place only. `howlplane creative` runs
`howlwriter native` inside the Dream → Writer → Create copy pipeline. Nothing
routed an ordinary writing prompt ("draft the report for this project", "write a
short note on X") to HowlWriter, so every such run was started by hand, with no
run record, no resume, and no place for an owner-only decision to wait.

## Evidence

From a real-assignment HowlWriter dogfood run, 2026-10-16 to 2026-10-19, measured
from its command receipts (`FINAL_COMMAND_OUTCOMES.json`; the run's private
material stays with the owner):

| Evidence | Measurement | Defect class |
| :--- | :--- | :--- |
| Writing commands started through HowlPlane | **0 of 15** recorded commands (9 HowlWriter `project` commands, 6 direct provider reviews); all were run by hand | Severe usability: no governed path from a writing prompt to the writing tool |
| Drafts whose process exit hid a failed verdict | **2 of 2** model drafts exited 0 with validation verdict BLOCKED | Verification: a script reading the exit code would have shipped them |
| Runs that failed before inference | **2 of 15** exited 2 (no model chosen, HowlPlane not importable) | Severe usability: no preflight, no record of what was missing |
| Commands covered by `creative` | **0 of 15** used `howlwriter native` | Verification: the only existing integration did not cover the work |

The HowlWriter-side defects are fixed (howlwriter #29: `--fail-if-blocked`,
named import errors; howl #19: the runtime receives HowlPlane). What remains is
the missing governed path, which only HowlPlane can provide.

## Decision

Add `howlplane write {run,resume,status}` (`write_pipeline.py`), built on the same
pattern as `creative_pipeline.py`:

- HowlPlane runs the `howlwriter` CLI on PATH and never imports it. Stages
  (preflight, write, check, audit) persist to an explicit run directory, and a
  stage is complete only while its outputs still match their recorded hashes.
- The operation is chosen from the inputs, never by a model: `--operation`, or
  `project-draft` with `--project`, `humanize` with `--input`, else `compose`.
- HowlWriter's exit codes keep their meaning. 3 records NOT_READY (the draft is
  kept), 4 AWAITING_OWNER, 2 NEEDS_INPUT; `howlplane write` exits with the same
  code, and `write resume` retries the blocked stage after the owner acts.
- `--provider` is required and recorded. The child runs without inherited
  `HOWLWRITER_PROVIDER`/`HOWLWRITER_REVIEW_PROVIDER` and with stdin closed, so the
  run record names every model, and HowlWriter can never wait on a question that
  nobody sees.
- `check` runs HowlWriter's deterministic `lint --json`; `write-audit.json`
  credits HowlWriter with the text and HowlPlane with orchestration only, and
  names the check behind `ready`. A humanized file has no such check, so its
  `ready` is null rather than true.

## Consequences

- **Authority.** HowlPlane gains no authority. It never passes `--owner-confirmed`
  or `--allow-remote`: the command line is built only from fixed templates, a guard
  refuses either flag before HowlWriter runs, and the prompt is passed as
  `--instructions=<prompt>` or after `--`, so a prompt like `--owner-confirmed`
  stays text. Sending project evidence to a remote model still needs the owner's
  own `howlwriter project authorize-remote` at a terminal.
- **Loops.** HowlWriter's model steps run agent CLIs with no tools in an empty
  directory, so they cannot call back into HowlPlane. The remaining cost of
  nesting is a second session on the same account: inside Claude Code
  (`CLAUDECODE=1`), `--provider claude` is refused unless `--allow-nested` is given.
- **Egress.** What leaves the machine is exactly what HowlWriter itself sends to
  the model named by `--provider`; `local` sends nothing. See `data_flows.md`.
- **Coupling.** HowlPlane depends on HowlWriter's documented CLI contract (global
  `--provider`, `--local-model`, `writer --fail-if-blocked`,
`project draft --json --fail-if-blocked`, `humanize`,
  `lint --json`, exit codes 2/3/4). A change there must update this pipeline and
  its tests.

## Rejected alternatives

- **An MCP server in HowlWriter for HowlPlane's agents.** More flexible, but an
  agent could pass owner-only flags on its own judgment, and agent → tool → agent
  loops become likely. No operational evidence asks for ad-hoc tool calls.
- **A skill telling agents to call the `howlwriter` CLI.** Cheapest, but nothing
  governs or records the run.
- **Extending `creative`.** Its contracts are the copy pipeline's; a prompt-driven
  draft does not fit them.
