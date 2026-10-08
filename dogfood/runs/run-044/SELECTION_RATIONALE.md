# run-044 selection rationale (clean pair on main, run 2)
Chosen by coverage (MISSION_COVERAGE.md), not by likely success:
- Language never covered: TypeScript (strict tsc, NodeNext ESM, node:test). Prior runs: Python, Go, JavaScript, Rust.
- Capability never checked: "integration task". relnotes must integrate with an external system (git) instead of a hand-made file: run git as a subprocess without a shell, parse arbitrary commit text robustly, map git failures to user errors, and prove it with integration tests against real temporary repositories that are isolated from the host's git config.
- Existing code (relnotes 0.3.0, commit 1a1b8ec, 5 tests, README). Dependencies already installed in the checkout (node_modules, ignored), as in a developer's working copy; this Node build has no native TypeScript support, so tsc is required.
- Requirements include a semantic rule shared between both input modes (breaking changes) and an exact output format, so the reviewer diff must be read carefully.
- Clean working tree (WIP preservation was covered in run-043).
Not a targeted experiment: normal AUTO routing, default verification.
