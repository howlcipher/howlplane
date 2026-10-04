# run-027 — BLOCKED (engine 37bb7b7), not clean; streak on 37bb7b7 reset to 0/2
Claude planned; Codex implementation hit the 600 s budget (EXECUTION_BUDGET_EXCEEDED; Codex implementations in runs 001-026 took 134-430 s) -> reroute to Claude -> Claude implemented, Codex reviewed, 2/2 rework rounds, BLOCKED (DOG-023 pattern on the fallback path).
DOG-022 verified live: the report labelled the two earlier verdicts "rework round N: sent back to implementation and re-judged" and left the final one unlabelled.
Cause of the overrun (DOG-025): Codex tried to perform "the normal Howl workflow" itself: it ran `howlplane route` (documentation/howl_route.txt) and copied HowlPlane's live session manifest, including the lease fence token, into the user's repository (documentation/howl_workflow.json).
