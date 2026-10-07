# run-041: TARGETED DOGFOOD EXPERIMENT (interruption + resume), on a new mission shape
Mission shape (new): Go concurrency in an existing codebase: worker pool, per-request deadlines, retries with backoff, deterministic ordering, signal handling, race-detector-clean tests.
Experiment: as a user whose terminal dies, send SIGINT to `howl orchestrate` ~90 s into implementation, then follow the documented recovery (`howl orchestrate inspect`, then `resume`). Expected: a clear interrupted state, no orphaned worker silently editing the repo, resumable session, no lost or duplicated work, correct final result.
Not counted toward the clean streak (targeted).
