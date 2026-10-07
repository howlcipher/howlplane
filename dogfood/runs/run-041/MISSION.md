linkcheck checks URLs one at a time, which takes minutes for our 2,000-link docs site, and it hangs forever when a server never responds. Please make it concurrent and robust:

1. Add `-workers N` (default 8, must be at least 1) to check URLs concurrently.
2. Add `-timeout D` (a Go duration, default 10s) as a per-request deadline. A request that exceeds it is reported as a failure, not a hang.
3. Retry transient failures: network errors, timeouts and 5xx responses get up to `-retries N` extra attempts (default 2) with exponential backoff starting at 200ms. 4xx responses are never retried.
4. Output must stay deterministic: one line per URL in input order, then the summary, regardless of which request finishes first. Keep the existing line formats and exit codes; a timeout or network error keeps the `FAIL ERR <url> (<reason>)` form.
5. Duplicate URLs in the file are checked once but still listed once per occurrence, in order.
6. Pressing Ctrl-C (SIGINT) stops outstanding requests promptly, prints the results gathered so far plus the summary, and exits 130.
7. Tests must not use the network: use httptest, and cover concurrency (prove requests overlap and that the worker limit holds), timeouts, retry counts and backoff, no retry on 4xx, ordering, duplicates, and the existing behaviour. Keep `go vet` and `go test -race ./...` clean. Update the README.
