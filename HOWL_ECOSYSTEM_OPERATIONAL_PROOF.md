# Howl Ecosystem Operational Proof

## Executive assessment

**Overall status: PARTIALLY VERIFIED. Governed release: BLOCKED.**

A real Factory assignment produced a standalone, executable incident-log CLI. Codex authored the implementation, tests, packaging and build backend through HowlPlane. Sixteen application tests pass, including offline installation from an extracted, reproducibly built source archive. An independent worker found two real defects; the recovery worker repaired them and independent retesting passed. Real worker termination preserved source, real coordinator interruption preserved a checkpoint, and Factory retained the superseded revision rather than erasing its history.

The native workflow did **not** finish independent review or acceptance. Claude planning timed out, Cursor review timed out, and Devin review failed. Independent sibling-worker review is separate evidence, not a fabricated successful native review. HowlProof executed and returned **REJECT** for literal synthetic credential-shaped test fixtures; Plane actually ingested those findings and retained the human-judgment requirement. No finding was silently dismissed.

The most important remaining safety blocker is independently protected owner authentication, credential custody and execution. ChangeOps had source paths enabling Model B despite documentation claiming it was unavailable. A reviewed, tested correction now exists in an unmerged security PR; it has not been deployed. Board also has a reproduced non-atomic publication boundary. This milestone's complete acceptance checklist is not achieved.

## Baseline and provenance

Discovery preceded application implementation. Baseline report commits are `aa1690f` and `87abcd7`. [baseline.json](evidence/operational_proof/baseline.json) records fifteen checkout commits/cleanliness, live GitHub default names, open PRs, release listings and CI workflow paths. [default_branch_heads.json](evidence/operational_proof/default_branch_heads.json) records live default tips separately.

Plane execution/source baseline is `07175bd6018ce4262656089db29698396cff51d0`. Live default is `24ac9ad14a47410065433f9ac11ece7b90fc5ce6`: its additional changes concern provider test isolation and Frame skill documentation. This mission preserves its recorded baseline; it does not claim those later test changes were in its execution. Forge local `main` is `3d6d6c67`, but its GitHub default remains `claude/howlforge-initial-build` at `9dbee38a`. The latter's integration document describes a proposed Plane conversation; executable Plane source does not call Forge. These differences are explicit compatibility/library-sync candidates.

ChangeOps documentation said Model B execution was unavailable and enrollment/adapters absent. Live source exposed enrollment/signing commands, real adapters and an environment-controlled execution path. Live source takes precedence. The security PR corrects both implementation and misleading documentation.

## Architecture, ownership and integration

