# run-030 independent verification (operator, 2026-10-04)
Target: dogfood-missions/run-030/logsum, base de86f10, changes delivered uncommitted (same as prior runs).
- gofmt clean; `go vet ./...` clean; `go test -count=1 ./...` 3 packages ok (3 new test files, 211 lines).
- Backward compatibility: pre-change binary (built from de86f10) vs new binary, stdout+stderr+exit code identical for: file arg, stdin, `-top 0`, `-top 2`, `-top -1`, missing file, two file args, no args with empty stdin.
- Window (sample log, UTC instants 20:55:36, 21:00:01, 21:00:02, 00:15:00(+0200 source), 09:30): `-since 21:00:00Z` -> 4 req/1152 B; `-until 21:00:00Z` -> 1; `-since 21:00:01Z -until 00:15:00Z` -> 2 (until exclusive); `-since 02:15:00+02:00 -until 00:15:01Z` -> 1 (cross-offset instant match); since inclusive at exact instant; equal bounds -> 0. malformed stays 1 in every window.
- JSON: valid; keys requests, bytes, malformed, status[], top_paths[] in text order; empty window -> `[]` arrays; `-top 0` -> empty top_paths.
- Errors: bad/empty/lowercase timestamps, date-only, since>until, `-format xml|JSON|''` all exit 2 with a message naming the flag; validated before the file is opened.
- README: usage, flags, semantics, exit codes accurate; its JSON example matches real output byte for byte; its window example works.
- Leak scan: no howl/howlplane/lease/fence/session text, no JSON or session files in the work tree.
- Code quality note (non-blocking, reviewer also noted): main.go has a second filtering pass for explicit zero-instant bounds; correct and tested, convoluted.
Verdict: all requirements met. CLEAN.
