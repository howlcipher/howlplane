# Task Journal: Rotate the evidence ledger and stop tests writing to real ledgers

## Summary

- **Task:** Hotfix without a backlog row. `logs/control_plane/evidence_ledger.jsonl` reached 422 MB with no rotation; investigation found 99% of it is test fixture records.
- **Status:** In progress
- **Started:** 2026-10-28
- **Agent and model:** Claude Code / Opus 5.5

## Pre-Flight Re-Evaluation

- **Model choice:** Implemented directly in Claude Code rather than delegated to agy: the change is concurrency sensitive (`flock`, atomic rename, crash recovery) in an append-only ledger that the completion gate and bootstrap authority read, and the owner asked this session to carry it through to merge.
- **Skills routed:** `python`, `software_development`, `quality_assurance`, `test_and_verify`.
- **Free tools:** Standard library only (`gzip`, `fcntl`, `shutil`); nothing to install.

## Plan

- [x] Measure the ledger: 444,142 lines, 99.1% fixture records; `TEST-FAILOVER-01` alone 332,241 (stopped 2026-09-28). A full run on origin/main still leaked 319 entries into the checkout and 19 into the operator's configured HowlPlane.
- [x] Rotation: size threshold, gzip archives, directory `flock`, crash safe staging, reads span archives.
- [x] Doctor and metrics diagnostics name the segment; doctor validates archive only history.
- [x] `resume` records into the ledger it was given (two internal orchestrators wrote to their default ledger).
- [x] Repository fingerprints ignore rotated segments (`is_ledger_file`).
- [x] Session guard in `tests/conftest.py`; leaking tests use temporary ledgers or `tests/_control_plane_home.py`.
- [x] Docs: README, change_log, data_flows, CONTROL_PLANE, TESTING.
- [ ] `make test-full` on the final tree, TIA record, PR, merge.
- [ ] After merge: fast forward the main checkout, then rotate the live ledger.

## Progress Log

- 2026-10-28 — Baseline on origin/main 24ac9ad: 2641 passed, 1 skipped (compiler from `scripts/fetch_frame_artifact_compiler.sh`).
- 2026-10-28 — Guard run found 43 leaking tests; all fixed. A sidecar `.lock` file broke `test_proposal_decision` and approval fingerprints, so the lock moved to the ledger's directory.
- 2026-10-28 — Checkpoint full suite: 2654 passed, 0 bytes written to the operator ledger; the one failure was slopslint (`python_tests` 4 clones over a ceiling of 2) from repeated orchestrator construction in `test_closed_loop_orchestrator.py`, fixed with a local `_orchestrator` helper (back to 2). `make lint` clean.

## Next Step

Delete this journal, push (the pre-push hook runs `make test lint build docs` on the final tree), open the PR, merge after CI.