| Producer → consumer / owner | Interface and version | Authentication and trust | Before → after evidence | Failure / remediation | Blocking? |
| --- | --- | --- | --- | --- | --- |
| Howl → installed components | Manifest schema 1; checksummed release archives/wheels | Public releases, checksums, explicit standard profile | Actual assets inspected; real empty-home standard install, recovery and repeat install succeeded | Not a clean OS image; released Frame lacks interop; Windows arm64 assets missing for Frame/ChangeOps despite manifest platform combinations | Linux slice: no; universal install: yes |
| Factory → Plane orchestration | `howlplane.factory_queue/v1`; `howlplane.orchestration/v1`, state revision 3 | Approved bounded queue, Git baseline, session ownership | Existing queue → two actual revisions, preserved supersession, checkpoints and real verification | Native audit cannot finish with observed providers | Yes |
| Plane → coding CLIs | `howlplane.agent_readiness/v1`; subprocess execution profiles | Existing CLI logins, connected policy, role permissions | Real Codex planning/implementation, real Claude/Cursor/Devin attempts | Authentication does not prove invocation availability; unknown capacity remains unknown | Yes for native independent audit |
| Frame → Plane | HFBC1 assessment/audit artifacts; interop descriptor | VM capability/instruction limits; advisory assessment only | Actual published v0.1.0 binary executed 13 selected artifact tests successfully | Generated-product runtime has separate immutable source pin `41d56cec`; version labels alone insufficient | Tested artifact scope: no |
| ChangeOps → protected execution | Model B Ed25519 binding and SQLite authority foundation | Independent signer/custody is a deployment requirement, not a signature algorithm property | Unsafe source/docs discrepancy reproduced → fail-closed CLI correction in PR24; protocol/race/security tests pass | Independently protected owner authentication, enrollment, credentials, executor and current-artifact policy recheck remain undeployed | Release: yes |
| Proof → Plane | `ai.review_finding/v1`, advisory handoff and `howlproof.plane_ingestion/v1` | Bundle integrity; no authority granted; this handoff not independently authenticated | Real source evaluation → REJECT → real Plane reconcile, two unresolved human-judgment findings | Literal synthetic fixture strings trigger scanner; no automatic acceptance; no ChangeOps verdict ingest | Acceptance: yes |
| Plane/coordinator → Board | `ai.evidence_entry/v1`, offline importer under pinned Frame `e77e008f` | Read-only projection, server stopped, coordinator records labeled | Real import/replay/rejection tests; final projection plus actual isolated HTTP API readback passed | Two-store publication can mutate missions before timeline failure; no transactional finalization guarantee | Durability milestone: yes |
| Forge → Relay | `howlforge.checkpoint/v1`, decisions/verification schemas | Worker assertions are continuity evidence, never approval | Existing adapters and deterministic tests; Relay full baseline 76 pass | Not invoked by the live Factory slice; no invented Forge participation | No |
| Plane → continuity | Native session/checkpoint and Git evidence | Fenced coordinator state and source hashes | Real worker and coordinator interruption, four file hashes preserved, superseded manifest retained | Manual bounded recovery revision required; not unattended continuous recovery | Autonomous reliability: yes |
| Provider-core → consumers | Policy API and `howl.interop/v1`; released v0.2.1 consumers | Runtime denial flags, no paid experiments | Generic local-denial alias bypass reproduced without inference → monotonic dual-alias denial, 60 tests, PR9 | Consumers still use old release until explicitly authorized release/pin update | Policy deployment: yes |
| FutureWorks → workforce/Plane | Organization/workforce definitions, versioned handoff and `howl.interop/v1` compatibility check | Advisory organizational intent, not execution permission | 22 selected routing/interop tests pass | No live mission intake through orgctl; Factory owns this slice | No |
| Align → assistants | Config schema 0.1.5 and interop | Configuration is not authorization | Inspected existing interfaces; owner config preserved | Optional; not used to fabricate worker readiness | No |
| Writer/Create/Dream/Instinct | Existing CLI/API and provider-core contracts | Component-specific evidence and egress policies | Interfaces/pins inspected; no demonstrated log-CLI integration need | Not invoked; no scope expansion | No |

No new component, agent framework, protected deployment or package dependency was introduced. The reusable runner is an explicit test tool owned by Plane, not a replacement orchestrator.

### Decisions and rejected alternatives

Existing Factory/orchestration preserves lifecycle ownership, provider failover and recovery; a new orchestrator would duplicate those responsibilities. A thin deterministic test runner makes existing component suites reproducible without claiming that separate suite executions equal an integrated mission.

Python's standard library supports a dependency-free log application and offline installation; Go would add a binary toolchain to this particular assignment. Frame is tested at its real existing assessment/Board boundaries rather than forcing the application through an unrelated generated-product template. The worker supplied a small in-tree standard-library packaging backend; repeated source builds and clean extracted-source installation were actually tested.

Protected execution is disabled rather than exposed behind a caller-controlled opt-in. Board transactional redesign, organizational workforce integration and creative tools were left outside this slice because they require different work or have no demonstrated need here.

## Engineering execution

Standalone repository: `operational_missions/incident_sift_20261028`.

Final signed source commit: **`02f9a2c0bfeef5eb0a621324ebdf6eb884c00705`**. Initial contract: `db34738d`; preserved interrupted checkpoint: `cdccf0c3`.

Portable deliverables: [Git bundle](evidence/operational_proof/incident_sift.bundle), [source archive](evidence/operational_proof/incident_sift-1.0.0.tar.gz), [manifest with hashes](evidence/operational_proof/application_manifest.json). Source archive SHA256: `16c190585b82b21e751958566277bd37272a253aeedf1a190a1cdad0a0b7bcd6`. No remote application repository, release or deployment was created.

The application validates UTF-8 JSONL and aware timestamps, groups error/critical records by UTC hour/category/source, filters deterministically, safely counts malformed data, rejects non-JSON constants, redacts report labels, omits messages/metadata and documents exit behavior. Its standard-library backend provides offline console-script installation and deterministic source builds.

### Actual workflow

