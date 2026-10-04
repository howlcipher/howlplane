# run-019 — BLOCKED (engine 2ee6c5b, DOG-018 parts 1+2), not a clean run
Setup: `howl agents doctor --live --agent claude_code --repo <target>` (READY), then the standard mission. Exit 2, Status BLOCKED, Resumable yes, Reroutes 0.
DOG-018 verified live: Claude planned (VERIFY_COMMAND parsed: python3 -m unittest discover -s tests -v) and ran 3 implementation rounds with no permission denial, no reroute, no interactive-only marking.
Blocked because each Codex review found new BLOCKING edge cases (corrupt records, PermissionError handling, README exit codes); every round fixed the previous set; 2/2 rework rounds spent. HowlPlane reported this truthfully and printed the resume command.
New finding DOG-019: harness verification ran only `git diff --check`; no verify command was derivable from a plain-unittest layout and the plan's VERIFY_COMMAND was not used, so the reviewer (read-only sandbox, tests errored on temp files) had no test evidence: "Persistence remains unverified".
