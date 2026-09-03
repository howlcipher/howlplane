# Testing Strategy

HowlPlane optimizes for useful defect detection per developer and CI minute.
Plain `pytest` and `make test-full` remain full regression gates; no marker is
silently excluded from the default suite.

## Tiers

| Tier | Meaning | Typical use |
| --- | --- | --- |
| `unit` | Fast isolated contract with no live boundary | Inner-loop behavior and error paths |
| `integration` | Real HowlPlane components with Git, providers, or processes faked | Subsystem changes |
| `acceptance` | In-process governed capability, synthesis, dogfood, or recovery flow | Important end-to-end capability |
| `slow` | Orthogonal measured runtime marker | Explicit local run, full PR gate, nightly |
| `live` | Intentional real provider, GitHub, or remote-service check | Explicitly enabled nightly or release only |

`tests/conftest.py` assigns exactly one primary tier during collection. There
are currently no intentionally live pytest tests; the marker exists to prevent a
future live check from entering normal pytest or ordinary CI accidentally.

### How `slow` is decided

`slow` is applied at the granularity the measurement supports, not per module.
A whole module is marked slow only when the family is uniformly expensive
(`_SLOW_MODULES`); where one or two tests dominate an otherwise cheap module,
those tests are named individually (`_SLOW_TESTS`). Marking a module slow to buy
back a single expensive test is what silently removes its cheap authority,
security, isolation, and durable-recovery assertions from `make test-fast`.

Recorded profile (`pytest --durations=0`, 1290 tests, 271.7s total, 254.1s
attributable to tests):

| Kept slow as a whole module | Cost |
| --- | --- |
| `test_provider_failover.py` | 87.8s / 66 tests, uniformly ~1.3s |
| `test_effective_implementer_identity.py` | 9.2s / 6 tests, ~1.5s each |
| `test_clean_environment_regression.py` | 8.1s / 6 tests, end-to-end |
| `test_scratch_isolation.py` | 7.2s / 6 tests, uniformly ~1.2s |
| `test_reasoning_strategy_dogfooding.py` | 5.8s / 8 tests |
| `test_howlframe_dogfood.py` | 5.2s / 8 tests |
| `test_closed_loop_orchestrator.py` | 5.0s / 7 tests |

| Marked per test instead | Module cost | Dominated by |
| --- | --- | --- |
| `test_docs.py` | 19.5s / 8 | `test_pdoc_api_generation` (19.5s) |
| `test_orchestrator.py` | 21.6s / 26 | 8 `run_loop` tests (~18.7s); `human_proxy_intercept` is free |
| `test_interrupted_governance_recovery.py` | 16.0s / 41 | 8 tests (~14.9s) |
| `test_langchain_compat.py` | 8.1s / 1 | its single test |
| `test_hygiene_policy.py` | 7.6s / 18 | 1 test (6.3s); the ATTACK A-F suite is free |
| `test_verification_view.py` | 6.4s / 22 | 3 tests (~5.3s) |
| `test_provider_preflight.py` | 6.6s / 14 | 2 `payload_loop` tests |
| `test_doctor.py` | 4.3s / 10 | 2 tests (~4.0s) |
| `test_install_global_codex.py` | 4.3s / 1 | its single test |
| `test_operational_resilience.py` | 4.4s / 26 | 1 test (3.1s) |
| `test_git_env_isolation.py` | 3.1s / 20 | 1 test (1.7s) |

`tests/test_test_taxonomy.py` fails if any entry stops resolving to a real
module or test, so a rename cannot silently drop a test from its tier or let an
expensive test rejoin the fast gate under a new name.

Re-profile with `pytest --durations=0` and update both sets when the numbers
move.

## Local commands

| Goal | Command |
| --- | --- |
| Fastest change feedback | `make test-changed` |
| Explain selection | `python scripts/select_relevant_tests.py --from-ref origin/main --dry-run` |
| Fast normal verification (Python + Go) | `make test-fast` |
| Fast Python only | `make test-fast-python` |
| Tier-specific checks | `make test-unit`, `make test-integration`, `make test-acceptance` |
| Measured expensive checks | `make test-slow` |
| Full PR or final gate | `make test-full` |
| Branch coverage | `make test-coverage` |
| Runtime visibility | `python -m pytest --durations=25` |

`test-changed` maps a changed source to a conventional corresponding test and
to tests importing or exercising its module. Changed tests run directly.
Agent and skill changes run their policy and manifest checks. Test
configuration, CI, central authority/orchestration seams, Go changes, and
unknown executable paths deliberately fall back to `tests/`. Documentation-only
changes select no application regression. Renames and deleted paths are
expanded from Git name-status output so their previous behavioral contract is
still considered. The selector prints every changed path, selected test, reason,
and fallback decision.