1. Established MISSION.md and a committed empty-implementation baseline, then submitted approved `INCIDENT-SIFT-001` through the supported finite queue.
2. Claude planning reached its real 300-second deadline. Plane recorded `HANDOFF REQUIRED`, not provider quota exhaustion or success.
3. Resumed the same session with the configured Codex default. The initial isolated readiness cache had no advertised models, so an explicit model could not be selected; no unsupported model was invented. Codex's actual invocation header identified `gpt-6.1-sol`/OpenAI; some native manifest model fields correctly remained `UNKNOWN`.
4. Killed the real Codex process tree during implementation after four Python files existed. Plane recorded the failed invocation and checkpoint, then handed off to Claude. All four file hashes survived at handoff.
5. Interrupted the coordinator and its still-running fallback worker; its durable state became `INTERRUPTED`. Committed the preserved source and independently reproduced findings.
6. Changed the bounded recovery contract. Factory retained session `c1d70482…` as `SUPERSEDED`, then created `8f48ccf3…`; it did not erase the first attempt or silently restart under the old definition.
7. Codex repaired findings, completed packaging and ran verification: 16 tests passed. Native orchestration refused completion without independent review.
8. Added the authenticated Cursor reviewer through supported resume options: 180-second timeout. Added authenticated Devin with its listed `claude-sonnet-4-6`: invocation failed. Native session remains **BLOCKED at review**; acceptance did not execute.
9. Separate independent sibling-worker review/retests passed, with same-provider independence limits recorded. Proof source scans ran separately and their rejected judgment flowed through real Plane reconciliation.

[Session summaries](evidence/operational_proof/session_summaries.json), native logs, [Factory ledger](evidence/operational_proof/factory_ledger.jsonl), and independent reviews preserve this history. Summaries intentionally omit coordinator lease tokens. Readiness/authentication and advertised models are not evidence that a worker completed a task. Tokens, subscription quota and cost were not exposed reliably; they are **unknown**, not zero. No paid API experiment or local inference was intentionally invoked. Both inference-denial environment aliases were set for mission processes; this is not an OS isolation guarantee.

### Reproduction

From this report's checkout:

```bash
git clone evidence/operational_proof/incident_sift.bundle /tmp/incident_sift
cd /tmp/incident_sift
bash scripts/test.sh
bash scripts/build.sh
python3 -m incident_sift --help
```

README.md in that repository contains the exact offline extracted-source venv install. Python 3.11+, Bash, venv and pip bootstrapping are required; there are no external build/runtime dependencies. Byte-identical archive reproduction is claimed with the same Python toolchain, not every possible platform/version. [application_reproduction.json](evidence/operational_proof/application_reproduction.json) records a real clone/test/build/lint reproduction.

## Verification and adversarial results

| Class | Actual execution | Result and limits |
| --- | --- | --- |
| Application correctness/build/install | Native `bash scripts/test.sh`; separate independent copied snapshot; clean Git-bundle clone | 16 tests pass; build twice, hash comparison, extracted-source offline fresh-venv installation and installed command outside source pass |
| Natural defect repair | Independent dotted API-key and NaN repros, recovery worker changes, independent retest | Both fixed; new subprocess regressions cover them |
| Deliberate defective implementation | Disposable source copy: increment changed from 1 to 2 | Tests exit1; exact-byte restoration matches original hash, all16 pass; simulated mutation/deterministic restoration, not claimed model repair |
| Worker interruption/handoff | Real SIGKILL of implementation worker descendants | Failed attempt/checkpoint/reroute observed; all four source hashes preserved; no claim that the killed worker finished |
| Coordinator interruption | Real SIGINT and worker termination | `INTERRUPTED` checkpoint retained; bounded revision/supersession used for recovery; owner/coordinator interventions disclosed |
| Provider failure | Real Claude/Cursor deadlines, real Devin exit1; 237 selected deterministic recovery/provider/authority tests | Real attempt failures separated from simulated contract tests; no quota/cost invented |
| Untrusted input | Command-like log message, disposable marker probe; malformed/type/bounds cases | No marker created and no raw message in report. Proves CLI data handling only; model-worker injection containment and exfiltration resistance remain unproven |
| Stale evidence | Real source receipt against cloned application, then simulated source mutation | Fresh receipt valid, changed artifact refused, exact-byte restoration valid; receipts cannot authorize promotion |
| Authority abuse | Actual Go signature/expiry/revocation/replay/concurrency/crash tests with synthetic keys; CLI bypass regression | Model B real actions refused in correction branch; cryptographic protocol tests do not establish independently protected custody |
| Interrupted finalization | Real Board VM importer with deliberately corrupt timeline store | Mission store written before exit1: reproduced failure, not recovery success. Exact import replay otherwise byte-identical; malformed/duplicate input leaves stores unchanged |
| Frame execution | Published v0.1.0 downloaded and both archive/binary hashes verified | 13 artifact tests pass with real VM; separate Board source pin compiled and executed |
| Component baseline | Relay76; Proof283 +2 skipped; provider baseline55 / correction60; Forge Go; HFW22; installer package suites | Independent component results, not fabricated end-to-end component participation |
| Runner | 20 behavior tests; real Relay/Proof/provider groups; independent adversarial review | Failures, timeout, background children, source mutation, stale receipts and output/source collisions fail closed; explicit fixed groups, no inference |
| Full Plane regression | Initial full run: 2645 passed,2 failed,1 skipped,3 deselected,1 warning | Two failures were missing devbox wrappers; native Go plus final runner targeted rerun22 pass. Final full run: 2656 passed,1 skipped,3 deselected,1 external ChromaDB deprecation warning in609.59s; Go suite passed, command exit0. Strict warning-free completion gate remains unmet |

