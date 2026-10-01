# Independent Review Change

Conduct independent, adversarial review passes on proposed code changes to falsify correctness and uncover hidden defects.

## 1. Principles

- **Independent reasoning:** Do not assume code works because it looks clean or tests pass.
- **Falsification directive:** Actively search for logic flaws, regressions, vulnerabilities, vacuous tests, architectural bloat, and needless complexity.
- **No biased briefs:** Reviewers receive objective, criteria, constraints, and diff, and are instructed to challenge correctness.

## 2. Reviewer Roles

Execute the recommended specialized reviewer roles from:
- `correctness-reviewer`: logic defects, unhandled edge cases, contract mismatches.
- `regression-reviewer`: backwards compatibility breaks, call-site drift, side-effects.
- `security-reviewer`: trust boundaries, credential leaks, injection risks, authorization.
- `test-falsifier`: vacuous assertions, inaccurate mocks, untested branches, falsified tests. Also duplicate test contracts, obsolete tests, tests whose names claim behavior they do not exercise, and append-only test accumulation. Check the recorded Test Impact Assessment against the diff, and require written evidence that equivalent protection remains for any test removed or consolidated.
- `architecture-reviewer`: unnecessary coupling, leaky abstractions, boundary violations.
- `simplicity-reviewer`: needless complexity, overengineering, dead code, minimal alternatives.

## 3. Procedure

1. Generate review briefs using the control plane:
   ```bash
   python -m src.control_plane briefs --task-file <task_spec.yaml> --diff-file <diff.patch>
   ```
2. For each reviewer role, examine the changes against its specialized falsification criteria.
3. Collect all structured findings into a findings file (e.g. `findings.yaml`) containing: ID, title, severity (`blocker`, `high`, `medium`, `low`, `informational`), category, location, evidence, and suggested fix.
