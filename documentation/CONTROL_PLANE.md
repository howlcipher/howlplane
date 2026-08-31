# HowlPlane — Multi-Agent Engineering Control Plane

**Repository:** `howlcipher/howlplane`  
**Status:** Operational / Maintenance Mode (Architectural Freeze)  
**Architecture Principle:**  
> *AI proposes and performs work.*  
> *Policies constrain actions.*  
> *Independent reviewers challenge assumptions.*  
> *Deterministic tools verify claims.*  
> *Evidence records historical truth.*  
> *Humans authorize meaningful risk.*

---

## 1. Operating Mode & Governance

### 1.1 Architectural Freeze Directives
The control plane architecture is **FROZEN**. It is infrastructure now (treated like CI, Git, or a test harness).
New framework capabilities are forbidden unless real operational portfolio usage exposes a correctness, security, verification, authority, or severe usability defect.

The default rule for any feature proposal:
- *"Is X blocking real engineering work?"* If no: **DO NOT BUILD IT**.

### 1.1.1 Named Carve-Out: Persistent Operation

The freeze above stands unchanged for every capability except the one named
here. Persistent operation is carved out because real operational portfolio
usage has already exposed the defects the freeze itself asks for as the price of
admission. The evidence, measured from durable state in this repository on
2026-08-30:

| Evidence | Measurement | Defect class |
| :--- | :--- | :--- |
| Campaigns terminated because the provider pool degraded | **231 of 722** (32%) recorded `stop_reason: all_providers_exhausted` | Severe usability — a third of all runs stop for a condition that resolves itself on a cooldown nothing is alive to wait for |
| Parked tasks ever surfaced to the operator | **0 of 722** campaigns contain a `ParkedTaskRecord` | Authority — the park-and-continue path that `OVERNIGHT_SAFE_ALLOWED_ACTIONS` grants has never once fired in production |
| Evidence ledger | `logs/control_plane/evidence_ledger.jsonl` at 139,870,255 bytes / 147,806 lines, no index, no rotation, `list_all_entries()` reads the whole file | Severe usability — every operational question costs a full-file scan |
| Work discovery | `discover_observations()` is reachable only from two test modules; `issues.md` / `improvements.md` are entirely hand-authored | Verification — the control plane cannot observe its own recurring failures |
| Durable operator memory | `documentation/agent_memory/` contains only its own init file | Severe usability |

**Scope of the carve-out.** Capability that gives the control plane a lifetime
longer than one CLI invocation, and the state required to make that lifetime
honest: a supervisory loop, a durable work portfolio, evidence-backed discovery,
blocker resolution, an operator-attention inbox, idle and backoff, and a derived
index over existing evidence.

**What the carve-out does not license.** It grants no new authority mechanism —
`AuthorityProfile`, `AuthorityEnvelope` and `HumanBoundaryGate` remain the only
authority paths, and `NEVER_DELEGATABLE_BOUNDARIES` is untouched. It does not
license concurrency, a package ecosystem, a learned prioritization model, or any
weakening of deterministic verification, reviewer independence, or the hygiene
ceilings.

**Standing obligation.** Every pull request landing under this carve-out must
cite the operational evidence that motivates it, in the same measured form as
the table above. A proposal that cannot produce such evidence falls back to the
default rule in §1.1 and is rejected. The carve-out narrows the freeze; it does
not suspend it.

See `documentation/adr/0006_persistent_factory_supervisor.md`.

### 1.2 Maintenance Priority Triage
Future control-plane issues and maintenance requests are classified strictly into:

| Tier | Classification | Action Policy |
| :--- | :--- | :--- |
| **P0** | Authority, security, or correctness failure | Fix immediately |
| **P1** | Deterministic verification failure | Fix promptly |
| **P2** | Repeated operational friction | Require repeated evidence across multiple tasks before changing architecture |
| **P3** | Optimization | Backlog only |
| **P4** | Speculative feature | Reject unless future evidence changes the need |