[Independent app review](evidence/operational_proof/app_postfix_review.json) records source hashes and limitations. The later coordinator edits only updated changelog/finding dispositions; application code and archived source bytes remain the independently reviewed ones.

### Independent runner review

The first runner was falsified. It allowed a receipt to overwrite an excluded source path and could report success while a child survived a successful parent. Both counterexamples were reproduced and repaired. Output must now lie outside every measured repository, source files are never excluded, all process-group exits are cleaned up, and leftover background processes make the observation fail. Follow-up independent review found no remaining blocker in this narrow trusted-command scope.

The runner is not a sandbox or authenticated attestation. Inference flags are cooperative, hostile detached processes are outside process-group containment, ignored dependencies and external symlink referents are outside its source fingerprint, and power-loss durability of directory entries is not established. The CLI removes an inherited live-provider test opt-in and rejects nonfinite/unbounded timeout values. It never interprets a receipt as owner approval.

### Proof judgment and security disposition

HowlProof source-scanning and documentation checks ran under a deliberately narrow external contract. Both initial and final scans reject two literal credential-shaped **synthetic test values**; these are not evidence of real credential exposure, but their automated disposition remains unresolved. Plane's real reconcile reports two findings requiring human judgment. No acceptance exception, credential rotation, approval or protected operation was fabricated. A scan result cannot establish functional readiness, and sibling-worker verification does not override the native workflow's blocked review.

The existing Model B source provided more execution access than its docs claimed. PR24 removes the caller environment bypass and refuses real `authority execute` actions before loading approvals; only the existing pure `synthetic_action` protocol path remains, explicitly labeled synthetic. Durable SQLite anti-replay, atomic reservation and crash tests exercise real code with temporary keys/state. They are not proof of protected owner identity, credential isolation, fresh artifact/policy checks or a deployed protected executor. Cooperative terminal/shared-account signatures are not independent authentication. Actual release/tag/deployment operations remain unauthorized.

### Clean installation

A real published Howl v0.2.1 archive matched SHA256SUMS. A read-only-root bubblewrap environment mounted a new empty writable home over the owner's existing home without changing HOME or writing owner installations. Standard installation downloaded actual published Frame0.1.0, ChangeOps0.2.0, Plane CLI/engine0.1.0 and Writer0.1.0 artifacts. No developer-source fallback was substituted. An initial fixture lacked writable /tmp; after correcting the fixture, recovery install and repeat install exited0. Doctor exited0 but reported **DEGRADED** because optional Ollama was absent and released Frame lacks interop metadata. This is an empty-home supported-host test, not a fresh OS VM or proof that unreleased source changes are installed. Proof, Board, Forge, Relay and Align are outside that manifest.

## Development record

