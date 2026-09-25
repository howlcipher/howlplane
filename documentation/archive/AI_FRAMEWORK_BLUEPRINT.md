# AI Framework Blueprint

**Product:** HowlPlane (formerly `ai_knowledge_library`)  
**Repository:** `howlcipher/howlplane`  
**Status:** Target Architecture  
**Purpose:** Define the long-term architecture, ownership boundaries, migration path, and acceptance criteria for turning this repository into the central AI framework used across the operator's devices and projects.

---

## 1. Executive Summary

HowlPlane evolves from a collection of rules, skills, prompts, utilities, and experimental orchestration components into a portable **local AI engineering control plane**.

The framework should be installed once per device and become the default entry point for AI-assisted work. Individual repositories should not carry full copies of global prompts, provider routing, memory systems, retry logic, or agent commands. Instead, each project should contain a small portable manifest describing its local context, commands, skills, security requirements, and routing preferences.

The intended user flow is:

```text
User, project, script, or automation
                  |
                  v
        ai_knowledge_library
        ├── loads global policy and user context
        ├── identifies the current project
        ├── retrieves relevant knowledge
        ├── chooses skills and workflows
        ├── selects a healthy AI provider
        ├── enforces capabilities and approvals
        ├── executes, validates, and records the run
        └── produces reusable artifacts and handoffs
                  |
                  v
 Claude Code / Codex / Gemini / Ollama / MCP / Zero
```

The framework must provide one entry point, one policy system, one project contract, one provider gateway, one security boundary, one run-history format, and one portable installation workflow.

---

## 2. Architectural Decision

### 2.1 Source of truth

`ai_knowledge_library` is the source of truth for:

- Global AI rules and behavioral constraints.
- Reusable skills and task workflows.
- User and device profiles.
- Project adoption and manifest tooling.
- Provider routing and health management.
- Context indexing and retrieval.
- Security policy and capability enforcement.
- Run history, telemetry, artifacts, and handoffs.
- Cross-platform installation, upgrades, and diagnostics.
- Optional templates and tool packs.

### 2.2 Relationship to `ai_router`

The useful functionality in `ai_router` should become the framework's provider-routing and run-lifecycle runtime.

The target ownership is:

```text
ai_knowledge_library
├── policy and knowledge plane
├── project/context plane
├── security plane
├── workflow/orchestration plane
└── runtime/router plane       <- ai_router functionality
```

`ai_router` may remain temporarily independent during migration, but there must eventually be only one provider configuration, one provider health model, one run database, one artifact format, and one public CLI.

### 2.3 Relationship to `Zero`

`Zero` remains an independent programming-language and execution project.

The framework may consume these reusable concepts from Zero:

- Explicit capability declarations.
- Fail-closed execution.
- Structured errors.
- Constrained-generation schemas.
- Optional Zero execution as a provider-neutral backend.

The Zero compiler, parser, VM, bytecode, and language-specific implementation remain in the Zero repository.

---

## 3. Design Principles

### 3.1 Local-first

The framework must work without a hosted control service. Core state, history, configuration, and knowledge remain on the device unless the user explicitly configures a remote service.

### 3.2 Portable by default

A repository must be clonable onto Windows, macOS, Linux, WSL, immutable Linux systems, and development containers without committed machine-specific paths.

Absolute symlinks, local mount paths, generated credentials, device-specific provider state, and machine-specific configuration must not be committed.

### 3.3 Thin projects, rich framework

Projects should contain domain code and project-specific guidance. Reusable orchestration, global prompts, provider clients, retry behavior, security rules, and generic agent commands belong in the framework.

### 3.4 Explicit capabilities

AI execution must be constrained by declared capabilities. The default is deny unless a capability is granted by global policy, project policy, the selected mode, or explicit user approval.

### 3.5 Explainable routing

Provider selection must be deterministic enough to explain. Every route decision should state the task classification, candidate providers, health exclusions, historical signals, user overrides, and final selection.

