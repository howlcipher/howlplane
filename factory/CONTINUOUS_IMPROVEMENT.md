# Continuous Improvement Campaign — Master Operating Contract

**Campaign ID:** `2026-09-27-continuous-improvement`

**Owner:** HowlPlane control plane

**Scope:** Four-repository portfolio: `howlcipher/howl`, `howlcipher/howlplane`,
`howlcipher/howlframe`, and `howlcipher/grocery-optimizer`.

---

## 1. Objective

Continuously develop **Grocery Optimizer** (`howlcipher/grocery-optimizer`)
while improving the Howl ecosystem, HowlPlane, and HowlFrame when evidence
shows platform work has greater value or is blocking product delivery.

The system repeatedly:

```
OBSERVE → DISCOVER → RECONCILE → PRIORITIZE → SELECT ONE JUSTIFIED WORK ITEM
→ EXECUTE THROUGH THE CORRECT REPOSITORY → VERIFY → INDEPENDENTLY AUDIT
→ REMEDIATE → ACCEPT → UPDATE EVIDENCE → REASSESS THE FOUR-REPOSITORY PORTFOLIO
→ CONTINUE
```

`IDLE` is a valid outcome. **"No valuable work"** is a valid result. The
Factory must never invent work simply to remain busy.

---

## 2. Product vs. Platform

| Repository | Role | Classification |
| :--- | :--- | :--- |
| `howlcipher/grocery-optimizer` | Product — consumer application | **Primary product** |
| `howlcipher/howl` | Ecosystem / integration | Platform |
| `howlcipher/howlplane` | Control plane / orchestration | Platform |
| `howlcipher/howlframe` | Governance / execution boundary | Platform |

Grocery Optimizer is the product. Howl, HowlPlane, and HowlFrame are the
engineering platform used to build and maintain it.

---

## 3. When platform work is justified

Platform self-improvement is justified only when evidence supports one or more
of the following:

1. A **P0** or **P1** issue exists in a platform repository.
2. A platform defect **blocks product development** in Grocery Optimizer.
3. Reliability evidence shows **repeated failure** in platform machinery.
4. A **provider, orchestration, or governance failure** materially reduces
   autonomous throughput.
5. **Cross-project contract problems** impede safe execution.
6. A **P2** issue has significant recurring operational cost.

Product work should normally continue when the platform is sufficiently
capable.

**Cosmetic platform cleanup must not starve product delivery.**

---

## 4. Prioritization policy

Evaluate candidate work across all four repositories using the same primary
ordering:

A. **Safety and integrity** — P0 first.
B. **Broken critical capability** — P1 next.
C. **Blocking relationships** — a P2 that blocks multiple important tasks may
   outrank an isolated P1 only when dependency evidence clearly supports the
   decision. Record the dependency explicitly.
D. **Product progress** — when platform capability is sufficient, advance
   Grocery Optimizer.
E. **Reliability / maturity** — address recurring P2 platform problems that
   materially affect autonomous operation.
F. **P3 work** — batch related P3 work only when worthwhile.
G. **P4** — never automatically consume substantial capacity on optional polish
   while higher-value work exists.

Do not use fake numerical precision. Every selection must be recorded together
with the evidence that justified it.

---

## 5. Finding prefixes

| Repository | Prefix |
| :--- | :--- |
| Howl ecosystem | `HE-*` |
| HowlPlane | `HP-*` |
| HowlFrame | `HF-*` |
| Grocery Optimizer | `GO-*` |

Cross-repository duplicates are reconciled by root cause. The same defect must
not be counted four times merely because four components observed it.

---

## 6. Mission integration

Each repository has version-controlled mission specifications:

| Repository | Mission pattern |
| :--- | :--- |
| `howlcipher/howl` | `docs/DOGFOOD_MISSION_NNN.md` |
| `howlcipher/howlplane` | `docs/DOGFOOD_MISSION_NNN.md` |
| `howlcipher/howlframe` | `docs/DOGFOOD_MISSION_NNN.md` |
| `howlcipher/grocery-optimizer` | `docs/MISSION_NNN.md` |

A completed mission may generate its successor, but **the master Factory decides
when the successor executes**. Mission `N` must never directly launch Mission
`N+1`. The supervisor consumes the completion evidence, consumes structured
findings, notices the `NEXT_MISSION` declaration, updates portfolio knowledge,
and compares the successor against ready work in all four repositories before
scheduling it.

---

## 7. Anti-thrash rules

Prefer completing a coherent bounded mission before switching repositories
unless:

