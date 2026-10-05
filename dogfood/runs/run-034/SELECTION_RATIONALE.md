# Mission selection rationale (run-034, adaptive tranche 1, Mission D)
Recent: 030 Go feature, 031 Python bug fixes, 032/033 Node refactor with dirty tree. All required source changes.
This mission instead tests:
- a test-only task with a hard "do not change the source" constraint (Howl must not "fix" what it finds)
- characterization of undocumented legacy rules (rounding modes, strict tier boundaries, discount combination, date windows)
- determinism requirements (no dependence on today's date)
- a judgement deliverable (TESTING_NOTES.md listing suspicious behaviour)
- the DOG-027 fix: reviewers must confirm pricing.py is unchanged; they now receive the session diff
First mission on engine 175b57a (DOG-026 + DOG-027 + DOG-028).