### 3.6 Append-oriented auditability

Runs must preserve inputs, route decisions, attempts, normalized events, validation, patches, and handoffs in human-readable artifacts.

### 3.7 Graceful degradation

The minimal framework must remain useful without Docker, Ollama, a vector database, a browser, MCP servers, or background workers.

### 3.8 Stable contracts before broad extraction

Reusable code should move into the framework only after its contract is defined. Domain-specific code must not be centralized merely because two implementations look similar.

---

## 4. Target System Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                         Interfaces                          │
│ CLI │ MCP Server │ Local API │ SDKs │ TUI/Web │ Automation │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                     Request and Project Layer               │
│ project discovery │ manifest │ task schema │ mode │ profile │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                   Policy and Context Assembly               │
│ rules │ skills │ prompts │ user profile │ device │ RAG      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                    Security and Validation                  │
│ quarantine │ secrets │ PII │ capabilities │ workspace gate  │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                 Workflow and Provider Runtime               │
│ classify │ route │ health │ cooldown │ execute │ fallback   │
│ handoff │ review loops │ structured output │ tool calls      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                         Providers                           │
│ Claude Code │ Codex │ Gemini │ Ollama │ APIs │ MCP │ Zero   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────v──────────────────────────────┐
│                   State and Observability                   │
│ SQLite │ events │ artifacts │ telemetry │ ratings │ indexes │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. Ownership Boundaries

### 5.1 Framework-owned concerns

The framework owns:

- Provider discovery and health checks.
- Provider adapters and normalized output.
- Provider cooldowns and fallback.
- Task classification and routing policy.
- Run IDs, history, events, attempts, and artifacts.
- Handoff generation between providers.
- Global rules, skills, and reusable prompts.
- Global and project context assembly.
- Semantic indexing and retrieval contracts.
- Secret redaction and sensitive-data policy.
- Capability checks and approval requirements.
- Workspace safety and Git-state validation.
- Common validation and review gates.
- Cross-platform installation and update behavior.
- Project adoption, doctor, and diagnostics.
- Shared templates and optional tool packs.

### 5.2 Project-owned concerns

Projects own:

- Domain models and business logic.
- Domain-specific prompts and evaluation rules.
- Project commands and test suites.
- Project-local architecture decisions.
- Project-specific data and databases.
- Generated application output.
- Product UI and user workflows.
- Domain-specific browser selectors and external integrations.
- Domain-specific security policy that is stricter than the global defaults.

### 5.3 Examples

`Career_Agent_Core` retains job discovery, fit scoring, ATS handling, document generation, application state, and submission behavior. It should consume global provider, security, context, and run contracts.

`baseball_optimizer` retains baseball data, metrics, optimization, and presentation. It may consume shared workflows, provider routing, observability, and project templates.

`redistricting-map` retains map generation, datasets, metrics, and pipeline behavior. It may consume shared project adoption, CI templates, research workflows, and run artifacts.

`Otaku-Timeline` retains AniList-specific data and presentation logic. It may consume shared API reliability patterns and project-level AI workflows without becoming part of the core framework.

---

## 6. Public CLI Contract

The final product should expose a single executable named `ai`.

### 6.1 Required commands

```text
ai install                 Install or repair the framework on this device
ai sync                    Pull updates and refresh generated integrations
ai uninstall               Remove framework-managed integrations safely
ai doctor                  Diagnose installation, providers, paths, and state

ai adopt [path]            Create or repair a portable project integration
ai project show            Display resolved project configuration
ai project validate        Validate the project manifest and local integration

ai route <task>             Explain which provider and workflow would be selected
ai run <task>               Execute a task through the framework
ai review [path]            Run a structured review workflow
ai resume <run-id>          Continue an interrupted or handed-off run

ai status                  Show provider health and current framework state
ai history                 List previous runs
ai inspect <run-id>         Inspect artifacts and route decisions
ai rate <run-id> <rating>   Record feedback for future routing

ai index [path]             Build or update a semantic index
ai search <query>           Search framework and project knowledge
ai skills list              List installed reusable skills
ai tools list               List available optional tools
```

