<div align="center">
  <h1>HowlPlane</h1>
  <p><strong>An AI Engineering Control Plane</strong></p>
  <p><a href="https://howlcipher.github.io/howlplane/"><strong>[ Documentation & Interactive System Console: howlcipher.github.io/howlplane ]</strong></a></p>
  <p>
    <img src="https://img.shields.io/static/v1?label=HowlPlane&message=Active&color=4a5aa8&style=for_the_badge" alt="HowlPlane Badge" />
    <img src="https://img.shields.io/static/v1?label=Skills&message=41&color=39ff14&style=for_the_badge" alt="Skills Badge" />
  </p>
  <p>
    <img src="https://img.shields.io/static/v1?label=Runtime&message=Python&color=3776AB&style=flat_square&logo=python" alt="Python Badge" />
    <img src="https://img.shields.io/static/v1?label=Binary&message=Go&color=00ADD8&style=flat_square&logo=go" alt="Go Badge" />
    <img src="https://img.shields.io/static/v1?label=Container&message=Docker&color=2496ED&style=flat_square&logo=docker" alt="Docker Badge" />
    <img src="https://img.shields.io/static/v1?label=Env&message=Linux&color=FCC624&style=flat_square&logo=linux" alt="Linux Badge" />
  </p>
  <p>
    <a href="https://github.com/howlcipher/howlplane/actions/workflows/release_installer.yml"><img src="https://github.com/howlcipher/howlplane/actions/workflows/release_installer.yml/badge.svg" alt="Release Installer" /></a>
    <a href="https://github.com/howlcipher/howlplane/actions/workflows/test.yml"><img src="https://github.com/howlcipher/howlplane/actions/workflows/test.yml/badge.svg" alt="Tests" /></a>
    <a href="https://github.com/howlcipher/howlplane/actions/workflows/docs.yml"><img src="https://github.com/howlcipher/howlplane/actions/workflows/docs.yml/badge.svg" alt="Docs" /></a>
  </p>
</div>

***

> **HowlPlane** coordinates AI-assisted engineering work across heterogeneous repositories through shared context, deterministic task routing, independent adversarial reviews, repeatable verification, durable evidence, and human-controlled authority boundaries.

### Core Directives
> - *AI proposes and implements work.*
> - *Policies constrain actions.*
> - *Independent reviewers challenge assumptions.*
> - *Deterministic tools verify claims.*
> - *Evidence records historical truth.*
> - *Humans authorize consequential risk.*

***

## Everyday Workflow

The primary entry point is the global `ai` command. Run it from inside any project repository on your machine:

```bash
# Stand inside any repository and prepare a governed task run (plan & dry-run):
cd /path/to/project
ai work "fix the highest-value open bug"

# Genuinely execute the complete closed-loop AI engineering lifecycle:
ai work "fix the highest-value open bug" --execute

# Force a specific implementation agent (e.g. claude_code, codex, gemini_cli, agy, devin_cli):
ai work "refactor database adapter" --agent codex --execute

# Inspect active project context, verification suites, and task runs:
ai status

# Deterministically route a task and select reviewer roles without mutating code:
ai route "patch authentication vulnerability"

# Run system and toolchain preflight diagnostics:
ai doctor

# Execute deterministic verification suites on the active repository:
ai verify
```

Direct vendor commands (`claude`, `codex`, `agy`) remain available as escape hatches when full multi-agent orchestration is not desired; `ai work` remains the governed default.

---

## Closed-Loop Operating Model

HowlPlane mechanically enforces every stage of the engineering lifecycle. An agent claiming "Done." has no authority to move a task to `complete`; `complete` must be earned through clean git deltas, independent review falsification, remediation, deterministic verification, and human authority authorization:

