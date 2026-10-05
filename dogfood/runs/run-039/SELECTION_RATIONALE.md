# Mission selection rationale (run-039, adaptive tranche 2)
Tranche 1 missions all finished in one 4-7 minute implementation pass against a 600 s budget.
This mission instead tests:
- a substantially larger, cross-cutting change (new module + CLI, schema change, auth on every endpoint, data migration, rewriting existing tests, docs): realistic size for a "make it multi-user" request
- behaviour near or beyond the implementation execution budget: timeout handling, partial work, recovery guidance (tranche 1 report frontier item 2)
- security-sensitive requirements (hashed tokens, 401 semantics, 404-not-403 isolation)
- evolving a codebase Howl itself produced earlier (run-031's result)
Normal AUTO routing, no budget flags (a user would not know to pass them).
