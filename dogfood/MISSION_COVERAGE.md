# Mission Coverage

Prevents repetitive dogfood. One row per run; adaptive tranches start at run-030.

## Runs

| Run | Mission | Greenfield/Existing | Language | Capability | Result | New defect class? |
| --- | --- | --- | --- | --- | --- | --- |
| 001-029 | Household recurring-task CLI (baseline fixture) | Greenfield | Python | CLI, local persistence, tests, docs | PASS (028/029 latest clean pair) | DOG-001..025 |
| 030 | logsum: time-window filter + JSON output | Existing | Go | existing-code modification, compatibility, timezone edge cases, docs | CLEAN | no |
| 031 | bookmarks API: 3 user-reported bugs (concurrency, tag match, ordering) | Existing | Python | bug investigation, HTTP API, concurrency, DB migration | in progress | |

## Capability matrix

- [x] greenfield CLI (001-029)
- [x] existing-code modification (030, 031)
- [ ] bug repair (031)
- [ ] refactor
- [x] persistent storage (001-029)
- [ ] data migration (031)
- [ ] REST/API (031)
- [ ] file transformation
- [ ] concurrency (031)
- [ ] configuration
- [x] multi-module change (030)
- [x] compatibility preservation (030)
- [ ] test-only task
- [x] documentation-sensitive task (030)
- [ ] error-recovery task
- [ ] integration task
- [x] non-Python language (030: Go)
