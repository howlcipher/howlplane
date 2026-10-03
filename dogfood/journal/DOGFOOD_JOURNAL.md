# Dogfood Journal

## 2026-10-02 Initialization
- Phase 0: searched for dogfood/CAMPAIGN_STATE.md, HANDOFF.md, journal, findings. None exist. Starting a new campaign.
- Phase 1/2: surveyed 11 Howl repos, all clean on main, fetch shows 0 behind.
- Phase 3: public interface is `howl orchestrate "<goal>" --repo <git repo>` (forwards to `howlplane orchestrate`), plus `howl factory prepare`, `howl agents doctor`. Documented in howlplane/documentation/ORCHESTRATE.md.
- Campaign records live in howlplane/dogfood on branch dogfood/campaign-household-tasks.
- 21:14 run-001 started: factory prepare OK (BYPASS trust), orchestrate session 791d33f0, planner=Claude.
- 2026-10-03 01:28Z run-001 ended exit=2 HANDOFF REQUIRED. Opened DOG-001. Generated app verified working by hand (read-only check).
- 2026-10-03 DOG-001 fixed (110091a), run-002 started. Discovery: howl engine is an editable install of howlplane working tree, so repair branches take effect immediately; campaign records moved to own worktree.
- DOG-001 fix 110091a pushed to origin/dogfood/DOG-001-persist-verdict-text.
- run-002 ended HANDOFF REQUIRED: DOG-001 fix verified; new root cause DOG-002 (acceptance not given audit). DOG-002 fixed b07a4e0. DOG-003 filed. Next: run-003.
- run-003 COMPLETE and independently verified: CLEAN RUN 1 PASS. DOG-002 pushed b07a4e0. Starting run-004 on unchanged code.
- run-004 FAILED (HANDOFF REQUIRED): DOG-003 blocked acceptance. Fix 82d3cde. run-003 retracted from clean count (streak 0/2). Next run-005.
- run-005 COMPLETE + verified: CLEAN RUN 1/2 on fixed code. DOG-003 first push failed slopslint (my dup tests); fixed a3c7d7e, repush in progress. Starting run-006.
- run-006 COMPLETE + verified: CLEAN RUN 2/2. DOGFOOD RESULT: PASS. Final report written.
- 2026-10-03 PR #138 merged (aeac4b9). howlplane checkout returned to main.
- 2026-10-03 follow-ups: DOG-004..008 written up; code committed 338478b; starting regression runs.
- run-007 resume: DOG-010 first fix produced false success (Codex rejected, Cursor accepted). Corrected: any-agent verdict is final. Run-007 not counted.
- run-008: HANDOFF REQUIRED on real Cursor finding (README JSON-error claim). DOG-011 filed. PRs being opened for followups + readme.
- run-009 COMPLETE + verified: clean run 1/2 on new code. Starting run-010.
- run-010 COMPLETE + verified: clean run 2/2 on new code (009+010). Final report addendum written.
- 2026-10-03 resume check (new session): all PRs merged, main src == aedf82f, PASS remains valid; howlframe fast-forwarded; no new runs.
- 2026-10-03 host fault: ~16 user site-packages missing (httpx, pydantic, ...) broke pre-push; restored via pip --user. Records branch dogfood/resume-check-20261003 pushed (46357fe).
- 2026-10-03 DOG-012 fixed (review budget 600 s), 9a9a59c; regression runs 011/012 planned on that engine. Target run-011 created (39f7b1a).
- 2026-10-03 run-011 (engine 9a9a59c): BLOCKED after 2 rework rounds. DOG-012 verified live (307 s review). DOG-011 loop fired live. Filed DOG-013 (review never converges), DOG-014 (reviewer lacks harness verification). Fixed both in 82414bf; push in progress. Next: run-012 from a fresh target.
- 2026-10-03 DOG-013/014 pushed (82414bf). run-012 COMPLETE (Cursor CLEAN first pass, Codex ACCEPTED), app verified: clean on 82414bf. Filed+fixed DOG-015 (a1d8ce2), so streak restarts: run-013 and run-014 on a1d8ce2.
- 2026-10-03 DOG-015 pushed (a1d8ce2). run-013 HANDOFF REQUIRED: acceptance rejected for missing TIA evidence (user's global rule). DOG-014/015 verified live. Filed+fixed DOG-016 (4a5d697), push in progress. Next run-014 on 4a5d697.