### 6.2 Compatibility aliases

During migration, `airouter` may remain as a compatibility alias. New documentation and workflows should use `ai route`, `ai run`, and `ai status`.

---

## 7. Portable Project Manifest

Each adopted project should contain a committed `.ai-project.toml` file.

Example:

```toml
schema_version = 1
name = "career-agent-core"
project_type = ["go", "automation", "browser"]

skills = [
  "career_automation",
  "secure_coding",
  "browser_testing",
  "observability"
]

[context]
include = [
  "README.md",
  "docs/**/*.md",
  "pkg/**/*.go",
  "cmd/**/*.go"
]
exclude = [
  ".env",
  "*.db",
  "applications/**",
  "logs/**"
]

[commands]
test = ["go", "test", "./..."]
lint = ["golangci-lint", "run"]
build = ["go", "build", "./..."]

[security]
capabilities = [
  "filesystem:repository",
  "network:public",
  "browser:project"
]

[routing]
implementation = ["codex", "claude", "antigravity"]
review = ["claude", "codex"]
research = ["gemini", "claude", "ollama"]
```

### 7.1 Manifest requirements

- Paths are relative to the repository root.
- Secret files are excluded explicitly or by global defaults.
- Commands are argument arrays, not shell strings.
- Provider lists are preferences, not direct executable definitions.
- Unknown capabilities fail validation.
- The framework may infer an initial manifest, but the committed manifest remains reviewable.
- Project settings may tighten global security but may not silently weaken non-overridable global policy.

### 7.2 Generated integration files

`ai adopt` may generate local links, agent-specific command shims, indexes, and caches. These generated artifacts must either live outside the repository or be ignored by Git.

Repositories must not commit absolute symlinks to a local checkout of `ai_knowledge_library`.

---

## 8. Provider Gateway

### 8.1 Provider categories

The framework supports:

- Coding-agent CLI providers: Claude Code, Codex CLI, Gemini/Antigravity.
- Local inference providers: Ollama and OpenAI-compatible local endpoints.
- Hosted inference providers: provider APIs configured by the user.
- Tool providers: MCP servers and local tools.
- Execution backends: optional Zero runtime integrations.

### 8.2 Provider contract

Every provider adapter should implement equivalent operations:

```text
probe()              Detect installation and version
health()             Return current availability and cooldown state
capabilities()       Describe supported task and output capabilities
prepare(request)     Construct a safe provider invocation
execute(request)     Run the provider
normalize(output)    Produce normalized events and result data
classify_failure()   Map failure to the shared failure taxonomy
redact()             Remove secrets from diagnostics and artifacts
```

### 8.3 Shared failure taxonomy

At minimum:

```text
usage_limit
authentication
model_unavailable
permission_or_approval
network_transient
provider_transient
cancelled
timeout
task_failure
invalid_output
security_denied
workspace_unsafe
unknown
```

Only availability and infrastructure failures trigger automatic fallback by default. A normal task failure must not be silently treated as a provider outage.

### 8.4 Configuration precedence

```text
Built-in defaults
    ↓
Device configuration
    ↓
User configuration
    ↓
Project manifest
    ↓
Explicit command-line override
```

Provider executable commands remain device-local and never belong in the portable project manifest.

---

## 9. Context and Knowledge Model

### 9.1 Context scopes

```text
Global
├── reusable rules
├── general technical knowledge
├── reusable skills
└── user profile

Device
├── installed providers
├── hardware and runtime capabilities
├── local paths
└── available services

Project
├── project manifest
├── README and architecture
├── selected source files
├── backlog and decisions
└── semantic index

Run
├── task request
├── selected context
├── attempts
├── tool results
├── validation
└── handoff state
```

