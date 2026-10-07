# Mission Coverage

Prevents repetitive dogfood. One row per run; adaptive tranches start at run-030.

## Runs

| Run | Mission | Greenfield/Existing | Language | Capability | Result | New defect class? |
| --- | --- | --- | --- | --- | --- | --- |
| 001-029 | Household recurring-task CLI (baseline fixture) | Greenfield | Python | CLI, local persistence, tests, docs | PASS (028/029 latest clean pair) | DOG-001..025 |
| 030 | logsum: time-window filter + JSON output | Existing | Go | existing-code modification, compatibility, timezone edge cases, docs | CLEAN | no |
| 031 | bookmarks API: 3 user-reported bugs (concurrency, tag match, ordering) | Existing | Python | bug investigation, HTTP API, concurrency, DB migration | CLEAN | no |
| 032 | invoicegen: centralize config, JSON config file, precedence, validation; uncommitted user WIP present | Existing (dirty tree) | Node.js | refactor, configuration, dirty working tree | HANDOFF | DOG-026 (verification not reworked); DOG-027/028 found on resume |
| 033 | same as 032, identical start, engine 744bc4a | Existing (dirty tree) | Node.js | regression | CLEAN | no |
| 034 | shopcalc: characterization tests only, source must not change | Existing | Python | test-only, legacy behaviour, determinism | CLEAN (15/15 mutants killed) | no |
| 035 | ini2json: INI to JSON converter | Greenfield | Rust | file transformation, parsing, encoding, error semantics, offline build | CLEAN (19/19 oracle cases) | no |
| 036 | household baseline fixture | Greenfield | Python | baseline regression on 175b57a | CLEAN 1/2 | no |
| 037 | household baseline fixture | Greenfield | Python | baseline regression on 175b57a | CLEAN 2/2 | no |
| 038 | TARGETED: DOG-028 fsmonitor hook, small calc change | Existing | Python | security boundary experiment | COMPLETE; 0 HowlPlane-invoked hooks | no |
| 039 | bookmarks: multi-user, API tokens, auth, isolation, upgrade | Existing | Python | large cross-cutting change, security semantics | deliverable OK; NOT CLEAN | DOG-029 |
| 040 | 039 rerun, identical start | Existing | Python | regression | deliverable OK; NOT CLEAN | DOG-030 |
| 041 | TARGETED: linkcheck concurrency + Ctrl-C mid-implementation + resume | Existing | Go | concurrency, interruption/resume | resume COMPLETE, deliverable OK | no (P3 hint wording) |
| 042 | creative pipeline: linkcheck landing page (Dream -> Writer -> Create) | Greenfield | ecosystem | cross-component handoff, provenance | COMPLETED; NOT CLEAN | DOG-031, DOG-032, DOG-033 |

## Capability matrix

- [x] greenfield CLI (001-029)
- [x] existing-code modification (030, 031)
- [x] bug repair (031)
- [x] refactor (032/033)
- [x] persistent storage (001-029)
- [x] data migration (031: automatic legacy DB upgrade)
- [x] REST/API (031)
- [x] file transformation (035)
- [x] concurrency (031)
- [x] configuration (032/033)
- [x] multi-module change (030)
- [x] compatibility preservation (030)
- [x] test-only task (034)
- [x] documentation-sensitive task (030)
- [x] error-recovery (032 resume after engine upgrade)
- [ ] integration task
- [x] non-Python language (030: Go)
- [x] dirty working tree / uncommitted user WIP (032/033)
- [x] greenfield non-Python (035: Rust)
- [x] interruption / resume (041)
- [x] ecosystem integration (042: HowlDream -> HowlWriter -> HowlCreate)
- [x] large cross-cutting change (039/040)
