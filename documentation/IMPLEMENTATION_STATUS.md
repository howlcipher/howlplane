# Implementation Status

> **Scope warning (2026-08-30).** This document tracks the **Go `ai`-framework
> blueprint** only — `internal/project`, `internal/runtime`, `internal/provider`,
> `internal/capability`, `cmd/ai`, and `schemas/`. It does **not** describe the
> live control plane, which is the Python package `src/control_plane/` and is
> documented in `documentation/CONTROL_PLANE.md`. The two tracks are parallel and
> unintegrated; the Python track is the one in operational use and is far more
> mature than the milestones below suggest. In particular, the open decision
> "final cross-platform standard paths for SQLite state" belongs to the Go
> blueprint and is unrelated to the derived factory index described in
> `documentation/adr/0006_persistent_factory_supervisor.md`, which keeps durable
> files authoritative. Read this file as blueprint history, not current state.

## Current-State Assessment
The repository currently functions as a hybrid Go/Python project. Go provides the `ai_installer` CLI (using Cobra), while Python drives the `src/core` logic, testing, and vector indexing. The orchestrator logic and provider interactions exist but are spread across experimental Python files, and there is no unified `ai` command. 

## Completed Blueprint Capabilities
- **Portable Project Manifest (`.ai-project.toml`) v1:** schema (`schemas/ai-project.schema.json`), capability vocabulary (`schemas/capability.schema.json`), Go parser and validator (`internal/project/manifest.go`) enforcing schema version, required fields, known capabilities, relative/non-escaping context paths, and argv-array (non-shell-string) commands. Backed by `internal/project/manifest_test.go`, `internal/project/examples_test.go`, and the Python schema-drift regression test `tests/test_manifest_schema_drift.py`.
- **Project root discovery:** `internal/project/discover.go` walks up from a start path to the nearest `.ai-project.toml` or `.git`, with cross-platform-flavored coverage in `internal/project/discover_test.go`.
- **`ai project validate` command:** `cmd/ai/project.go` discovers the project root, loads and validates the manifest, and reports specific errors on failure.
- **Manifest specification document:** `documentation/PROJECT_MANIFEST_SPEC.md`.
- **Task/event/result/handoff schemas (blueprint section 11):** `schemas/task-request.schema.json`, `schemas/run-event.schema.json`, `schemas/run-result.schema.json`, `schemas/handoff.schema.json`, and the shared `schemas/failure-class.schema.json` taxonomy (blueprint section 8.3). Mirrored on the Go side by `internal/runtime` (`TaskRequest`, `RunEvent`, `RunResult`, `Handoff`, `FailureClass`, each with `Load<Type>`/`Parse<Type>`/`Validate`). Backed by `internal/runtime/*_test.go` and the Python schema-drift regression test `tests/test_task_event_schema_drift.py`. Example fixtures live in `examples/runs/`.
- **Provider interface and router (blueprint sections 8.2, 3.5):** `internal/provider` defines the `Provider` adapter contract (`Probe`, `Health`, `Capabilities`, `Prepare`, `Execute`, `Normalize`, `ClassifyFailure`, `Redact`) and a `Router` that selects among registered providers, returning an explainable `RouteDecision` (candidates considered, exclusion reasons, final selection). No adapters implement `Provider` yet — see Partially Completed Capabilities.
- **Shared capability enum de-duplication:** the 24-value capability enum previously hardcoded separately in `internal/project/manifest.go` now lives once in `internal/capability`, used by both `internal/project` and `internal/runtime`.
- **Task/event/result/handoff specification document:** `documentation/TASK_EVENT_SPEC.md`.

(Legacy implementations of provider routing exist in `ai_router`/`src/core` but are not yet migrated to the unified framework.)

## Partially Completed Capabilities
- **CLI Foundation:** The `ai` executable (`cmd/ai`) exists with the `project validate` subcommand; the `ai_installer` Cobra CLI remains a separate pattern reference. Most commands from blueprint section 6.1 (`adopt`, `route`, `run`, `status`, etc.) are not yet implemented.
- **Provider Routing:** The shared `Provider` interface and `Router` exist in `internal/provider`, but no adapter (Claude Code, Codex, Gemini, Ollama) implements the interface, nothing in `cmd/ai` calls the router, and the legacy Python logic in `src/core` (`provider_preflight.py`, `transport_retry.py`, `claude_code_backend.py`) is not yet migrated behind it.

## Missing Capabilities
- `ai adopt`, `ai route`, `ai run`, and the remaining unified CLI surface from blueprint section 6.1.
- Real provider adapters implementing `internal/provider.Provider`, and health checks wired to them.
- State-directory and run-artifact persistence (blueprint 11.1): nothing yet writes `internal/runtime` documents to `~/.local/state/ai-framework/runs/<run-id>/`.
- Scoped context indexing.

## Technical Risks
- **Language Divergence:** Parsing manifests in Go for the CLI may require duplicated parsing logic in Python if Python components also need to read `.ai-project.toml`.
- **Dependency Bloat:** Adding TOML parsing to Go requires an external dependency, as the standard library does not support TOML. 
- **Overlapping Orchestration:** Existing Python scripts and the new framework may drift if not unified cleanly.

## Architectural Decisions Still Required
- **Shared Parsing:** How will Python access the `.ai-project.toml` data (e.g., Go binary outputs JSON for Python, or duplicate parsing)?
- **State Location:** Final cross-platform standard paths for SQLite state and runs.

## Ordered Implementation Backlog
1. ~~Define formal task, event, result, and handoff schemas.~~ Done.
2. Establish state-directory policy.
3. Implement `ai adopt`.
4. ~~Package router behind an interface.~~ Done.
5. Implement a real provider adapter (e.g. Claude Code) against the `internal/provider.Provider` interface.
6. Wire `internal/runtime` documents to the state-directory layout (blueprint 11.1) so runs persist.

## Current Active Milestone
**Task/Event/Result/Handoff Schemas + Provider Router Interface — complete**

All deliverables landed:
- `ai.task_request/v1`, `ai.run_event/v1`, `ai.run_result/v1`, `ai.handoff/v1`, and `ai.failure_class/v1` JSON schemas (`schemas/`).
- Go runtime types and validators (`internal/runtime`), mirroring `internal/project/manifest.go`'s hand-rolled-validation-mirrors-JSON-Schema pattern for each of the four artifacts.
- Shared capability enum extracted to `internal/capability`, removing the duplicate hardcoded map previously inline in `internal/project/manifest.go`.
- Provider adapter contract and explainable router (`internal/provider`: `Provider` interface, `Router`, `RouteDecision`).
- Example fixtures (`examples/runs/*.json`), all passing validation on both the JSON Schema side and the Go `Validate()` side.
- Task/event spec document (`documentation/TASK_EVENT_SPEC.md`).
- Go test coverage (`internal/runtime/*_test.go`, `internal/provider/router_test.go`, `internal/capability/capability_test.go`) and a Python schema-drift regression test (`tests/test_task_event_schema_drift.py`) that keeps the JSON schemas and example fixtures from silently diverging.

Previous milestone: **Portable Project Contract v1 — complete** (`.ai-project.toml` schema, project discovery, `ai project validate`, `documentation/PROJECT_MANIFEST_SPEC.md`).

Next milestone per the ordered backlog above: establish the state-directory policy (blueprint 11.1), which unblocks persisting `internal/runtime` documents to real run directories and, after that, a first real provider adapter.
