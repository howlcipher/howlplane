---
name: test_driven_development
description: Forces the agent to write unit tests for all backend logic.
---
Use behavior-focused Test Driven Development. For a bug, first reproduce the
observable failure, implement the correction, then run the targeted and
relevant regression tests. For new behavior, define the observable contract,
test it, implement it, then run the smallest appropriate tier and any warranted
broader regression.

Do not generate tests merely because a function exists. Tests must protect
behavior, contracts, invariants, authority boundaries, and regressions. On
every code-changing task, assess whether current tests are still relevant,
whether a failure or edge case is missing, and whether an obsolete or duplicate
test should be updated, consolidated, or removed with evidence.
