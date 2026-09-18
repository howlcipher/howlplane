# HowlPlane Factory Incident Report — 2026-09-15

## Summary

A live dogfooding run against `howlcipher/howlframe` exposed four concrete
HowlPlane issues and confirmed that several pieces of provider failover
machinery were already working. All four issues are now fixed and covered by
deterministic regression fixtures. The complete HowlPlane verification suite
passes (`make test-full`, 1566 Python tests + Go tests).

## Confirmed Defects

1. **Campaign identity defaulted to a stopped historical campaign.**
   `howlplane factory status` run from the HowlFrame repository resolved the
   historical campaign `e95237ce7bac3feb346602aa` even though a newer campaign
   (`howlframe-safe-20260915-144921`) was actively dispatching work with an
   explicit `--state-dir`. Root cause: the convenience resolver only looked at the
   canonical/default state directory when no active process was found there, so
   it silently returned the older stopped state directory.
   Fixed in `src/control_plane/factory/campaign.py` by teaching
   `resolve_campaign(..., prefer_active=True)` to scan every state directory for
   the repository and prefer a single active process.

2. **Service unit identity did not follow the executed state directory.**
   The running service was named `howlplane-factory-e95237ce7bac3feb346602aa.service`
   while executing `--state-dir .../howlframe-safe-20260915-144921`. Root cause:
   unit names were derived from the repository campaign id instead of the state
   directory name. Fixed in `src/control_plane/factory/service.py`: `_unit_name`
   now names the unit after `campaign.state_dir.name`.

3. **Claude's weekly-limit message was classified as `ENGINEERING_FAILURE`.**
   The two-second failure produced the message
   `You've hit your weekly limit · resets Sep 16, 2pm (America/Detroit)` and was
   reported as an engineering failure. Root cause: the classifier did not recognize
   the "weekly limit" / "daily limit" wording. Fixed in
   `src/control_plane/synthesis/provider_pool.py` by adding those phrases to the
   substring/regex patterns for `SESSION_LIMIT`.

4. **A harness execution-budget timeout caused immediate reselection with no
   bounded cooldown.** The first AGY attempt consumed ~9:49 and returned
   `EXECUTION_BUDGET_EXCEEDED`; Plane then selected AGY again for the next item,
   burning another ~8:50 before finally seeing `SESSION_LIMIT`. Root cause:
   `EXECUTION_BUDGET_EXCEEDED` left the provider in `AVAILABLE` state, so nothing
   discouraged immediate reuse. Fixed in
   `src/control_plane/synthesis/provider_pool.py`: budget kills are now recorded
   as `DEGRADED` with the normal transient cooldown, making the provider
   eligible but deprioritized until the cooldown expires or a reset occurs.

## Behavior Already Working

The following live sequence was already correct and is now an explicit
regression fixture:

```text
AGY
→ SESSION_LIMIT
→ provider exhausted (SESSION_EXHAUSTED)
→ same work item retried
→ Codex selected
```

The Marathon attempt loop in `src/control_plane/synthesis/marathon.py` already:

* records each failed attempt,
* persists provider state,
* clears the pinned provider before selecting the next candidate,
* excludes previously-attempted resources,
* transitions the `TaskSpec` back to `planned`, and
* selects the next eligible provider.

The new fixture `tests/test_factory_incident_regression.py` locks this
behavior in, together with the A/B/C sequence requested below.

## Root Causes

### Campaign resolution

* `src/control_plane/factory/campaign.py` `resolve_campaign(..., prefer_active=True)`
  now scans `_list_campaign_state_dirs()` for the repository, checks process
  liveness via `is_process_record_active()` (shared with the service path), and
  returns the single active campaign or raises `CampaignError` when several are
  active.
* `src/control_plane/factory/service.py` `_unit_name()` now uses the state
  directory name, so a non-default campaign gets a distinct systemd unit.
* `src/control_plane/locking.py` `is_process_record_active()` is the single
  liveness predicate used by both the campaign resolver and the service supervisor,
  removing duplicated `systemctl` / `is_process_alive` checks.

### Claude terminal failure

