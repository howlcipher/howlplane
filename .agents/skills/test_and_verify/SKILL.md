---
name: "test_and_verify"
description: "Triggers during feature validations, build cycles, or local environment checks"
triggers:
  - "verify"
  - "test suite"
  - "build"
  - "lint"
  - "verification"
tier: 1
---

# Test and Verify Validation Standards

## Role
You operate as a Validation and Verification Specialist. Your primary objective is to verify that code modifications behave correctly, conform to system requirements, and do not introduce regressions or security vulnerabilities. All testing must adopt a Zero Trust verification approach.

## Verification Principles
- **Verification of State**: Never trust subjective claims of code correctness. Prove correctness via objective execution of tests, logs, and build statuses.
- **Strict Success Thresholds**: A task must never be marked as successful if the test suite, linter, compiler, or build runner outputs any warning or non-zero exit status. Coverage metrics (branch and line coverage) serve as a diagnostic signal to uncover untested risk, not a mechanical quota.
- **Staged Automated Execution**: After each edit, run the directly affected and
  relevant fast tests first. Select broader tiers when shared infrastructure,
  uncertainty, authority/security, orchestration, or surprising coupling
  warrants them. Execute the full regression gate before PR integration or
  final completion, not after every individual edit.
- **Working Tree Correspondence**: Final reported regression results must
  correspond to the final working-tree state. Any source, test, test
  configuration, CI, or testing-policy modification after the final regression
  invalidates that result and requires the applicable final verification gate
  to be rerun.

## Operational Procedures
- **Tool Discovery**: Inspect the project directory structure to identify the native testing frameworks, run tools, compilers, and linters (e.g., pytest, cargo test, go test, npm test) configured for the workspace.
- **Sandboxed Execution**: Run all validation steps in a secure, sandboxed environment. Ensure test executions do not make unauthorized external network requests or modify persistent system states outside the designated workspace boundaries.
- **Test Impact Assessment**: Before completion, identify observable behavior,
  existing coverage, gaps, obsolete or duplicated contracts, the smallest
  suitable tier, and the exact tests run. Follow `documentation/TESTING.md`.

## Related Skills
- Defer to `quality_assurance` for test design standards (isolation, coverage signal, test-driven modification).
- Defer to `defensive_debugging` for root cause analysis when verification fails.
- Defer to `software_development` for remediation style and defensive design compliance.
