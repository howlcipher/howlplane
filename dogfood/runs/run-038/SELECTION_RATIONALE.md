# run-038: TARGETED DOGFOOD EXPERIMENT (not part of the normal-user clean streak)
Purpose: prove DOG-028 through the public CLI. The target repository has `core.fsmonitor` configured (as Watchman users do) to a logging hook that records which process invoked git. Pass: no hook invocation whose caller is HowlPlane's own process (agents' own git calls inside their sandboxes may appear). Control: plain `git status` fires the hook; engine evidence() does not (checked before the run).
Mission deliberately small; its result is verified but only the hook log is the experiment's evidence.
