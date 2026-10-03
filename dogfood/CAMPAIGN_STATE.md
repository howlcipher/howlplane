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
