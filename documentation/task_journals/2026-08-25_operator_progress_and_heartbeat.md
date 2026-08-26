# Task Journal: Operator-Visible Progress and Heartbeat Tracking

## Architecture and Recovery Header

STATUS:
    IN_PROGRESS

STARTING_MAIN:
    b9d3637

WORKING_BRANCH:
    feat/operator-progress-heartbeat

CURRENT_PHASE:
    implementation_and_unit_testing

LAST_COMPLETED_PHASE:
    architecture_and_control_plane_integration

OPERATOR_PROGRESS_ARCHITECTURE_DECISIONS:
    Governed operations (e.g. `ai work ... --execute`) can block inside an
    implementation provider, reviewer, remediation cycle, or verification
    subprocess for multiple minutes without producing terminal output.
    Make observable execution liveness and truthful phase visible to operators
    without exposing model chain-of-thought, inventing percentage estimates,
    or streaming noisy internal provider outputs.
    Represent truthful execution phases: PREPARING, ROUTING, IMPLEMENTING,
    REVIEWING, REMEDIATING, VERIFYING, AWAITING_AUTHORIZATION, COMPLETE, FAILED,
    CANCELLED.
    Emit immediate status lines upon entering each phase.
    Emit periodic wall-clock heartbeats (every ~30s by default) during blocking
    operations and stop heartbeat emission immediately when child processes exit.
    Persist atomic, durable machine-readable state in
    `.task_runs/<TASK_ID>/progress.json` with schema `howlplane.task_progress/v1`.
    Enhance `ai status` to inspect `progress.json` and report current phase,
    resource, wall-clock elapsed time, and heartbeat freshness (`7s ago`),
    truthfully distinguishing active runs from dead processes (`STALE (Process not running)`).
    Route human operator progress output to stderr so structured JSON on stdout
    remains clean and parseable.
    Ensure progress tracking is provider-neutral across Claude Code, Codex, AGY,
    Devin CLI, Gemini CLI, local Ollama, and test mock backends.

DOGFOOD_ORIGIN:
    During dogfood testing of governed HowlPlane operations executing against
    the HowlFrame codebase with the AGY implementation provider, the task
    spent several minutes in implementation without any observable output,
    appearing frozen to the operator even though the underlying AGY subprocess
    was actively compiling and modifying files. This motivated a first-class,
    deterministic progress and heartbeat mechanism integrated throughout the
    governed loop.

FILES_CHANGED:
    src/control_plane/progress.py
    src/control_plane/orchestrator.py
    src/control_plane/review_runner.py
    src/control_plane/verification.py
    src/control_plane/launcher.py
    documentation/task_journals/2026-08-25_operator_progress_and_heartbeat.md
    tests/test_progress.py
