---
name: "defensive_debugging"
description: "Triggers during error troubleshooting, crash analysis, or runtime exception reviews"
triggers:
  - "debug"
  - "stack trace"
  - "crash"
  - "exception"
  - "root cause"
  - "troubleshoot"
tier: 1
---

# Defensive Debugging Standards

This skill defines the mandatory diagnostic protocol, isolation procedures, and validation requirements that must be executed when troubleshooting software errors, crash reports, or runtime exceptions.

## Diagnostic Protocol

Before modifying any source code or application configurations, execute the following sequential isolation process:

1. **Log Analysis**: Retrieve and thoroughly inspect the active application logs, stack traces, or terminal standard error (`stderr`) outputs. Identify the exact exception class, error code, and failure message.
2. **State and Input Isolation**: Isolate the specific source file, line number, and runtime state (including environment variables, configurations, database state, and user inputs) that triggered the execution block or crash.
3. **Root Cause Analysis**: Formulate and document a clear, evidence-based root cause explanation. Contrast what the system actually did with what it was expected to do, referencing specific lines of code or data.
4. **Remediation Design**: Devise a targeted fix that addresses the root cause directly. The fix must minimize the blast radius, adhere to the minimal-change principle, and avoid unrelated refactoring.

## Defensive Code Remediation Principles
- **Input Validation**: When fixing input-triggered issues, implement strict validation mechanisms (boundaries, types, and schemas) at the system boundaries. Never trust incoming data.
- **Defensive Error Handling**: Always catch, handle, and log exceptions gracefully. Ensure the remediation prevents the leakage of internal system details, raw configuration parameters, or detailed stack traces to end users.

## Related Skills
- Defer to `quality_assurance` for regression suites and coverage signals that validate a fix.
- Defer to `test_and_verify` for the automated verification workflow after remediation.
- Defer to `software_development` for clean-code style and secure-by-default remediation standards.
