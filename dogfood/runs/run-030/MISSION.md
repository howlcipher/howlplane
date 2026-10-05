In this existing Go project (logsum, a Common Log Format access-log summarizer), add time-window filtering and JSON output.

1. Add `-since` and `-until` flags that restrict the summary to requests inside a time window. Both accept RFC 3339 timestamps (for example `2000-10-10T21:00:00Z` or `2000-10-11T02:15:00+02:00`). `-since` is inclusive and `-until` is exclusive. Log lines carry their own UTC offsets, so compare instants, not wall-clock text. Either flag may be used alone.
2. Add a `-format` flag accepting `text` (the default, the current report) and `json`. The JSON report must contain the request count, byte total, malformed count, per-status counts, and the top paths in the same order as the text report, and must be valid JSON.

Constraints:
- With no new flags, output must stay byte-for-byte identical to today's output.
- Malformed lines are still counted as malformed whether or not a window is given; well-formed lines outside the window are excluded, not counted as malformed.
- Invalid values (an unparseable timestamp, `-since` later than `-until`, an unknown format) must exit with code 2 and a clear message on stderr, consistent with the existing usage errors.
- Keep the existing tests passing and add tests for the new behaviour, including edge cases.
- Update README.md so it accurately documents the new flags and the JSON output.