---

## 2. What This Is and What It Is Not

### What This Is
- A **local-first engineering control plane** for coordinating CLI coding agents (Claude Code, Codex, Gemini CLI, Devin CLI, Antigravity CLI / agy, Local Ollama, and future tools).
- A **deterministic lifecycle orchestrator** with explicit state transitions, reproducible routing, independent adversarial reviews, evidence collection, and fail-closed human authority gates.
- A **shared policy and verification harness** separating orchestration rules from project-specific local truth.

### What This Is Not
- **Not an autonomous software company simulation:** Does not pretend LLM agents possess infallible judgment or replace human engineering authority.
- **Not another coding agent or LLM wrapper:** Does not compete with Claude Code, Gemini CLI, Codex, or Devin CLI; it coordinates them.
- **Not a heavyweight enterprise platform:** Requires zero cloud infrastructure, no Kubernetes, no Redis, no background daemons, and zero paid API calls to route. It runs directly in the developer's terminal.

---

## 2. Core Architectural Workflow

```text
Human Objective / Backlog Item
              ↓
    Task Specification (TaskSpec)
              ↓
    Deterministic Task Router
    ├── Agent Type Selection
    └── Independent Reviewer Role Selection
              ↓
    Implementation Delegate (or local agent)
              ↓
    Specialized Independent Reviewers (Falsification Briefs)
    ├── correctness-reviewer
    ├── regression-reviewer
    ├── security-reviewer
    ├── test-falsifier
    ├── architecture-reviewer
    └── simplicity-reviewer
              ↓
    Review Reconciliation Engine
    (confirmed | likely | disputed | false_positive | out_of_scope | requires_human)
              ↓
    Remediation
              ↓
    Deterministic Verification Plan
    (claimed → tested → observed → verified)
              ↓
    Durable Evidence Ledger (Scrubbed / Redacted)
              ↓
    Human Authority Gate (AWAITING_HUMAN decision packet if boundary triggered)
              ↓
    Ship / Complete
```

---

## 3. Control Plane Capabilities

### 3.1 Task Specification (`TaskSpec`)
Represents an engineering task as a canonical, machine-readable object conforming to JSON Schema Draft 2020-12 (`schemas/task-spec.schema.json`).

**Explicit Lifecycle States:**
- `discovered`
- `planned`
- `implementing`
- `reviewing`
- `remediating`
- `verifying`
- `awaiting_human`
- `complete`
- `failed`
- `blocked`

State transitions are strictly enforced via `spec.transition_to(new_state)`. Illegal transitions (e.g. jumping from `discovered` directly to `complete`) raise an `InvalidStateTransitionError`.

### 3.2 Agent Capability Registry (`AgentRegistry`)
A declarative registry describing available agent types without hardcoding ephemeral model names.

Fields:
- `agent_id`: Identifier (e.g. `claude_code`, `codex`, `gemini_cli`, `devin_cli`, `agy`, `local_ollama`)
- `provider`: `anthropic`, `google`, `openai`, `cognition`, `local`
- `interface`: `cli`, `headless_cli`, `api`, `ide`
- `capabilities`: `[code_generation, file_editing, code_review, terminal_execution, git_operations, architectural_reasoning, deep_debugging, autonomous_workflow]`
- `reasoning_tier`: `tier_1`, `tier_2`, `tier_3`
- `cost_class`: `free_local`, `subscription_included`, `paid_api`
- `availability`: `available`, `degraded`, `unavailable`

### 3.3 Deterministic Task Router (`TaskRouter`)
Matches task requirements against agent capabilities without making remote LLM calls:
- High-risk, security, or architectural tasks (`tier_1`) route to `claude_code` or `devin_cli`.
- Standard implementation tasks (`tier_2`) route to `agy`, `codex`, or `gemini_cli`.
- Low-risk, local-only helper subtasks (`tier_3`) route to `local_ollama`.
- Explicit human overrides (`preferred_agent`) are honored immediately and recorded.
- Automatically selects specialized reviewer roles based on domain and risk.

