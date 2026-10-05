# run-038 result — TARGETED DOGFOOD EXPERIMENT (DOG-028), not counted in the clean streak
Status: COMPLETE (exit 0, audit CLEAN); app verified (unittest OK).
Experiment: target repo had core.fsmonitor set to a hook logging the process that invoked git.
- Control before the run: plain `git status` invoked the hook; engine `evidence()` (175b57a) did not.
- During the public-CLI session: 4 hook invocations, all with caller `codex exec ... --sandbox workspace-write` (the agent's own git use inside its sandbox); 0 from HowlPlane's process, despite a checkpoint fingerprint (git status + git diff) at every stage.
Result: DOG-028 verified through the public CLI. Observation (not a Howl defect): the target repo had no .gitignore, so __pycache__/ is left untracked.
