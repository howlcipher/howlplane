# run-043 selection rationale (clean pair on main, run 1)
Goal: prove main bb7ba21 (DOG-026..034 merged) on the paths the recent fixes touched, through the public CLI only.
- Existing Python code change (pingbot, a webhook notifier CLI; commit 5169bf2, 11 tests, README).
- Credential-heavy request: "API token", "the token itself", `Authorization: Bearer <token>`, `PINGBOT_API_TOKEN=<your-token>==`, `PINGBOT_TIMEOUT=10`. Pre-checked with the canonical redactor: unchanged, so any redaction marker in the goal or review notes is a false redaction (DOG-029).
- The task is about masking, so the diff and README will contain masked forms, placeholders and token-shaped test fixtures: exercises the unredacted reviewer diff (DOG-027/030).
- Uncommitted user WIP, not mentioned in the request: modified pingbot/templates.py (imported by the CLI) and untracked notes/release-checklist.md. sha256 in dogfood-missions/run-043/wip.sha256.
- A real latent bug (values containing `=` truncated) the request asks to fix.
Not a targeted experiment: normal AUTO routing, default verification.