```text
Human Objective / Backlog Item
              ↓
  Stage 1: Discovery & Context Audit (discovered)
  ├── Project stack discovery (.ai-project.toml, project_manifest.yaml, stack markers)
  └── HowlFrame capability-bounded shadow context audit (read-only dogfooding)
              ↓
  Stage 2: Deterministic Planning & Routing (planned)
  ├── Agent Selection (Claude Code, Codex, Gemini CLI, Devin CLI, Antigravity)
  └── Specialized Reviewer Selection
              ↓
  Stage 3: Repository Baseline Isolation
  └── Captures commit SHA, modified files, and untracked files prior to agent launch
              ↓
  Stage 4: Implementation Agent Launch (implementing)
  └── Subprocess execution with live stdio streaming and timeout enforcement
              ↓
  Stage 5: Task-Attributable Delta Capture
  └── Isolates newly created/modified diffs from pre-existing repository dirt
              ↓
  Stage 6: Independent Adversarial Reviews (reviewing)
  ├── Specialized Reviewer Roles (Correctness, Regression, Security, Test Falsifier, etc.)
  ├── Strict structured findings parsing (YAML/JSON with fail-closed malformed handling)
  └── Review Reconciliation Engine (confirmed, likely, disputed, requires_human)
              ↓
  Stage 7: Autonomous Remediation & Re-Review Loop (remediating ↔ reviewing)
  ├── Targeted re-review dispatching only to reviewers with open findings
  └── Configurable remediation cycle limits
              ↓
  Stage 8: Deterministic Verification (verifying)
  └── Executes test suites, linters, and repository hygiene gates (slopslint)
              ↓
  Stage 9: Human Authority Boundary Gate (awaiting_human)
  └── Enforces explicit operator authorization for high-risk actions
              ↓
  Stage 10: Complete Evidence Ledger & Run Finalization (complete / awaiting_human / failed)
  └── Writes structured artifacts to .task_runs/<task_id>/ and durable ledger
```

---

## Core Capabilities

1. **Deterministic Task Routing (`src/control_plane/router.py`)**: Capability-based matching without remote LLM calls. Routes tasks by risk tier, reasoning requirements, and agent capability profiles.
2. **Specialized Independent Reviewers (`src/control_plane/reviewers.py`)**: Generates targeted falsification briefs designed to uncover subtle regressions, contract mismatches, security boundaries, and test coverage gaps rather than cheerleading code.
3. **Review Reconciliation Engine (`src/control_plane/reconciler.py`)**: Synthesizes multi-reviewer findings into structured categories (`confirmed`, `likely`, `disputed`, `requires_human`). Blocker and high-severity findings require explicit non-empty resolution reasons before dismissal.
4. **Deterministic Verification Suites (`src/control_plane/verification.py`)**: Executes build, test, lint, and repository hygiene gates (`slopslint`) through deterministic subprocess execution.
5. **Durable Evidence Ledger (`logs/control_plane/evidence_ledger.jsonl`)**: Append-only structured record of task lifecycles, review findings, and verification outcomes with automated credential and token redaction.
6. **Human Authority Boundaries (`src/control_plane/human_boundary.py`)**: Enforces explicit human authorization on consequential actions (production deployments, infrastructure modifications, database drops, package releases, and repository hygiene policy weakenings).
7. **Reasoning Strategy Dogfooding (`src/control_plane/reasoning/`, in development)**: Records redacted execution trajectories, immutable baseline and candidate experiment definitions, versioned strategy identities, deterministic comparisons, and evidence-linked observations. Milestone #60A remains experimental until staged crash recovery, the shared experiment runner, and live evidence verification are complete; it does not change model weights or authority envelopes.

---

## Operating Modes & Egress Governance

HowlPlane provides mechanically enforced runtime egress governance configured via `config/settings.yaml`:

- **`local_only` (Default):** Outbound network egress is blocked at the application runtime level. Hosted LLM provider dispatches are rejected in preflight, LangSmith telemetry is hard-disabled, and remote document sync operations are blocked. Local Ollama backends and local vector search operate fully offline.
- **`connected`:** Enables configured hosted providers (Anthropic, Google AI, OpenAI) and optional telemetry when explicitly authorized by the operator.

*Note:* Application-level runtime egress governance complements but is distinct from OS-level kernel network namespace sandboxing. For complete egress mappings, see the [Data Flows & Network Egress Reference](documentation/data_flows.md).

---

## Cross-Repository Operation

