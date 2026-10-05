# Mission selection rationale (run-030, adaptive tranche 1, Mission A)
Runs 001-029 all used one mission: a greenfield Python CLI for household tasks with local persistence.
This mission instead tests:
- modifying an existing repository (existing code, tests, README, module layout) rather than greenfield
- Go instead of Python (go test discovery, go module layout, gofmt)
- backward compatibility (byte-identical default output)
- time-zone-aware edge cases (instant comparison across offsets, inclusive/exclusive bounds)
- a second output format (JSON) that must agree with the existing report
- documentation that must stay accurate
Chosen as the prompt's default first adaptive mission (existing-project modification); not chosen for likely success or failure.
