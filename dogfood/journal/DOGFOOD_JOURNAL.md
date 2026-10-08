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
- 2026-10-03 DOG-016 pushed (4a5d697). run-014 COMPLETE, verified: CLEAN RUN 1/2 on 4a5d697. Starting run-015 on the same engine.
- 2026-10-03 run-015 COMPLETE + verified: CLEAN RUN 2/2 on 4a5d697. DOGFOOD RESULT: PASS. DOG-017 found from review notes, fixed separately (27a5080). PRs #142, #143, #144 opened. Checkout returned to main.
- 2026-10-03 resume check 2 (new session): #142, #146 merged; main 4c4e69b src == 4a5d697 (empty diff). Live engine checkout fast-forwarded a0ada6a -> 4c4e69b. CLI health OK. PASS remains valid; no new runs. Only open item: PR #144 (DOG-017), the user's to merge.
- 2026-10-04 PR #144 (DOG-017) merged a7f3bc4; engine src changed, PASS must be re-earned: run-016, run-017 next.
- 2026-10-04 run-016 (engine a7f3bc4) COMPLETE, 1 rework round converged, verified: CLEAN RUN 1/2. Starting run-017.
- 2026-10-04 run-017 (engine a7f3bc4) COMPLETE, verified: CLEAN RUN 2/2. DOGFOOD RESULT: PASS (run-016, run-017). FINAL_REPORT Addendum 3.
- 2026-10-04 groomed FINDINGS statuses (all 17 RESOLVED). Claude recovered via documented `howl agents doctor --live` (NEEDS ACTION -> READY). run-018 started: exploratory run with Claude in the pool (not needed for PASS).
- 2026-10-04 run-018 (exploratory, Claude READY): COMPLETE WITH WARNINGS; Claude implementation denied python3 test run in greenfield repo -> reroute -> Claude interactive-only again. DOG-018 filed; part 1 fix e821971 on dogfood/DOG-018-greenfield-denial (push in progress). Part 2 (greenfield grant policy) needs the user's decision.
- 2026-10-04 run-019 (engine 2ee6c5b, Claude READY): BLOCKED after 2 rework rounds; DOG-018 verified live (Claude planned+implemented 3 rounds, 0 denials, 0 reroutes). Filed DOG-019 (verification ignores the plan's test command).
- 2026-10-04 DOG-019 fixed 245c705 (pushed). run-020 (245c705): COMPLETE WITH WARNINGS; DOG-019 verified live; Claude refused `-t ..` -> DOG-020 (own regression from 2ee6c5b's prompt), fixed f83882d, push in progress. Next: two clean runs (run-021, run-022) on the DOG-020 engine.
- 2026-10-04 run-021 (f83882d): BLOCKED after 2 rework rounds; Claude implemented with 0 denials (DOG-018/020 verified), plan verification passed every round (DOG-019). DOG-021 filed: rework fixes only cited instances.
- 2026-10-04 DOG-021 fixed 22806e8. run-022 (22806e8): COMPLETE, Claude implemented, 2 rework rounds converged, verified: CLEAN RUN 1/2. DOG-022 (report UX) filed. Starting run-023.
- 2026-10-04 run-023 (22806e8): BLOCKED, streak reset to 0/2. DOG-023 filed: Claude->Codex review converges 1/4. Asked the user for a policy decision.
- 2026-10-04 DOG-023: user chose 'Prefer Codex implementer'; fixed 7a516c0 (push in progress). Next: run-024, run-025 on 7a516c0.
- 2026-10-04 run-024 (7a516c0): COMPLETE, verified: CLEAN RUN 1/2. Starting run-025.
- 2026-10-04 run-025 (7a516c0) COMPLETE, verified: CLEAN RUN 2/2. DOGFOOD RESULT: PASS (run-024, run-025) with Claude in the pool. FINAL_REPORT Addendum 4.
- 2026-10-04 PRs #147 and #148 merged; main 4e31e53 src == 7a516c0; PASS holds on main; engine checkout on main; howl/howlproof fast-forwarded (docs only).
- 2026-10-04 post-merge records push hit the flaky pre-push test; captured it: DOG-024 (test suite leaks factory worktrees into the real data home). Fixed test-only on dogfood/DOG-024-test-xdg-isolation.
- 2026-10-04 #149/#150 merged (#150 landed on the #149 branch, not main; carried to main by the DOG-022 PR). Removed 73 pytest-leaked worktrees (user approved). DOG-022 fixed on dogfood/DOG-022-report-superseded-verdicts; engine src changed, so run-026/run-027 re-verify.
- 2026-10-04 run-026 (37bb7b7): COMPLETE, verified: CLEAN RUN 1/2. Starting run-027.
- 2026-10-04 DOG-022 pushed (37bb7b7). run-027 (37bb7b7): BLOCKED after Codex budget overrun -> DOG-025 (workers re-run Howl workflow; session manifest with fence token copied into repo). Streak 0/2.
- 2026-10-04 DOG-025 fixed 5a806e3 (pushed). run-028 (5a806e3): COMPLETE, verified: CLEAN RUN 1/2. Starting run-029.
- 2026-10-04 run-029 (5a806e3) COMPLETE, verified: CLEAN RUN 2/2. DOGFOOD RESULT: PASS (run-028, run-029). FINAL_REPORT Addendum 5.
- 2026-10-04 PR #151 merged (e74e460); main src == 5a806e3; PASS holds on main; howlframe fast-forwarded; all agents READY.

## Adaptive tranche 1 (2026-10-04)
- Resume check: main 41a1621, `git diff 5a806e3 41a1621 -- src pyproject.toml` empty -> baseline PASS (028/029) holds; no baseline rerun. All 12 ecosystem repos clean and current. Records branch dogfood/adaptive-tranche-1. MISSION_COVERAGE.md created.
- run-030 (41a1621) Mission A, existing Go project logsum (+since/until, -format json): COMPLETE in 4.5 min, Claude plan/review, Codex impl, first-pass CLEAN; independently verified (byte-identical default output vs pre-change binary, cross-offset windows, README JSON example exact). CLEAN. No findings; observation OBS-030-1 (no public cleanup for factory worktrees).
- run-031 (41a1621) Mission B, existing Python HTTP/SQLite service, 3 symptom-only bug reports + automatic DB upgrade: started.
- run-031 (41a1621): COMPLETE 5.0 min, first-pass CLEAN; verified (legacy DB upgraded in place, 100-way concurrent POST all 201/unique, exact tags, stable newest-first paging). CLEAN.
- run-032 (41a1621) Mission C, Node invoicegen config refactor with uncommitted user WIP: HANDOFF REQUIRED; `npm test` 1/59 failed and the session stopped with no rework, output truncated, Failures: []. User WIP preserved (sha256). DOG-026 filed (P1).
- DOG-026 fixed 744bc4a (failed verification -> bounded rework; failure-first excerpts; run_verification guards OSError/timeout; Blocked by + Next in report/inspect). Pushed, pre-push 2321 passed. Live engine switched to the branch. run-033 (same mission, identical start) started.
- run-033 (744bc4a): COMPLETE 8.5 min, CLEAN; verification passed first time (DOG-026 not exercised). Resume of run-032's paused session on 744bc4a: verification failed -> REWORK round 1 -> fixed -> COMPLETE. DOG-026 verified live.
- Resume review notes: reviewer had no shell, could not diff vs HEAD -> DOG-027. Building its test exposed DOG-028 (HowlPlane's git status/diff run core.fsmonitor/textconv/diff.external from .git/config). Fixed both in 175b57a (stacked), pushed, pre-push 2327 passed. Live engine -> 175b57a.
- run-034 (175b57a) Mission D, test-only characterization of legacy shopcalc/pricing.py (must stay unchanged): started.
- run-034 (175b57a) test-only characterization: COMPLETE 5.8 min, CLEAN; pricing.py untouched, 91 tests, 15/15 mutants killed. DOG-027 verified live (reviewer confirmed scope from HowlPlane's diff).
- run-035 (175b57a) greenfield Rust ini2json: COMPLETE 6.6 min, CLEAN; 19/19 operator oracle cases. Final adaptive pair clean on 175b57a. Next: household baseline fixture x2 on 175b57a (engine changed since the last baseline PASS), as run-036/run-037.
- run-036, run-037 (175b57a) household baseline fixture: both COMPLETE, verified CLEAN -> baseline restored on the new engine.
- run-038 TARGETED (DOG-028): fsmonitor logging hook in the target repo; 4 calls from Codex's sandbox, 0 from HowlPlane. DOG-028 verified live.
- PRs opened: #153 (DOG-026), #154 (DOG-027/028, stacked). ADAPTIVE DOGFOOD TRANCHE 1: PASS. Live engine checkout returned to main.

## Adaptive tranche 2 (2026-10-05)
- Post-merge check: #153, #155 on main; #154 landed on its stack base, not main (175b57a missing). Opened #156 with only 175b57a. Live engine kept on 175b57a (= main + #156).
- run-039 (175b57a): large multi-user/auth/migration change to the bookmarks service, started.
- run-039: large multi-user change, deliverable correct; goal rewritten by redaction -> DOG-029 (fixed, #157). run-040 rerun: DOG-029 verified live; reviewer saw redacted diff -> DOG-030 (fixed on #156).
- run-041 TARGETED: Ctrl-C mid-implementation, clean checkpoint, resume COMPLETE.
- run-042 ecosystem creative pipeline: DOG-031 (howl doctor false HEALTHY; howl#15), DOG-032 (installer gap, open), DOG-033 (hidden review items; fixed). Pipeline COMPLETED with intact lineage.
- 2026-10-07: tranche 2 STOPPED at the user's request. Engine checkout returned to main.
- 2026-10-07 user: "push and merge everything". Merged #157, #156 (after update-branch + CI), howl#15. DOG-033 first push failed the pre-push SlopsLint clone ceiling (my duplicated test helper); deduplicated, pushed 4d3a214, PR #158.
- 2026-10-07 follow-ups (user): (1) `howl update` -> "up to date" (no howl releases); rebuilt howl from main c9d37d5 and installed it, old binary kept as howl.prev; doctor HEALTHY. (3) DOG-032 option B implemented, verified live (preflight 9/9, pipeline 7/7 via component CLIs), merged #160. (5) Workaround packages removed from the engine runtime. (4) Hook policy: DOG-034 guard implemented (user hooks kept; hooks changed mid-task block the commit).

## Clean pair on main (2026-10-08)
- Preconditions: howlplane checkout on main bb7ba21 == origin/main, clean; howl doctor HEALTHY; 5 agents READY; no open PRs in howlplane or howl. Records branch dogfood/clean-pair-20261008.
- run-043 (bb7ba21) existing Python pingbot, credential-heavy request + uncommitted WIP: COMPLETE 5.1 min, review CLEAN first pass; 21 tests, 26/26 probes; goal byte-identical, no redaction markers; WIP sha256 intact; reviewer scoped by HowlPlane's diff and quoted token-shaped literals unmasked. CLEAN 1/2.
- run-044 (bb7ba21) existing TypeScript relnotes, git integration: COMPLETE 8.8 min, review CLEAN first pass; 16 tests, 29/29 probes (two probe iterations failed on my own fixture, fixed). CLEAN 2/2.
- Clean pair on main bb7ba21. No new finding. DOG-026 rework and DOG-034 did not fire.