## Continuous Test Impact Assessment

Before finishing every code-changing task, answer and act on:

1. What observable behavior changed, and which current tests prove it?
2. Do those tests still represent intended behavior?
3. Is a happy-path, failure, edge, durability, or authority test missing?
4. Did a test become obsolete or duplicate another behavioral contract?
5. What is the smallest appropriate tier, and are integration or acceptance
   tests also warranted?
6. Which exact tests ran, and why was the broader gate sufficient?

For bugs, reproduce the failure, implement the correction, pass the targeted
test, then run relevant regression. For new behavior, define and test the
observable contract before implementation. Do not add tests merely for line
coverage. Remove or consolidate a test only with written evidence that better
or equivalent protection remains.

Final reported regression results must correspond to the final working-tree
state. Any source, test, test configuration, CI, or testing-policy modification
after the final regression invalidates that result and requires the applicable
final verification gate to be rerun.

## CI and runtime governance

Pull requests and normal pushes run `test-fast` on Python 3.11, 3.12, and 3.13, then
one full branch-coverage regression on Python 3.12. Go coverage runs once.
This preserves compatibility validation without multiplying every slow
behavioral flow by every interpreter. The full job publishes its duration
report as an artifact. Nightly and manual runs execute the full pytest suite on
all three versions (3.11, 3.12, 3.13), including slow and acceptance markers. A live marker only runs
when the repository variable `HOWLPLANE_RUN_LIVE_TESTS` explicitly enables it,
and that step tolerates pytest's exit code 5 so an empty `live` selection does
not fail the job the moment the gate is switched on.

The `main-protection` ruleset requires the status check context `test-python`.
Because the Python work is now split across a matrix, an aggregator job named
`test-python` depends on `fast-python` and `full-python` and reports their
combined result. Do not rename or remove it without updating the ruleset first:
a required check that never reports is not a skipped check, it is a permanently
blocked pull request.

Both Python jobs install the pinned Go toolchain (`tests/test_go_build.py` is
`integration`, not `slow`, and shells out to `go build` and `gofmt`), and all
Python jobs install the pinned SlopsLint binary
(`test_hygiene_policy.py::test_provider_integrity_verification` and repository
hygiene verification steps verify it on the live system and fail closed when it
is absent).

The current package metadata says `>=3.9`, but the live source imports
`tomllib`, which is standard-library only from Python 3.11. CI therefore treats
3.11, 3.12, and 3.13 as the tested compatibility set. Aligning the package metadata is
a separate compatibility decision; it is not hidden by this test optimization.

## Audit record

| Family | Decision | Evidence and retained coverage |
| --- | --- | --- |
| Authority profiles, human boundary, permissions | KEEP / integration | Security and irreversible-action invariants remain explicitly protected |
| Provider pool, failover, review traversal | KEEP / integration + slow | Durable fallback, exhaustion, and attribution have high regression value despite filesystem cost |
| Synthesis, acceptance canary, closed loop, dogfood, marathon | KEEP / acceptance | These prove major user capabilities with external seams faked |
| Core legacy orchestrator approval loop | REWRITE / unit, `run_loop` tests slow | It now fakes configured MCP startup; the approval contract remains while accidental `npx` processes are removed. The `human_proxy_intercept` authority tests stay in the fast gate. **Known gap:** `run_loop`'s `mcp_`-prefixed tool dispatch has no test, and `orchestrator_factory` now forces `mcp_clients == {}`, so it cannot gain one by accident |
| Selector infrastructure | REWRITE + ADD COVERAGE / unit | Direct, multiple, changed-test, docs, agent, central, unknown, rename/delete, no-diff, fallback, and explanation paths are tested |
| Documentation generation, installer, hygiene, compiler discovery | KEEP / slow | Measured expensive boundaries remain valuable at full and nightly gates |

No test was removed in this milestone. The intentionally deferred work is
per-test timing history or hard budgets: duration artifacts and `--durations=25`
make meaningful regressions visible without brittle millisecond limits.

## Measured gate cost

| Gate | Tests | Wall time |
| --- | --- | --- |
| `make test-fast` (Python) | 1109 of 1296 | 51.2s |
| Full `pytest tests/` | 1296 | ~272s |
| Full + branch coverage | 1296 | ~302s, 77.2% |

Moving `slow` from module to test granularity returned 164 tests to the fast
gate for 2.9s, including the ATTACK A-F hygiene suite, 25 of 26 operational
resilience tests, 33 of 41 interrupted-governance-recovery tests, 19 of 22
verification-view tests, 19 of 20 git-environment-isolation tests, and the
legacy orchestrator's human-approval intercept tests.