Final provider selection belongs to the shared configurable resource pool, not
the router's legacy static list. The router supplies an advisory recommendation
only among candidates already admitted by operator permission, egress,
readiness, role capability, capacity, and economic policy. See
[Configurable AI Resource Pool](AI_RESOURCE_POOL.md).

### 3.4 Specialized Independent Reviewer Roles
Reviewers are instructed explicitly to **challenge assumptions and falsify correctness**:
1. **`correctness-reviewer`**: Identifies logic bugs, unhandled boundary cases, and contract mismatches.
2. **`regression-reviewer`**: Identifies breaking changes to call sites, signatures, and configuration drift.
3. **`security-reviewer`**: Audits trust boundaries, credential leaks, injection vectors, and authorization.
4. **`test-falsifier`**: Searches for vacuous assertions, inaccurate mocks, and missing negative test branches.
5. **`architecture-reviewer`**: Flags unnecessary coupling, leaky abstractions, and structural violations.
6. **`simplicity-reviewer`**: Searches for opportunities to solve the problem with a smaller diff and less code.

### 3.5 Review Reconciliation Engine (`ReviewReconciler`)
Synthesizes findings across multiple reviewer roles:
- **`confirmed`**: Findings with multi-reviewer agreement or verified proof.
- **`likely`**: High-confidence findings from domain reviewers.
- **`disputed`**: Conflicting evaluations preserved for explicit resolution.
- **`requires_human_judgment`**: Security or policy disputes routed to human authority.
- **`false_positive` / `out_of_scope`**: **Strict Rule:** Any dismissal of a `blocker` or `high` finding requires an explicit, non-empty `resolution_reason`. Silent dismissal is forbidden.

### 3.6 Deterministic Verification Plan (`VerificationPlan`)
Separates review claims from verifiable command outcomes:
- **Status lifecycle:** `claimed` → `tested` → `observed` → `verified` (or `failed`).
- Executes build, lint, repository hygiene, unit tests, integration tests, and security scans via deterministic subprocess calls (no `shell=True`).
- **Repository Hygiene Gate (`slopslint`):** Runs deterministic repository hygiene gates enforcing monotonic duplication ceilings (`slopslint check --classify --enforce`), ceiling ratchets (`slopslint ratchet <base-ref>`), stale tombstone elimination, and provider integrity verification. Policy modifications are semantically classified (`TIGHTENING`, `NEUTRAL`, `WEAKENING`, `DEBT_ACCEPTANCE`, `HARD_REJECT`, `UNKNOWN`), enabling autonomous quality improvements (ceiling decreases, stale tombstone deletions) while gating debt acceptance and policy weakenings behind human authority.

### 3.7 Durable Evidence Ledger (`EvidenceLedger`)
Append-only log (`logs/control_plane/evidence_ledger.jsonl`) recording actions, commands, results, findings, and verification outcomes.
- **Automated redaction:** Automatically scrubs API keys (`sk-...`, `ghp_...`), passwords, authorization headers, private keys, and email addresses before recording.

### 3.8 Transparent Performance Metrics (`MetricsCalculator`)
Calculates real engineering statistics from evidence history without subjective guessing:
- First-pass success rate
- Task completion and abandonment rates
- Total review findings and blocker counts
- Verification failure counts
- Rework cycle counts
- Repository hygiene checks, regressions caught, ceilings lowered, ceiling raise attempts blocked, and debt acceptance tracking
- Per-agent performance breakdowns

### 3.9 Project Adapters (`ProjectAdapter`)
Establishes a clean architectural boundary:
- **Control plane** supplies orchestration rules, reviewer roles, and verification schemas.
- **Project repository** supplies local truth: `.ai-project.toml`, `project_manifest.yaml`, `AGENTS.md`, `.slop/config.yml`, `.slop/ceilings.yml`, local skills, and test/build commands.