### 9.2 Indexed-document metadata

Every indexed chunk should use a shared metadata contract:

```json
{
  "schema": "ai.context_chunk/v1",
  "scope": "project",
  "project": "career-agent-core",
  "source": "docs/architecture.md",
  "content_type": "architecture",
  "sensitivity": "internal",
  "commit": "<git-sha>",
  "updated_at": "<timestamp>"
}
```

### 9.3 Retrieval rules

- Retrieval is scoped before similarity ranking.
- Sensitive content is not indexed without explicit policy.
- Project context does not leak into another project by default.
- Stale chunks are tied to a source hash or commit and replaced deterministically.
- Retrieval results are recorded in the run context manifest.
- Minimal installations may use lexical search only.

---

## 10. Security and Capability Model

### 10.1 Capability families

```text
filesystem:none
filesystem:repository
filesystem:explicit_paths
filesystem:user_approved

network:none
network:public
network:allowlist
network:user_approved

process:none
process:test_only
process:project_commands
process:user_approved

browser:none
browser:read_only
browser:project
browser:user_approved

git:read
git:edit
git:commit
git:push

database:none
database:project
database:user_approved

secrets:none
secrets:named_reference
```

### 10.2 Required security gates

- Prompt-injection and untrusted-context labeling.
- Secret redaction before diagnostics and model submission.
- Repository boundary checks for file access.
- Safe subprocess argument arrays; no implicit shell execution.
- Clean-worktree requirement for edit mode unless explicitly overridden.
- Explicit intent for commit and push capabilities.
- Network-host and redirect validation where supported.
- Structured validation of model-generated commands or plans.
- Durable audit events for denials and approvals.

### 10.3 Approval behavior

Capabilities may be:

- Always denied.
- Allowed by policy.
- Allowed only in a named mode.
- Allowed once after user approval.
- Allowed for the current run.
- Allowed for a specific project command.

No model may grant itself additional capabilities.

---

## 11. Run, Event, and Artifact Contracts

### 11.1 State location

Use platform-appropriate user state directories. A Linux example is:

```text
~/.local/state/ai-framework/
├── state.db
├── projects/
├── indexes/
└── runs/
    └── <run-id>/
        ├── request.json
        ├── project.json
        ├── context_manifest.json
        ├── route.json
        ├── attempts/
        ├── events.jsonl
        ├── result.json
        ├── validation.json
        ├── handoff.md
        └── git.patch
```

### 11.2 Request schema

A task request should include:

```json
{
  "schema": "ai.task_request/v1",
  "task": "Implement retry handling and add tests",
  "mode": "edit",
  "project_root": "/resolved/path",
  "task_type": "implementation",
  "required_capabilities": ["filesystem:repository", "process:project_commands"],
  "preferred_providers": [],
  "metadata": {}
}
```

### 11.3 Event schema

Every provider and workflow emits normalized JSONL events:

```json
{
  "schema": "ai.run_event/v1",
  "run_id": "...",
  "attempt_id": "...",
  "timestamp": "...",
  "source": "provider:codex",
  "type": "message|tool_call|tool_result|warning|error|status|metric",
  "payload": {}
}
```

### 11.4 Handoff schema

A handoff must summarize:

- Original task.
- Project and starting commit.
- Context already reviewed.
- Work completed.
- Files modified.
- Commands run and results.
- Failure or interruption reason.
- Remaining work.
- Security constraints and denied operations.
- Recommended next provider or workflow.

---

## 12. Workflow Model

### 12.1 Reusable workflow stages

A workflow may include:

```text
classify
→ assemble context
→ security preflight
→ plan
→ execute
→ validate
→ review
→ repair
→ summarize
→ persist artifacts
```

Not every task requires every stage.

### 12.2 Required reusable workflows

The current reusable commands should become framework workflows:

