# run-032 result
Status: HANDOFF REQUIRED (exit 2) after 5.8 min. NOT CLEAN. Finding DOG-026.
Engine 41a1621. AUTO: Claude planning, Codex implementation (5 min); `npm test` (derived) failed 1/59; session stopped with no rework.
Implementation defect left by Codex: file `outDir: 42` accepted when INVOICE_OUT_DIR overrides it, while its own test expects exit 2.
Dirty-tree probe: user's uncommitted templates/footer.txt and untracked notes/todo.md preserved byte-for-byte (sha256 OK); not committed, stashed, or altered. Workers' changes left uncommitted alongside them.
Not counted toward the tranche; mission rerun as run-033 on the DOG-026 engine.
