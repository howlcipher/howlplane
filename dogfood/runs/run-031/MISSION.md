Bug report for this bookmarks service. We run it in production with several clients, and users have reported three problems:

1. When a few clients save bookmarks at the same moment, some of the requests fail with a 500 error or the connection is dropped, and the bookmark is lost. It works fine when we test by hand one request at a time.
2. Filtering with `GET /bookmarks?tag=py` also returns bookmarks tagged only `python` (and `?tag=go` returns `golang` ones). A tag filter should match the exact tag.
3. The README says `GET /bookmarks` is newest first, but bookmarks saved within the same second come back oldest first, so a client that just saved several bookmarks sees them in the wrong order and pagination boundaries look inconsistent.

Please investigate, find the root causes, and fix them. Requirements:
- Keep the HTTP API and JSON shapes backward compatible for existing clients.
- Our existing `bookmarks.db` files, created by the current version, must keep working after the upgrade with no data loss and no manual steps. If the schema has to change, the service must upgrade an old database automatically on startup.
- Add regression tests for each problem, including one that exercises concurrent requests against the real HTTP server, and keep the existing tests passing.
- Update the README if behaviour or operations change.