* `src/control_plane/synthesis/provider_pool.py` `classify_failure()` matches
  the message against the `SESSION_LIMIT` regex, which now includes
  `(?:session |daily |weekly |monthly )?limit` and the corresponding
  `EXHAUSTION_PATTERNS` substrings.

### Execution-budget cooldown

* `src/control_plane/synthesis/provider_pool.py` `record_result()` maps
  `EXECUTION_BUDGET_EXCEEDED` to `ProviderAvailabilityStatus.DEGRADED` and
  includes that class in the transient cooldown set. The ranking logic already
  treated `DEGRADED` as lower priority than `AVAILABLE`/`UNKNOWN`, so no
  separate retry system was needed.

### Deadline visibility

* `src/control_plane/progress.py` already accepted `deadline_seconds`; the
  heartbeat formatter now emits `elapsed MM:SS / MM:SS` when a deadline is set.
* `src/control_plane/orchestrator.py` passes
  `deadline_seconds=self.config.timeout_seconds` for implementation and
  `deadline_seconds=remediation_timeout` for remediation, so operators see the
  actual enforced bound.

## Timing Analysis

The live AGY durations were approximately:

* **Work Item A:** `00:09:49`
* **Work Item B:** `00:08:50`

Both were harness timeouts. The effective deadline chain is:

1. `FactorySupervisor` / Marathon uses `self.config.timeout_seconds`.
2. `Orchestrator._implement_task()` passes that value as
   `timeout_seconds` to the backend.
3. `AgyBackend` passes it to the `agy` CLI as `--print-timeout`.
4. When the harness kills the subprocess, `SubprocessAgentBackend` records
   `timed_out=True`, `exit_code=0`, and `TIMEOUT_SOURCE_HARNESS`.
5. `ProviderPoolManager.classify_failure()` maps this to
   `EXECUTION_BUDGET_EXCEEDED` without treating it as provider capacity.

The ~10 minute wall-clock time matches the configured Factory timeout; it was
not doubled or accidentally inherited from an old configuration. The second
shorter duration (`8:50`) was the same harness timeout after AGY had already
partially consumed its session budget internally and terminated sooner with a
`SESSION_LIMIT` screen.

After the fixes, an operator watching progress sees:

```text
IMPLEMENTING | agy | elapsed 00:08:30 / 00:10:00 | still working
```

## Campaign Identity Analysis

The confusing live state was produced by three independent facts:

1. **The service was started with an explicit `--state-dir`** pointing at
   `.../howlframe-safe-20260915-144921`, but the systemd unit name was generated
   from the repository campaign id (`e95237ce...`). Because the old historical
   campaign existed, `_unit_name()` reused that id instead of the state directory
   name.

2. **Ordinary `factory status` discovered the repository, picked the default
   state directory** (`e95237ce...`), found it stopped, and reported it. It never
   scanned sibling state directories for a running process.

3. **Explicit `--state-dir` always worked:** running status with the new state
   directory directly returned the active campaign, confirming that the state
   directory itself and its metadata were healthy.

The fix makes status/logs/stop all use the same resolver path as start/run:
`resolve_campaign(..., prefer_active=True)`. When one campaign is active it is
returned; when several are active an error is raised; when only stopped
historical campaigns exist the canonical/default directory is used.

## Claude Analysis

The durable evidence was:

```json
{
  "error": "You've hit your weekly limit · resets Sep 16, 2pm (America/Detroit)",
  "exit_code": 1,
  "failure_class": "ENGINEERING_FAILURE",
  ...
}
```

The two-second duration and the explicit "weekly limit" text make this a
terminal provider capacity signal, not an engineering failure. The classifier
fell through to `ENGINEERING_FAILURE` because the regex and substring lists only
matched "session limit", "usage limit", etc. The added patterns now catch
"you've hit your weekly/daily limit" and route it to `SESSION_LIMIT`, which
`record_result()` then persists as `SESSION_EXHAUSTED`.

## Fixes

