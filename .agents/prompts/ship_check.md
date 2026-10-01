# Ship Check and Human Authority Gate

Evaluate evidence, verification status, and human authority boundaries before marking a task complete or merging changes.

## 1. Principles

- **Human Authority Boundary:** High-risk actions (production changes, infrastructure apply, destructive migrations, paid services, external messaging, credentials) require explicit human sign-off.
- **Fail-Closed:** Absence of human response is treated as DENIED.
- **Complete Decision Packet:** When human authorization is needed, present objective, change summary, evidence, risks, findings, and verification status in a concise packet.

## 2. Procedure

1. Evaluate policy boundaries:
   ```bash
   python -m src.control_plane check-boundary --task-file <task_spec.yaml> --actions <planned_actions>
   ```
2. If boundaries are triggered (exit code 2), transition task state to `awaiting_human` and present the decision packet to the human operator.
3. If clean and authorized:
   - Ensure evidence ledger has recorded all milestones.
   - For a code-changing task, require a valid Test Impact Assessment: `python -m src.control_plane tia check <task_id>` must pass. Use `--no-code-change` only when the task changed no code or tests. A missing or invalid assessment blocks completion; do not skip it.
   - Confirm the regression run it records covers the final working tree. If anything changed after that run, rerun the required gate.
   - Run final project verification.
   - Transition task state to `complete`.
   - Close out task journal and push verified commits.