### 3.10 Human Authority Boundaries (`HumanBoundaryGate`)
Enforces strict human sign-off on high-risk operations:
- Production deployments
- Infrastructure apply (`terraform apply`, `kubectl apply`)
- Destructive database migrations (`DROP TABLE`, truncate)
- Credential provisioning and rotations
- Paid service consumption
- External communications (emails, webhooks)
- Package publishing
- Submitting applications or resumes
- Accepting debt tombstones or repointing existing debt (`slop_debt_acceptance`) — **Invariant:** AI agents cannot self-accept slop debt. Proposing a new tombstone or repointing a fingerprint pauses at `AWAITING_HUMAN`.
- Weakening repository hygiene scan scope, ignore rules, or detector parameters (`hygiene_policy_weakening`).
- Prohibited ceiling inflation or configuration deletion (`hygiene_policy_violation`) — **Invariant:** Ceilings cannot be increased without explicit human policy override; ceiling raises are rejected by default.

When triggered, the task enters `awaiting_human` and produces a structured **Decision Packet**:
```markdown
# 🛑 Human Authority Decision Packet: Task `<id>`
- Objective
- Boundary Triggers
- Proposed Change Summary
- Verification Status
- Key Evidence
- Identified Risks
- Recommended Action
```

### 3.11 Knowledge & Skills Layer Subsystem
The former `ai_knowledge_library` now operates as the context, policy, and capability subsystem within HowlPlane:
- **`AGENTS.md`**: Canonical global engineering context and grounding protocol loaded across all agents.
- **Rules (`.agents/rules/`)**: Anti-manipulation, prompt sanitization, and architectural constraints.
- **Skills (`.agents/skills/`)**: Tiered domain skills providing deterministic workflows and references.
- **Prompts (`.agents/prompts/`)**: Canonical multi-agent prompt library.
- **Grounding Profile (`USER_PROFILE.md`)**: Local user profile grounding personal context and career materials.

### 3.12 HowlFrame Architectural Relationship & Dogfooding Position
HowlPlane serves as a **primary real-world dogfooding consumer** of the HowlFrame toolchain:
- **What HowlFrame is:** An AI-native programming language and capability-bounded execution runtime.
- **Architectural role:**
  ```text
  HowlPlane (AI Engineering Control Plane)
        |
        +-- normal deterministic tooling (Go, Python, Make, Git)
        |
        +-- AI coding agents (Claude Code, Codex, Gemini CLI, Devin CLI, Antigravity)
        |
        +-- HowlFrame bounded execution runtime (shadow verification / dogfooding)
  ```
- **Why HowlPlane dogfoods HowlFrame:** Smaller applications prove individual language features; HowlChangeOps proves governed consequential change execution; HowlPlane provides high-frequency real AI engineering workloads that pressure generated structured programs, capability boundaries, malformed AI output, instruction budgets, partial failures, result normalization, and structured evidence.
- **Independence & Fail-Closed Isolation:** HowlPlane remains completely functional without HowlFrame. HowlFrame is an optional runtime dependency for selected bounded tasks. If the HowlFrame binary is unavailable, crashes, times out, or exceeds budget, HowlPlane records the diagnostic failure in the evidence ledger and continues normal operation without altering any routing, human authority, or verification decisions.
- **First Dogfooding Slice — Project Context Audit (`SHADOW MODE`):**
  - **Program Artifact:** `integrations/howlframe/project_context_audit.howl` (compiled to `project_context_audit.hfbc`).
  - **Input Contract:** `howlplane.project_context/v1` (normalized subset of `ProjectContext`).
  - **Audit Contract:** `howlplane.project_context_audit/v1` (evaluating project types, AGENTS.md presence, verification surface counts, and hygiene policy status).
  - **Runner Boundary:** `HowlFrameAuditRunner` invokes the fixed bytecode with finite instruction budget (`--max-instructions 100000`), zero capabilities granted (`CapNone`), no shell execution (`shell=False`), and finite process timeout.
  - **Configuration:** `HOWLPLANE_HOWLFRAME_DOGFOOD=shadow` (or `~/.config/howlplane/config.toml` `[dogfood] howlframe = "shadow"`).
  - **Disagreement Model:** Compares local `ProjectAdapter` facts against HowlFrame observed truth (`MATCH`, `MISMATCH`, `HOWLFRAME_FAILURE`, `HOWLFRAME_UNAVAILABLE`, `INVALID_OUTPUT`, `BUDGET_EXCEEDED`, `TIMEOUT`).
  - **Evidence:** Audit results recorded durably into `logs/control_plane/evidence_ledger.jsonl`.
  - **CLI Surface:** `ai status` and `ai howlframe-audit` display dogfood execution results and findings.