- Work next backlog item.
- Resume an interrupted task.
- Groom and normalize backlogs.
- Review current diff.
- Create or update an ADR.
- Perform architecture review.
- Run project validation.
- Extract reusable knowledge from completed work.

### 12.3 Workflow-provider separation

Workflows define stages and success criteria. Providers execute stages. A workflow must not hardcode a single provider unless the capability is provider-specific.

---

## 13. Installation Profiles

### 13.1 Minimal

For laptops, WSL, low-powered devices, containers, and lightweight VMs:

- `ai` executable.
- Global rules and skills.
- Project manifests.
- CLI-provider routing.
- SQLite state and run artifacts.
- Lexical context search.
- No daemon required.

### 13.2 Standard

Adds:

- Semantic indexing.
- MCP server.
- Local API.
- TUI or web status interface.
- Extended telemetry and history.
- Optional background service.

### 13.3 Full

Adds optional components:

- Ollama and local models.
- ChromaDB or PGVector.
- Background workers.
- Browser automation.
- Homelab integrations.
- Extended tool packs.

### 13.4 Installation commands

```text
ai install --profile minimal
ai install --profile standard
ai install --profile full
ai sync
ai doctor
```

The installer must be idempotent and must record which files and links are framework-managed.

---

## 14. Proposed Repository Layout

This is a target layout, not a requirement for one immediate restructuring change.

```text
ai_knowledge_library/
├── cmd/
│   └── ai/                       # Main cross-platform executable
├── runtime/
│   ├── router/                   # Provider selection and fallback
│   ├── providers/                # Provider adapters
│   ├── execution/                # Safe process and API execution
│   ├── workflows/                # Run lifecycle and orchestration
│   ├── state/                    # SQLite and migrations
│   └── artifacts/                # Run artifacts and handoffs
├── knowledge/
│   ├── rules/
│   ├── skills/
│   ├── prompts/
│   ├── profiles/
│   └── schemas/
├── context/
│   ├── indexing/
│   ├── retrieval/
│   ├── cache/
│   └── stores/
├── security/
│   ├── quarantine/
│   ├── capabilities/
│   ├── secrets/
│   ├── workspace/
│   └── validation/
├── adapters/
│   ├── cli/
│   ├── mcp/
│   ├── local_api/
│   └── sdk/
├── templates/
├── toolpacks/
├── documentation/
├── installers/
└── tests/
```

Existing paths may remain during migration. New modules should avoid increasing overlap between current orchestration implementations.

---

## 15. Templates and Tool Packs

### 15.1 Project templates

Potential templates:

```text
templates/
├── go-service/
├── python-tool/
├── rust-api/
├── typescript-web/
├── static-site/
├── ai-agent/
└── data-pipeline/
```

Commands may include:

```text
ai new go-service my-project
ai add docker
ai add github-actions
ai add playwright
```

### 15.2 Optional tool packs

Small reusable administration utilities may be grouped by capability:

```text
toolpacks/
├── filesystem/
├── windows-iis/
├── database/
├── observability/
└── reporting/
```

Tool packs are optional and may not increase the dependency footprint of the minimal profile.

---

## 16. Migration Plan

### Phase 0: freeze overlapping ownership

**Goal:** Avoid building additional duplicate routing and orchestration systems.

- Mark this blueprint as the target architecture.
- Document current overlapping modules.
- Require new provider-routing features to choose one future owner.
- Add architecture tests or review checks where practical.

**Exit criteria:** New work references the ownership boundaries in this document.

### Phase 1: contracts and portability

**Goal:** Establish stable cross-project contracts.

- Define `.ai-project.toml` schema version 1.
- Define task, capability, event, result, and handoff schemas.
- Implement `ai adopt` and `ai project validate`.
- Remove committed absolute symlinks from adopted projects.
- Generate agent-specific integrations during install or adoption.
- Establish platform-specific state and configuration directories.