- a P0 emerges,
- the current mission becomes blocked,
- continuing would waste significant resources,
- an authority boundary requires stopping, or
- a dependency must be repaired elsewhere.

Do not bounce between repositories because two tasks have similar priority.

---

## 8. Anti-self-obsession rule

The Howl platform exists partly to deliver useful work. Do not allow continuous
self-refactoring to become the objective. Use the existing Factory portfolio
policy and anti-starvation mechanisms rather than inventing a new scoring
system.

---

## 9. Failure / blocking handling

When a work item cannot continue because of authority, credentials, external API
access, provider exhaustion, human decision, destructive operation, or an
unresolved cross-repo dependency, **park that work with clear evidence**.
Continue with another justified item if possible. Do not turn one blocked
repository into a global stop unless it truly blocks the entire campaign.

---

## 10. Authority boundaries

Preserve HowlPlane and HowlFrame authority boundaries. The master objective does
not authorize unlimited mutation. Never weaken:

- human-only boundaries,
- credential requirements,
- destructive-operation safeguards,
- approval semantics, or
- repository ownership.

Do not fabricate retailer credentials. Do not bypass external service
requirements.

---

## 11. Continuous operation

The Factory should continue while justified executable work exists, or while a
temporary resource condition is expected to resolve and the existing supervisor
can safely back off and recheck.

The Factory should idle or back off when nothing valuable is executable.

Surface `OWNER_REQUIRED` when human authority or input is truly required.

---

## 12. Outcome measurement

Track useful campaign-level evidence when existing Factory capabilities permit:

- missions completed per repository,
- findings opened / resolved,
- P0/P1 age,
- blocked work,
- manual interventions,
- provider failures,
- resumptions,
- retries,
- verification failures,
- independent audit findings,
- repeated root causes,
- time spent on product work vs. platform work, and
- repository dispatch distribution.

Avoid claiming effectiveness metrics that the implementation cannot actually
measure.

---

## 13. Factory commands

All commands run from the `howlplane` repository. The installed HowlPlane CLI
is available as both `howlplane` and `howl`; the canonical command is
`howlplane`.

### Start the continuous campaign

This command uses `--target ecosystem` and the workspace defined in
`factory/howl-workspace.yaml`. It does **not** pass `--max-work-items`, so the
campaign runs continuously until stopped or idled.

```bash
howlplane factory start \
  --target ecosystem \
  --workspace factory/howl-workspace.yaml \
  --product-repo howlcipher/grocery-optimizer \
  --objective "Continuously develop Grocery Optimizer and improve the Howl platform based on verified evidence, always selecting the highest-value justified executable work across the four repositories." \
  --authority standard
```

### Status

```bash
howlplane factory status
```

JSON output:

```bash
howlplane factory status --json
```

### Logs

```bash
howlplane factory logs
howlplane factory logs --lines 200
howlplane factory logs --follow
```

### Stop

```bash
howlplane factory stop
```

### Resume

```bash
howlplane factory resume
```

### Owner reauthorization

If the authority envelope expires, the Factory surfaces `OWNER_REQUIRED`. To
reauthorize:

1. Confirm the active campaign and workspace:

   ```bash
   howlplane factory status
   ```

2. Reauthorize with the desired profile:

   ```bash
   howlplane factory start \
     --target ecosystem \
     --workspace factory/howl-workspace.yaml \
     --product-repo howlcipher/grocery-optimizer \
     --authority standard \
     --authority-profile overnight-safe
   ```

   Or use a bounded command such as `howlplane factory run-once` with the same
   `--authority-profile` to re-bind the envelope for a single tick without
   launching an unbounded campaign.

---

## 14. Launch prerequisites

Before starting the campaign, verify:

1. `factory/howl-workspace.yaml` parses with the current Workspace schema.
2. All four repositories exist at the declared absolute paths.
3. Each repository identity matches the declared `repository` slug and Git
   remote.
4. Each repository contains its mission file:
   - `howl/docs/DOGFOOD_MISSION_001.md`
   - `howlplane/documentation/DOGFOOD_MISSION_001.md`
   - `howlframe/docs/DOGFOOD_MISSION_001.md`
   - `grocery-optimizer/docs/MISSION_001.md`
5. `howlcipher/grocery-optimizer` is configured as the product repository via
   `--product-repo` and the workspace role `product`.
6. No mission was executed as part of this setup.
7. The Factory was not started.
8. No unintended repository code was modified.

---

## 15. Success criteria for setup

This setup is complete when the above commands are documented, the workspace and
operating contract are version-controlled in `howlplane`, and a human can start
the campaign with one verified command.