---

## 4. CLI & Prompt UX

### CLI Subcommands
```bash
# Initialize a new task specification
python -m src.control_plane init-task --task-id TASK-101 --objective "Implement feature" --risk medium

# Route task to appropriate agent and reviewers
python -m src.control_plane route-task --task-file task_TASK-101.yaml

# Generate independent review briefs for a diff
python -m src.control_plane briefs --task-file task_TASK-101.yaml --diff-file change.diff

# Reconcile multi-agent review findings
python -m src.control_plane reconcile --findings-file findings.yaml --output report.md

# Run deterministic project verification
python -m src.control_plane verify --project-dir . --task-id TASK-101

# Record evidence to the append-only ledger
python -m src.control_plane record --task-id TASK-101 --agent-id agy --action verification_executed --result passed

# View transparent historical metrics
python -m src.control_plane metrics

# Check human authority boundaries
python -m src.control_plane check-boundary --task-file task_TASK-101.yaml --actions "terraform apply"
```

### High-Level Canonical Prompts
- `/work_next_item`: Full end-to-end backlog item orchestration.
- `/route_task`: Deterministic task classification and routing.
- `/review_change`: Specialized independent reviewer falsification.
- `/reconcile_reviews`: Finding reconciliation preserving disagreements.
- `/verify_change`: Deterministic verification execution.
- `/ship_check`: Pre-ship verification and human boundary evaluation.

---

## 5. Worked Example: Career Agent Core

### 1. Specification (`TaskSpec`)
```yaml
schema: ai.task_spec/v1
task_id: CAREER-042
repository: Career_Agent_Core
objective: Add ATS keyword density analysis to resume parser
acceptance_criteria:
  - Calculate frequency of job posting keywords in parsed resume sections
  - Return missing high-frequency keywords
  - 100% test coverage on scoring edge cases
constraints:
  - Zero external cloud API calls
  - Must run within existing Go pipeline
risk_level: medium
required_skills:
  - software_development
  - career_assistant
recommended_reasoning_tier: tier_2
current_state: discovered
```

### 2. Routing (`python -m src.control_plane route-task`)
- **Selected Agent:** `agy` (Antigravity CLI / Tier 2)
- **Rationale:** Balanced Go implementation; delegates code edits to headless CLI.
- **Reviewers:** `correctness-reviewer`, `test-falsifier`, `regression-reviewer`, `simplicity-reviewer`.

### 3. Implementation
The delegate applies changes to `internal/parser/keyword_density.go` and `internal/parser/keyword_density_test.go`.

### 4. Independent Review & Falsification
- `correctness-reviewer` checks boundary handling (empty keyword lists, duplicate tokens).
- `test-falsifier` discovers that the test suite does not check case-insensitive keyword collisions.
- Finding `F001` recorded: High severity test gap.