HowlPlane is designed to operate seamlessly across any codebase on your machine (Go, Python, TypeScript, Rust, C++, etc.). The control plane provides orchestration rules, reviewer roles, and verification schemas, while the target repository provides local truth: manifest configurations, local skills, and test/build commands.

---

## HowlFrame Architectural Relationship & Dogfooding Position

HowlPlane serves as a **primary real-world dogfooding consumer** of the [HowlFrame](https://github.com/howlcipher/howlframe) toolchain:

```text
HowlPlane (AI Engineering Control Plane)
      |
      +-- normal deterministic tooling (Go, Python, Make, Git)
      |
      +-- AI coding agents (Claude Code, Codex, Gemini CLI, Devin CLI, Antigravity)
      |
      +-- HowlFrame bounded execution runtime (future constrained / policy execution paths)
```

- **What HowlFrame is:** An AI-native programming language and capability-bounded execution runtime.
- **Why HowlPlane dogfoods HowlFrame:** Smaller applications prove individual language features; HowlChangeOps proves governed consequential change execution; HowlPlane provides high-frequency real AI engineering workloads that pressure generated structured programs, capability boundaries, malformed AI output, instruction budgets, partial failures, result normalization, and structured evidence.
- **Independence:** HowlPlane remains completely usable without HowlFrame. HowlFrame is an optional runtime dependency for selected bounded tasks.

---

## Knowledge & Skills Layer Subsystem

The repository's shared context layer operates as an integrated subsystem within HowlPlane:

- **`AGENTS.md`**: Canonical global engineering context and grounding protocol loaded natively across agents.
- **Rules (`.agents/rules/`)**: Anti-manipulation, prompt sanitization, and safety constraints.
- **Skills (`.agents/skills/`)**: 40 domain skills covering software engineering, quality assurance, defensive security, database management, and systems logic.
- **Prompts (`.agents/prompts/`)**: Canonical multi-agent prompt library (`work_next_item`, `route_task`, `review_change`, `reconcile_reviews`, `verify_change`, `ship_check`).
- **Grounding Profile (`USER_PROFILE.md`)**: Local user profile grounding personal context and career materials.

---

## Global Installation & Setup

### Option 1: Standalone Binary Installer (Recommended)
1. Download the latest `ai_installer` executable (Linux, macOS, Windows, `.deb`, `.rpm`) from the **[GitHub Releases](https://github.com/howlcipher/howlplane/releases)** page.
2. Run the executable in your terminal:
   ```bash
   ./ai_installer
   ```
   The interactive installer links global rules to Gemini CLI / Antigravity, Claude Code, Codex, and Devin CLI, and sets up your environment.

### Option 2: Script-Based Setup
```bash
# Linux or macOS:
chmod +x scripts/install_global.sh
./scripts/install_global.sh

# Windows (PowerShell):
.\scripts\install_global.ps1
```

### Option 3: Python Development Install
```bash
pip install -e ".[dev]"
```

### Configuration & Discovery Precedence
The `ai` launcher locates the HowlPlane control plane using the following order:
1. `--control-plane-dir <path>` CLI flag
2. `HOWLPLANE_HOME` or `HOWLPLANE_DIR` environment variable
3. `AI_KNOWLEDGE_LIBRARY` environment variable (deprecated fallback)
4. `~/.config/howlplane/config.toml` (canonical) -> `~/.config/ai-control-plane/config.toml` -> `~/.config/ai/config.toml`
5. Self repository root detection

---

## Documentation Index

- [Control Plane Architecture](documentation/CONTROL_PLANE.md)
- [User Guide & Operator Reference](documentation/USER_GUIDE.md)
- [Local AI Worker (Ollama) & Bounded Dogfooding](documentation/LOCAL_MODEL.md)
- [Data Flows & Network Egress Reference](documentation/data_flows.md)
- [Coding Standards & Hygiene](documentation/coding_standards.md)
- [AI Framework Blueprint](documentation/AI_FRAMEWORK_BLUEPRINT.md)
- [Localizations & Languages](documentation/languages/README_en_US.md)
- [Change Log](change_log.md)
