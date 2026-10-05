# Mission selection rationale (run-031, adaptive tranche 1, Mission B)
Recent runs: 001-029 greenfield Python CLI; 030 existing Go CLI feature addition (spec given precisely).
This mission instead tests:
- bug investigation from user-reported symptoms (no file/line pointers; root causes must be found)
- an HTTP/REST service (ThreadingHTTPServer) rather than a CLI
- concurrency (race in MAX(id)+1 id allocation under threaded requests)
- data compatibility: existing on-disk SQLite databases must survive any schema change (migration)
- exact-match semantics (tag filter) and ordering ties (second-resolution timestamps)
All three symptoms were reproduced by the operator before submission (24/30 concurrent adds raised IntegrityError; tag=py matched 25/25 instead of 13; same-second ids listed [1..5] ascending).