**Exit criteria:** A fresh project can be adopted on two different operating systems without editing committed paths.

### Phase 2: unify routing and run state

**Goal:** Make provider routing a framework service.

- Integrate or package `ai_router` functionality beneath the framework.
- Expose `ai route`, `ai run`, `ai status`, `ai history`, and `ai inspect`.
- Use one provider configuration and health database.
- Use one failure taxonomy.
- Use one artifact layout.
- Preserve a temporary compatibility path for `airouter`.

**Exit criteria:** Claude Code, Codex, and Gemini CLI tasks can be routed and handed off through the framework.

### Phase 3: connect orchestration, context, and security

**Goal:** Route every workflow through shared context and policy gates.

- Make workflow orchestration request providers through the shared runtime.
- Implement scoped context assembly.
- Add context-manifest artifacts.
- Standardize quarantine, secrets, workspace, and capability gates.
- Record approvals and denials as events.

**Exit criteria:** Provider selection, context selection, and capabilities are explainable from one run record.

### Phase 4: migrate a pilot project

**Recommended pilot:** `Career_Agent_Core`, because it contains the most duplicated provider, prompt, retry, and agent-control functionality.

- Replace copied global prompts and commands with a project manifest.
- Keep only career-specific project instructions locally.
- Route generic provider calls through the framework gateway.
- Reuse global run IDs and telemetry where practical.
- Keep job-domain logic within the project.

**Exit criteria:** The project remains independently testable while using the framework for shared AI infrastructure.

### Phase 5: templates, tool packs, and additional projects

- Adopt `baseball_optimizer`, `redistricting-map`, `pizza_party_metrics`, `Otaku-Timeline`, and Zero using manifests.
- Extract stable templates from repeated CI and project scaffolding.
- Convert mature utility scripts into optional tool packs.
- Add automated repository scans for repeated framework candidates.

**Exit criteria:** New projects can start with the framework without copying framework source.

---

## 17. Initial Implementation Backlog

The first implementation issues should be small and contract-oriented.

| Priority | Item | Deliverable |
| --- | --- | --- |
| P0 | Add project manifest schema | `schemas/ai-project.schema.json` and examples |
| P0 | Define capability vocabulary | Versioned schema and validation rules |
| P0 | Define task and run event schemas | JSON schemas plus test fixtures |
| P0 | Define state-directory policy | Cross-platform path resolver and tests |
| P0 | Implement project discovery | Locate repository root and manifest safely |
| P0 | Implement `ai project validate` | Validate paths, commands, capabilities, and schema |
| P1 | Implement `ai adopt` | Generate a manifest and local integrations safely |
| P1 | Add portable integration test | Adopt the same fixture from different base paths |
| P1 | Inventory duplicate runtime modules | Written mapping of current and future ownership |
| P1 | Package router behind an interface | Framework-level route request and response contract |
| P1 | Normalize provider failure classes | Shared enum/schema and adapter tests |
| P1 | Create common artifact writer | Atomic run-directory and JSONL handling |
| P2 | Add scoped lexical context search | Minimal-profile retrieval implementation |
| P2 | Add semantic context adapter | Optional Chroma/PGVector implementation |
| P2 | Add MCP interface for `ai run` | External agents can invoke framework workflows |
| P2 | Pilot Career Agent integration | Project manifest and first shared provider call |

---

## 18. Acceptance Criteria for the Framework

The framework is considered usable as the central AI control plane when all of the following are true:

### Portability

- A user can install the minimal profile on Windows, macOS, and Linux.
- A repository contains no required absolute link to the library checkout.
- `ai doctor` identifies missing providers and broken managed integrations.
- `ai sync` updates global rules and generated integrations idempotently.

### Project integration

- `ai adopt` creates a valid portable manifest.
- Project-specific commands are argument arrays and run from the project root.
- Global and project guidance can be resolved without copying the full knowledge library into the project.
- A project can tighten security and context exclusions locally.