| Owner | Change / source commit | PR / disposition |
| --- | --- | --- |
| Plane | Mission report/evidence, inference-free component test runner and20 behavioral tests; baseline commits `aa1690f`, `87abcd7` | Focused operational-proof PR pending final evidence/gates; no completion or merge claimed yet |
| Provider-core | Generic inference-denial alias fix, signed `c3328e126e55caf916eaa9de5d8b9ae698b2c4d0` | [PR9](https://github.com/howlcipher/howl-provider-core/pull/9), CI Python3.11–3.14 pass; **unmerged, security owner approval required** |
| ChangeOps | Fail-closed undeployed Model B, signed `4f0ac30437e8e4816dc1fb484e6005f72c84c1fd` | [PR24](https://github.com/howlcipher/howlchangeops/pull/24), CI pass; **unmerged, security owner approval required** |
| Standalone application | Worker-authored app, signed final commit `02f9a2c0bfeef5eb0a621324ebdf6eb884c00705` | Local isolated repository plus portable Git bundle/archive; no release or remote publication |

Provider PR8 initially contained an unsigned agent commit. It was superseded by an identical-tree signed cherry-pick in PR9; PR8 is closed, its branch/history preserved, and no force push occurred. ChangeOps signature verifies locally; GitHub reports unknown_key, so GitHub-verified identity is not claimed. Initial standalone mission-contract commit is retained as historical provenance rather than rewritten. Existing owner branches/worktrees and credentials were preserved. No production service was deployed and no protected PR was merged.

## Acceptance accounting

| Criterion | Assessment |
| --- | --- |
| Actual assignment through supported workflow | Demonstrated by finite queue and native sessions |
| Eligible AI workers implemented real executable source | Demonstrated; Codex invocation identity observed |
| Reproducible build/tests/install | Demonstrated for application; ecosystem install scope/limits above |
| Independent verification | Demonstrated by separate worker and Proof; native provider audit blocked; same-provider worker limitation recorded |
| Confirmed defects resolved or blocked | Two app defects fixed; security corrections in unmerged PRs; Board failure unresolved |
| Interrupted work recovered without source loss | Demonstrated with explicit coordinator interventions and revision handoff; not fully unattended |
| Authority enforced without bypass | Correction branches fail closed; independently protected production authority unavailable |
| Provenance survives handoff | Hashes, checkpoints, superseded session, ledger and portable source retained |
| Board accurately represents final mission | Actual pinned Frame importer and transient isolated Board HTTP API readback demonstrate FAILED/noncompleted state, app pass, Proof rejection, REQUIRE_APPROVAL and absent envelope; coordinator/manual origin visible in timeline. Transactional publication remains unproven |
| Entire milestone VERIFIED | **No**: native audit, authority deployment and finalization remain blocked |

## Ranked remaining work

1. **Protected authority**: highest safety/correctness and dependency impact; substantial custody/deployment work, high value. Keep real execution unavailable until independently authenticated owner intent, protected keys/executor, artifact freshness, expiry/revocation and crash-safe anti-replay are demonstrated.
2. **Atomic Board publication**: high correctness and evidence-integrity impact; blocks finalization reliability. Bound a solution to an atomic mission/timeline generation or equivalent transaction, with deterministic kill/failure recovery tests.
3. **Reliable native independent review and verification parity**: high autonomy value; bounded provider diagnostics/timeout fixes, explicit fixture-finding disposition, compatible source/release pins and warning-free supported-toolchain gates. Do not turn authenticated discovery into claimed availability.

### Next three bounded missions

1. **Model B protected-deployment proof**: independently provision owner signer and executor custody; require exact artifact/policy binding; demonstrate missing, fabricated, expired, revoked and replayed approvals fail; race/crash retries never double-apply a synthetic protected effect. Acceptance requires explicit owner authorization before any real deployment/action. No production release is part of the test.
2. **Board transactional finalization**: inject failure before/after each publication mutation; readers see either the entire previous or entire new generation, recovery is idempotent, and incomplete outcomes never display completed/approved. Preserve existing ingestion schemas and verification distinctions.
3. **Repeat this same slice to native acceptance**: diagnose observed Claude/Cursor/Devin failures, obtain one real independent eligible reviewer under an explicit bound, rerun all application gates at the same source digest, retain Proof findings/dispositions, and publish an accurate transactional Board result from compatible pinned tools. Acceptance requires actual audit and acceptance evidence; unavailable providers remain failures. Do not start the future three-application or backend milestones here.

### Final local gate disposition

`make test-full` on the final source/test tree with native Go first on PATH exited0: **2656 passed,1 skipped,3 live tests deselected,1 warning**, followed by passing Go tests. The warning is external ChromaDB's Python3.14 use of deprecated `asyncio.iscoroutinefunction`. It was retained, not filtered. Under this repository's strict no-warning completion rule, this is not a clean completion/merge gate. Initial broken Distrobox wrappers were bypassed by selecting the actual installed native toolchain, not by changing tests. The exact logs preserve both initial failures and final observations.

Board's final supported offline import was read through an actual compiled transient HTTP server in a private network namespace. The server was terminated after reads. [board_api_readback.json](evidence/operational_proof/board_api_readback.json) shows the noncompleted mission, passing and failing verification, absent authority envelope and REQUIRE_APPROVAL. Its timeline carries the full partial/native/release blocker explanation. This establishes API/display-data projection, not browser rendering or transactional durability.
