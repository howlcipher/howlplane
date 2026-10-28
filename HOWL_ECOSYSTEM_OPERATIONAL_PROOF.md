# Howl Ecosystem Operational Proof

## Executive assessment

Status: PARTIALLY VERIFIED (baseline; execution in progress).

This report records the 2026-10-28 mission. No completed or verified vertical slice is claimed at baseline. Repository identities, local cleanliness, GitHub default branches, open pull requests, release listings and CI workflow paths are recorded in `evidence/operational_proof/baseline.json`.

## Baseline architecture and integration

| Producer → consumer | Interface | Trust boundary | Baseline evidence | Classification / next action |
| --- | --- | --- | --- | --- |
| Factory queue → Plane orchestration | howlplane.factory_queue/v1 → howlplane.orchestration/v1 (state revision 3) | Explicit approved task; isolated Git workspace | Existing finite queue, fingerprint, retry, supersede and subprocess workers | Exercise live; blocker until demonstrated |
| Plane → remote coding CLI | Agent readiness/v1; provider execution profiles | Existing subscription auth; role permissions; no paid APIs | Codex and Claude report authenticated; old smoke evidence stale | Bounded real run needed |
| Provider core → remote/local commands | Versioned interop and provider policy | Runtime inference denial | HOWL_FORBID_LOCAL_INFERENCE recognized; bare alias needs inspection | Potential policy blocker |
| Frame → Plane | Compiled assessment/audit artifacts; HFBC1 | Capability grants and finite instruction limits | Published compiler compatibility differs from generated-product source pin | Real execution and exact pins required |
| ChangeOps → protected execution | Model B detached approval / protected executor foundation | Independent owner and credential custody | Production CLI deliberately blocked; deployed authenticator/adapters absent | Release BLOCKED; do not enable |
| Proof → Plane / Board | Finding and evidence file adapters | Independent judgment; no execution authority | Adapters exist; no ChangeOps verdict ingest | Exercise supported adapters; do not invent automatic ingest |
| Relay → worker | Versioned checkpoint file | Worker assertions are not authority | Bounded checkpoint ingestion exists | Recovery proof required |
| Plane / Proof → Board | ai.evidence_entry/v1 offline import | Read-only display, whole-input validation | Offline import requires stopped server; multi-file finalization not atomic | Exercise safely, report limitation |
| Forge / FutureWorks → Plane | Workforce / mission contracts | Advisory role definitions, no authority delegation | Existing contracts under inspection | Optional unless needed by slice |
| Howl installer → components | Versioned manifest and checksummed releases | Download checksums and explicit install profile | Release coverage narrower than ecosystem; source builds distinct | Clean production install unproven |
| Align → workers | Assistant config | Configuration does not grant authority | Existing installed CLIs used | Optional |
| Writer / Create / Dream / Instinct | Existing public tool interfaces | Their own evidence/egress gates | No demonstrated need for log CLI | Out of implementation scope |

## Bounded engineering mission

Build a standalone Python standard-library incident-log CLI in a new isolated Git repository. JSONL validation, category/time/source grouping, safe malformed-input handling, filters, deterministic output, report redaction, CLI documentation, automated regression tests, and clean installation are explicit acceptance criteria. The orchestrated worker must author the implementation. This report author will not prewrite application code.

Use the existing finite queue and orchestration rather than a new adapter: this preserves lifecycle ownership and existing recovery. Python avoids runtime dependencies and supports clean installation; Go offers a standalone binary but adds toolchain/build scope. HowlFrame is exercised separately as an ecosystem boundary, rather than forcing an unsupported log-application generator path.

## Execution and verification record

Pending. Both FORBID_LOCAL_INFERENCE and HOWL_FORBID_LOCAL_INFERENCE will be set to 1 for mission processes. No releases, deployments or protected operations are authorized. Shared-account model approval is not independently authenticated authority.

## Development record

Report and integration evidence owner: HowlPlane, branch `mission/operational-proof-20261028`, isolated worktree. Existing source checkouts remain untouched. No commits, PRs or merges yet.

## Remaining work

Execute the slice, inject faults, test real boundaries, obtain independent review, record commands/results, and revise this assessment from observed evidence. Governed release remains separate and blocked.
