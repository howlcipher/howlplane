# Campaign State

Updated: 2026-10-02 (initialization)

## Repositories (all fetched; none behind upstream)
| Repo | Path (under /run/media/system/tallgeese/dev) | Branch | HEAD | Remote | Dirty? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| howl | howl | main | 8cb157d | github.com/howlcipher/howl | no | installer CLI, `howl` v0.1.1 |
| howlplane | howlplane | main (campaign branch dogfood/campaign-household-tasks) | 83b117c | github.com/howlcipher/howlplane | no | orchestration, owns `howl orchestrate` |
| howlframe | howlframe | main | 5a229d6 | github.com/howlcipher/howlframe | no | |
| howldream | howldream | main | f1cc5ca | github.com/howlcipher/howldream | no | |
| howlcreate | howlcreate | main | bab145a | github.com/howlcipher/howlcreate | no | |
| howlforge | howlforge | main | 067aee9 | git@github.com:howlcipher/howlforge | no | |
| howlInstinct | howlInstinct | main | cb211db | git@github.com:howlcipher/howlInstinct | no | |
| howlwriter | howlwriter | main | e2758da | github.com/howlcipher/howlwriter | no | |
| howlboard | howlboard | main | cd2f2fd | github.com/howlcipher/howlboard | no | |
| howlproof | howlproof | main | 56ecea6 | github.com/howlcipher/howlproof | no | |
| howlrelay | howlrelay | main | 7d6b600 | github.com/howlcipher/howlrelay | no | |

Other agents' worktrees exist (howlplane/.worktrees/*, howlplane-factory-reconcile on feat/factory-milestone-five-controller, several `howl-*-hardening-*` dirs, worktrees/*). Do not touch them.

## Installed ecosystem (`howl status`)
Howl 0.1.1; HowlFrame, HowlChangeOps, HowlPlane (+ control-plane engine), HowlWriter all 0.1.0, up to date.

## Prior art
`howl-creative-dogfood/run01..07` and `howlplane/documentation/DOGFOOD_MISSION_001.md` are earlier, different campaigns. Not resumed.

## Runs
| Run | Status | Notes |
| --- | --- | --- |
| run-001 | FAILED | HANDOFF REQUIRED, DOG-001 |
| run-002 | FAILED | HANDOFF REQUIRED, DOG-002 (DOG-001 verified) |
| run-003 | COMPLETE, verified, but NOT COUNTED | same latent DOG-003 defect; streak reset |
| run-004 | FAILED | HANDOFF REQUIRED, DOG-003 blocked acceptance (b07a4e0) |
| run-005 | CLEAN RUN 1/2: PASS | COMPLETE, verified, engine @82d3cde |
| run-006 | CLEAN RUN 2/2: PASS | COMPLETE, verified, same engine src |

## RESULT
DOGFOOD RESULT: PASS (run-005 and run-006). See FINAL_REPORT.md.

## Resume check (2026-10-03, new session)
All campaign PRs merged: howlplane #139, #140, #141 (main a0ada6a); howl #14 (main 77482cb).
`git diff aedf82f a0ada6a -- src` is empty, so main's engine source is byte-identical to the engine that produced clean runs 009 and 010. The installed engine is an editable install of howlplane/src (runtimes/howlplane-engine venv `.pth`), and the checkout is on main.
Repository survey: all 12 Howl repos clean on main, none ahead; howlframe fast-forwarded 5a229d6 -> 9c73d28 (not used by this mission). No new runs were needed to keep the PASS valid.

## Runs (follow-up campaign 2, 2026-10-03)
| Run | Engine | Status | Notes |
| --- | --- | --- | --- |
| run-011 | 9a9a59c (DOG-012) | BLOCKED | rework fired live, never converged -> DOG-013, DOG-014 |
| run-012 | 82414bf | COMPLETE, verified (clean) | CLEAN notes invisible -> DOG-015; engine changed, streak reset |
| run-013 | a1d8ce2 | HANDOFF REQUIRED | acceptance rejected for missing TIA evidence -> DOG-016 |
| run-014 | 4a5d697 | CLEAN RUN 1/2: PASS | |
| run-015 | 4a5d697 | CLEAN RUN 2/2: PASS | |

## RESULT (campaign 2)
DOGFOOD RESULT: PASS (run-014, run-015, engine howlplane dogfood/DOG-013-review-convergence 4a5d697). PRs #142 (DOG-012), #143 (DOG-013..016, stacked), #144 (DOG-017, independent). None merged.

## Resume check 2 (2026-10-03, new session)
Merged since the campaign 2 PASS: howlplane #142 (DOG-012), #146 (DOG-013..016; replaced closed #143), #145 (records). Still open: #144 (DOG-017, `howlplane route` only, not on the mission path).
`git diff 4a5d697 origin/main -- src pyproject.toml` is empty: main @4c4e69b is byte-identical in engine source to the engine that produced clean runs 014 and 015.
The live engine checkout (howlplane, editable install via runtimes/howlplane-engine `.pth` -> howlplane/src) was 17 commits behind and has been fast-forwarded to 4c4e69b. Clean tree, no local work lost.
Public CLI health: `howl --help` OK; `howl agents doctor --repo dogfood-missions/run-015/household-tasks` exit 0. `python3 -m pip check`: only version-pin conflicts (litellm, mcp), no missing packages.
Other repos (howl 77482cb, howlforge, howlcreate, howldream, howlproof): clean on main, up to date.
No new runs were needed: the PASS (run-014, run-015) holds for main.