### 5. Reconciliation (`python -m src.control_plane reconcile`)
- `F001` classified as `likely` finding.
- Implementation delegate adds case-insensitive collision test and normalizes tokens.
- Finding `F001` marked `confirmed` and `fixed`.

### 6. Verification (`python -m src.control_plane verify`)
- `go test ./...` → `verified` (exit 0)
- `go build ./...` → `verified` (exit 0)

### 7. Evidence Recording & Human Gate Check
- Evidence entry recorded in ledger.
- `HumanBoundaryGate.evaluate` checks actions → No boundary triggered (`low` risk internal library).
- Task transitions: `verifying` → `complete`.

---

## 6. Global Command Launcher (`ai`)

The `ai` command is a thin, deterministic global entrypoint into the Multi-Agent Engineering Control Plane. It allows developers to operate from inside **any** project repository without manually loading shared rules or specifying library paths.

### 6.1 Installation & Configuration
Install the global launcher to `~/.local/bin/ai`:
```bash
# Run global installer from HowlPlane
bash scripts/install_global.sh
```
Or configure user configuration at `~/.config/howlplane/config.toml` (canonical) or `~/.config/ai-control-plane/config.toml` (legacy):
```toml
[control_plane]
path = "/path/to/howlplane"
```

Resolution precedence for the control plane:
1. `--control-plane-dir <path>` CLI argument
2. `HOWLPLANE_HOME` or `HOWLPLANE_DIR` environment variable
3. `AI_KNOWLEDGE_LIBRARY` environment variable (deprecated fallback)
4. `~/.config/howlplane/config.toml` -> `~/.config/ai-control-plane/config.toml` -> `~/.config/ai/config.toml`
5. Relative installation repository detection
6. Fail closed (`ERROR: configured HowlPlane control plane not found`)

### 6.2 Normal Operator Workflow
```bash
# Stand inside any repository and work an objective:
cd ~/projects/howlchangeops
ai work "work the next highest-value backlog item"

cd ~/projects/howlframe
ai work "fix runtime memory limit correctness bug"

cd ~/projects/Career_Agent_Core
ai work "fix issue 552"

# Inspect project status, verification suites, and active task runs:
ai status

# Deterministically route a task without creating artifacts:
ai route "patch authentication vulnerability"

# Inspect configured resources and current readiness/capacity:
ai providers
ai providers --json

# Reset only one resource's current capacity observation:
ai providers reset codex

# Run preflight diagnostics:
ai doctor

# Execute deterministic verification on the current project:
ai verify
```

### 6.2.1 Recovering a Held Task Lock

A task run holds `.task_runs/<task-id>/.task.lock` while it works. `ai status`
reports the owner's state, and what you do next depends on which of three
things can actually be established about it:

| Owner state | What it means | What to do |
| --- | --- | --- |
| `ACTIVE` | The owning process is running on this host. | Wait, or `ai cancel <task-id>`. The lock is never taken from a live owner. |
| `STALE` | The owner is provably gone (`ESRCH`, or its PID was recycled). | Nothing. The next `ai resume` reclaims it automatically. |
| `AMBIGUOUS` | Liveness cannot be established here — the lock was written on another host, or the PID belongs to another user. | `ai unlock <task-id>`, then `ai resume <task-id>`. |

```bash
# Reclaim a lock whose owner is gone or cannot be verified:
ai unlock HOWLFRAM-EXAMPLE-01
ai resume HOWLFRAM-EXAMPLE-01
```

`ai unlock` is the only takeover path and is deliberately a human action. It
refuses an `ACTIVE` lock outright and records every reclamation in the
evidence ledger as `stale_lock_reclaimed`. `ai resume` never steals a lock.

### 6.3 Direct Provider Escape Hatches
Direct vendor commands remain available as ungoverned escape hatches when full multi-agent orchestration is not desired:
```bash
claude
codex
agy
```
`ai work` remains the governed, fail-closed default.
