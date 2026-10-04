# run-011 verification
Not a clean run: exit 2, Status BLOCKED (AUDIT BLOCKED after 2 rework rounds). Engine: howlplane dogfood/DOG-012-review-budget 9a9a59c.
- DOG-012 verified live: Cursor reviews 92 s, 246 s, 307 s; the last would have been killed at the old 300 s budget.
- DOG-011 rework loop fired live for the first time and worked mechanically (findings -> Codex -> re-verify -> re-review, twice).
- New findings: DOG-013, DOG-014.
Generated app not independently verified (run did not complete); it remains in dogfood-missions/run-011 untouched.
