# Verify Engineering Change

Execute deterministic, project-specific verification suites and capture verifiable evidence.

## 1. Principles

- **Deterministic proof:** Differentiate between `claimed`, `tested`, `observed`, and `verified`.
- **Clean gate policy:** "Tests passed" is not enough if warnings or unverified checks remain.
- **Fail-closed:** Any failing required step marks overall verification as `failed`.

## 2. Procedure

1. Run the project verification plan:
   ```bash
   python -m src.control_plane verify --project-dir . --task-id <task_id>
   ```
2. Check deterministic output:
   - Build compilation (e.g. `go build`, `tsc`, `cargo build`).
   - Linters and static analyzers (e.g. `flake8`, `bandit`, `gofmt`).
   - Automated unit and integration tests (e.g. `pytest`, `go test`).
3. Record step results, exit codes, and durations in the evidence ledger:
   ```bash
   python -m src.control_plane record --task-id <task_id> --agent-id <agent_id> --action verification_executed --result <passed|failed>
   ```
4. For a code-changing task, perform and record the Test Impact Assessment (policy: `AGENTS.md`, `documentation/TESTING.md`). State the behavior that changed, the tests that protect it, which tests were added, updated, or removed, whether duplicate, obsolete, or misnamed tests were reviewed, and whether the risk requires the full regression gate. Save it as JSON matching `schemas/test-impact-assessment.schema.json`, then:
   ```bash
   python -m src.control_plane tia record --task-id <task_id> --file tia.json
   ```
   Run the regression scope the assessment calls for against the final tree. Any later source, test, or configuration edit invalidates that run and requires it again.
