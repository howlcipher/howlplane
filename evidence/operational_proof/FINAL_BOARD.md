# Final truthful Board projection and API readback

Final ledger: `worktrees/howl_operational_proof/evidence/operational_proof/board_mission.jsonl`. Actual supported importer ran against a new isolated `board_final_state`, followed by reimport of the coordinator's updated final result sentence while the server was stopped. Exact command, ledger hash, store hashes and snapshots: `board_final_projection.json`.

Observed state FAILED (the importer's available noncompletion state), app verification passed exit 0 and HowlProof verification failed exit 30, authority REQUIRE_APPROVAL, manual projection recording mode. No approval was inferred. Timeline explicitly states overall PARTIALLY VERIFIED, native independent review BLOCKED, acceptance unexecuted, HowlProof unresolved, governed release BLOCKED and two-store publication limitation. The importer does not project arbitrary task_failed verification_summary; the coordinator put the final explanatory sentence in result so it survives timeline projection.

Actual compiled backend API readback also executed: `board_api_readback.json` contains `/api/missions`, `/api/missions/get` (JSON body via POST), and `/api/timeline` responses. All showed the imported mission and the explicit partial/blocked explanation. No browser rendering was tested.

The backend was compiled with Board's supported Frame source pin, then run transiently inside `bwrap --unshare-net`, with read-only root, writable isolated evidence directory, private temporary storage and no outbound network. Explicit capabilities match Board's documented server grants. The subprocess was terminated and waited after readback; no persistent service or deployment remains. Reproducer: `board_api_probe.py` inside the same namespace wrapper. `board_api_server.log` records the real server startup.

Two initial readback harness errors were corrected: omitted scoped fetch grants caused capability denial, and sending id as a query instead of the supported JSON body returned HTTP 400. Neither was treated as product success. The final run exited 0. Source and application files were not modified.