### Routing

- The framework routes among at least Claude Code, Codex CLI, and Gemini/Antigravity.
- Route decisions are explainable.
- Provider availability failures can trigger fallback.
- Normal task failures are not mislabeled as provider outages.
- Provider state and run history persist locally.

### Security

- Edit mode validates the repository and working tree.
- Dangerous capabilities require policy or approval.
- Provider commands do not use implicit shell execution.
- Secrets are redacted from diagnostic artifacts.
- Denials and approvals appear in the run event stream.

### Context

- Context retrieval is scoped by project.
- The selected context is recorded in a manifest.
- Sensitive exclusions are enforced before indexing.
- The minimal profile works without a vector database.

### Observability

- Every run has a request, route decision, attempt records, normalized events, result, and validation status.
- Edit runs capture the resulting Git patch.
- Interrupted runs can produce a useful handoff.
- User ratings can influence future routing without becoming the sole signal.

---

## 19. Non-Goals

The first stable framework does not need to:

- Replace provider authentication systems.
- Copy or inspect provider OAuth token stores.
- Automatically commit or push every edit.
- Require a daemon for basic use.
- Require a vector database.
- Centralize every helper script immediately.
- Move domain logic out of existing projects.
- Guarantee that one provider can resume every other provider's internal session.
- Grant models unrestricted shell, filesystem, browser, or network access.
- Make Zero the mandatory implementation language or runtime.

---

## 20. Risks and Mitigations

### Risk: the repository becomes a monolith

**Mitigation:** Maintain stable interfaces, optional components, installation profiles, and plugin boundaries. Core routing and policy should not require every tool pack.

### Risk: Python, Go, and project runtimes diverge

**Mitigation:** Define language-neutral JSON schemas and local process/API contracts before creating language-specific SDKs.

### Risk: copied prompts continue to drift

**Mitigation:** `ai adopt` should detect vendored global prompts and report them. Generated shims should point to framework-managed sources.

### Risk: one framework failure blocks every project

**Mitigation:** Preserve direct project commands, use graceful fallback, keep artifacts human-readable, and make the minimal profile small and testable.

### Risk: context leaks between projects

**Mitigation:** Scope indexes and caches by project identity and repository root. Record all selected context in the run manifest.

### Risk: over-automatic execution

**Mitigation:** Use capability gates, explicit modes, clean-worktree checks, provider-native permissions, and user approvals for irreversible actions.

---

## 21. Decision Log

| Decision | Status | Rationale |
| --- | --- | --- |
| `ai_knowledge_library` is the central framework | Accepted for roadmap | It already owns global rules, skills, installation, context, and orchestration experiments |
| `ai_router` becomes the routing runtime | Proposed | It has the stronger provider health, fallback, history, and artifact model |
| Projects use `.ai-project.toml` | Proposed | It replaces copied global files and machine-specific links with a portable contract |
| One public executable named `ai` | Proposed | It gives users and projects a stable central entry point |
| Capabilities default to deny | Proposed | AI execution needs explicit, auditable boundaries |
| Zero remains independent | Accepted for roadmap | Its compiler and VM are a product, while selected contracts can be shared |
| Minimal installation avoids heavy dependencies | Accepted for roadmap | The framework must fit on many devices and remain easy to clone and use |

---

## 22. Immediate Next Step

The next code change after approving this blueprint should **not** be a broad directory rewrite.

Create the versioned project manifest and capability schemas, then implement a read-only `ai project validate` command. These contracts will give later routing, context, security, and project-adoption work a stable foundation.

Suggested first milestone:

```text
Milestone: Portable Project Contract v1

Deliverables:
- .ai-project.toml specification
- JSON schema
- capability schema
- path and command validation
- example manifests
- project discovery
- ai project validate
- cross-platform tests
```

This milestone is deliberately small enough to complete and validate without prematurely merging all existing orchestration code.
