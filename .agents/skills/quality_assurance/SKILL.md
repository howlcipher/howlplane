---
name: "quality_assurance"
description: "Triggers during testing, validation, and QA processes."
triggers:
  - "testing"
  - "test coverage"
  - "regression"
  - "validation"
  - "qa"
tier: 1
pipeline_pass: 2
---

# Quality Assurance Guidelines

Standards, methodologies, and guidelines for testing, validation, and quality assurance processes.

## Testing Principles and Isolation
- **Test Automation**: Prioritize automated test execution over manual verification to ensure reproducible, fast, and consistent validation. Enforce strict success thresholds, where any test failure, compiler warning, or linter error invalidates the task.
- **Verification of State**: Never trust subjective claims of correctness. Always prove correctness via objective execution and verification of tests, logs, and build statuses.
- **Unit Testing**: Design unit tests to be completely isolated, stateless, and fast. Avoid external dependencies, database queries, or network requests in unit tests.
- **Integration Testing**: Write comprehensive integration tests to verify API endpoints, database transactions, and component interactions under realistic scenarios.

## Enforcements and Metrics
- **Coverage Signal**: Use branch and line coverage to discover untested risk,
  never as the sole reason to add tests. Prioritize authority, irreversible
  actions, durability, fallback, recovery, public contracts, and integration
  boundaries.
- **Regression Testing**: Execute full regression test suites on every pull request to identify side-effects or regressions in existing functional paths.
- **Test-Driven Modification**: Before implementing a feature or fixing a bug, locate or write tests that cover the affected code pathways. Ensure new code is tested under both success and edge/failure conditions.
- **Suite Relevance**: For every code change, assess whether existing tests still
  represent intended behavior, whether coverage is missing, and whether a test
  is obsolete or duplicative. Keep the cheapest test for the inner-loop
  contract while retaining broader integration coverage at its proper gate.

## Related Skills
- Defer to `test_and_verify` for the operational verification workflow (tool discovery, automated execution, sandboxed runs).
- Defer to `defensive_debugging` for the diagnostic protocol and remediation principles when tests fail.
