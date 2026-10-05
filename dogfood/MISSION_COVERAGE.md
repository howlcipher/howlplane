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
