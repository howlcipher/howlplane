# run-044 independent verification (operator, 2026-10-08)
Engine: howlplane main bb7ba21. Session a1136123: Claude plan, Codex implementation (7 min), Claude review (CLEAN, first pass), Claude acceptance. 8.8 min, exit 0, 0 reroutes, rework rounds 0. Verification: `npm test`, derived from the project.

## Deliverable
- `npm test` (tsc strict + node:test): 16/16 pass. test/notes.test.ts untouched (`git diff 1a1b8ec -- test/notes.test.ts` empty). New src/git.ts and test/repo.test.ts (real temporary repos with their own identity, dates and a null global config).
- Behavioural probe runs/run-044/verify044.sh (built CLI against a fresh real git repo with tags, a --no-ff merge, both breaking forms, an indented decoy footer, and a subject with `|`, a tab, quotes and non-ASCII text): 29/29 PASS:
  - range v1.0.0..v1.1.0 = `git log FROM..TO` minus merges, oldest first; merged-branch commit included; merge commit skipped; base commit excluded; entries end with the matching 7-char hash; scope bold; awkward subject byte-intact; no stderr.
  - Breaking changes first: `feat!:` and a `BREAKING CHANGE:` footer each listed once there; an indented footer and a subject containing "BREAKING CHANGE:" do not count.
  - default `--to` == `--to HEAD`; empty range -> `## 2.0.0\n\nNo changes.`, exit 0.
  - exit 2 with one stderr line: unknown --from / --to (ref named), non-repo, missing path, git absent from PATH (`relnotes: git is not installed`).
  - `--from 'v1.0.0;touch pwned'` -> exit 2, no file (no shell); `--from=--output=<file>` -> exit 2, no file written (option injection guarded by `--end-of-options`).
  - usage errors exit 2: --repo without --from, --input with --repo, neither.
  - file input: `!` rule applied, no hashes; output without breaking commits unchanged from 0.3.0.
  - (Two probe iterations failed on my fixture: the topic branch conflicted on the same file, so `feat!:` became the merge commit and was correctly skipped. Fixture fixed; not a deliverable defect.)
- README: new mode, range semantics, section order, breaking rules, every error message and the git version requirement; each claim checked above or by reading src (`cannot read git history` path read only). `fix(api)!:` example verified.

## Howl-specific checks
- Goal: report Goal equals MISSION.md byte for byte; zero redaction markers in stdout/stderr.
- Reviewer diff (DOG-027): notes cite "the README diff", list changed and new files, and state test/notes.test.ts is untouched.
- Repo: HEAD 1a1b8ec unchanged, nothing committed; only the 5 expected paths changed; no Howl state; dist/ and node_modules/ ignored as before.
- Out of scope, did not fire: DOG-026 rework, DOG-034.

Reviewer non-blocking notes judged: (2) a bare `feat!:` subject renders as raw text under Breaking changes, an edge the request does not define; (4) a misleading message for a non-GitError exception in repo mode, unreachable in practice. Neither violates a requirement.

Verdict: CLEAN.