| File | Change |
|------|--------|
| `src/control_plane/factory/campaign.py` | `resolve_campaign(..., prefer_active=True)` scans all state dirs, prefers active, reports ambiguity; added `_target_dir_from_metadata()` helper. |
| `src/control_plane/factory/service.py` | `_unit_name()` derives the unit name from the state directory name. |
| `src/control_plane/locking.py` | Added shared `is_process_record_active()` predicate. |
| `src/control_plane/cli.py` | `factory status`, `logs`, `stop`, `run`, `run-once` all route through `resolve_campaign(prefer_active=True)`; explicit `--state-dir` remains authoritative. |
| `src/control_plane/synthesis/provider_pool.py` | Claude weekly/daily limit phrases added to `SESSION_LIMIT` patterns; `EXECUTION_BUDGET_EXCEEDED` now maps to `DEGRADED` with a cooldown. |
| `src/control_plane/agent_execution.py` | Added `_TIMEOUT_MARKERS`, `_build_result()`, `_build_timeout_result()` to share timeout detection between blocking and watchdog paths; fixed `[.!?]?` punctuation in terminal control regexes. |
| `src/control_plane/progress.py` | Already supported `deadline_seconds`; heartbeat now prints `elapsed / deadline`. |
| `src/control_plane/orchestrator.py` | Passes `deadline_seconds` to implementation and remediation progress operations. |
| `tests/test_factory_cli.py` | Regression tests for active-campaign preference, multiple-active ambiguity, explicit `--state-dir`, and service unit naming. |
| `tests/test_factory_incident_regression.py` | Deterministic A/B/C provider fixture covering budget exceeded, Claude weekly limit, AGY session exhaustion, Codex failover, and recovery. |
| `tests/test_progress.py` | Deadline-in-heartbeat assertion. |
| `tests/test_agent_execution.py` | Updated budget-kill assertions to expect `DEGRADED`. |
| `tests/test_dogfood_failure_handling.py` | Updated existing budget-kill tests to expect bounded `DEGRADED` cooldown. |

## Regression Evidence

Selected new tests:

```text
tests/test_factory_incident_regression.py
  test_agy_execution_budget_is_degraded_not_exhausted
  test_claude_weekly_limit_is_session_exhausted
  test_live_incident_provider_sequence
  test_failed_implementation_attempt_does_not_advance_lifecycle

tests/test_factory_cli.py
  test_factory_status_prefers_active_campaign_over_stopped_historical
  test_factory_status_rejects_multiple_active_campaigns
  test_factory_service_unit_name_reflects_explicit_state_dir
  test_factory_status_explicit_state_dir_wins_without_repository

tests/test_progress.py
  test_tracker_phase_exposes_effective_deadline_in_heartbeat
```

Full verification:

```text
$ make test-full
...
1566 passed, 3 deselected in 313.23s (Python)
Go packages: PASS
slopslint check --classify --enforce: PASS (active_clones = 13)
```

## Expected Operator Behavior

1. **Old stopped campaign + new active campaign:** `howlplane factory status`
   from the repository now reports the new active campaign and its actual state
   directory. If several are active, it prints an error listing them and asks for
   `--state-dir`.

2. **AGY session exhausted:** After `SESSION_LIMIT`, AGY is recorded as
   `SESSION_EXHAUSTED`. Subsequent work items skip AGY until cooldown/reset.
   Status shows the exhaustion reason.

3. **AGY generic execution timeout:** After `EXECUTION_BUDGET_EXCEEDED`, AGY is
   recorded as `DEGRADED` with a bounded cooldown. It is still eligible after the
   cooldown, but it is deprioritized, so another provider is tried first.

4. **Claude terminal failure:** A weekly/daily limit message is classified as
   `SESSION_LIMIT` and Claude becomes `SESSION_EXHAUSTED`, not an engineering
   failure.

5. **Codex failover:** When AGY (and Claude if also exhausted) are ineligible,
   the provider pool selects Codex; the work item is retried from `planned` and
   the trajectory records the handoff.

6. **Subsequent work after AGY exhausted:** Work Item C does not immediately
   re-select AGY while it remains `SESSION_EXHAUSTED`; it starts with the next
   eligible provider. A manual reprobe/reset or cooldown expiration returns AGY
   to eligibility.
