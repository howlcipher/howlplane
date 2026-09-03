# Work the Next Backlog Item

Work exactly one item end to end using the Multi-Agent Engineering Control Plane, leaving the repository in a state where this chat can be cleared and the next session starts with zero context.

## Control Plane Workflow
```text
Human objective / Backlog item
      ↓
Task specification (TaskSpec)
      ↓
Deterministic Router (agent + specialized reviewers)
      ↓
Implementation delegate (or local execution)
      ↓
Independent Reviewers (falsification briefs)
      ↓
Review Reconciliation (no silent dismissal of defects)
      ↓
Deterministic Verification (build, lint, tests)
      ↓
Evidence Ledger Recording
      ↓
Human Authority Gate (if boundary triggered)
      ↓
Ship / Done
```

## 1. Select

- Check `documentation/task_journals/` first (ignore `TEMPLATE.md`). If a journal for an in-flight item exists, resume that item instead of starting a new one.
- **Concurrent-session check:** whenever selecting *any* item (whether resuming a journal/worktree or picking a fresh item from the backlog), run `ps aux | grep -E "claude|agy"` and look for other live processes beyond this session's own. If another live process shares this repository's working directory (verify via `readlink /proc/<pid>/cwd`), immediately run `git fetch` and compare `HEAD` against `origin/main` right after selecting the item and before opening a task journal or starting delegation. A match alone is not disqualifying — concurrent sessions working unrelated items are routine on this machine — but if you cannot rule out that another live process is already working the same journal path or the same target repo/files, pause via `AskUserQuestion` and confirm with the user before beginning delegation, rather than assuming the field is clear.
- Run `git worktree list` and look for local or remote unmerged branches whose names suggest agent worktrees (e.g. `worktree-*` branches, or anything under `.claude/worktrees/`). If a worktree or unmerged worktree branch exists, inspect it before selecting: an uncommitted task journal inside a worktree counts as a resume point exactly as if it were in `documentation/task_journals/`, and unmerged commits on a worktree branch may mean a whole item is already done and just needs merging and closing out rather than redoing.
- Otherwise read the ranked tables in `issues.md` and `improvements.md` and pick the single highest-priority open item across both. Bugs outrank improvement work of similar score, per the rule in `issues.md`.
- **Below-floor gate:** never silently pick an item flagged `⚠️ below floor` (score under the 0.5 ROI floor defined in `improvements.md`). Skip past it to the highest-scoring above-floor item, and tell the user which flagged items were skipped so they can confirm one (work it anyway), re-scope it until it clears the floor, or close it. Work a below-floor item only on the user's explicit confirmation in the current session.

## 2. Re-evaluate & Specify

- Confirm the item is still worth doing and that its stated requirements still match the current code and environment; both may have changed since the item was filed.
- If it is stale, update the row and detail section, merge it into another item, or close it with a dated note explaining why. A well-documented closure counts as completing this run.
- Open a task journal from `documentation/task_journals/TEMPLATE.md` and initialize a machine-readable task specification:
  ```bash
  python -m src.control_plane init-task --task-id <ID> --objective "<objective>" --risk <low|medium|high|critical>
  ```
- Run the control plane router to determine the implementation agent type and specialized reviewer roles:
  ```bash
  python -m src.control_plane route-task --task-file <task_spec.yaml>
  ```

## 3. Execute & Delegate

This session is the orchestrator, not the implementer. To preserve Claude session limits, keep only selection, re-evaluation, backlog and journal edits, verification, review reconciliation, and the commit in this session; delegate the implementation itself to a non-Claude model.

- Route the matching skills from `.agents/skills/`, scan for free tools, and read the item's detail section before writing the delegation brief.
- Write a self-contained brief for the delegate: the item's detail section, the specific files involved, the tests to add or update, and the relevant protocol constraints. The delegate starts with zero repository context, so the brief must stand alone.
- Pick the delegate from what is live right now, starting from the router recommendation / Gemini model column:
  - **Antigravity CLI (headless):** `agy -p "<brief>" --model "<model>" --mode accept-edits --print-timeout 30m`. Headless agy does not treat the invocation directory as its workspace — always give absolute file paths in the brief. **Always verify the claimed diff before trusting it:** on 2026-07-19 GPT-OSS 120B via agy returned a detailed, plausible "Changed files" table describing exactly the right edits after running for several minutes, but made zero real changes anywhere on disk (`git status` was clean, `grep` for the new symbols found nothing). Run `git status`/`git diff` after every delegation, before reading its summary as fact. If the brief contains backticks (e.g. quoting Go/Python identifiers), write it to a file and pass it as `agy -p "$(cat brief.txt)"`. Run `agy -p` with a Bash timeout well above the tool's 2-minute default (or in the background). List live model names with `agy models`. When Antigravity is fully unavailable, fall back to local Ollama for drafting and apply trivial fully-specified edits directly.
  - **Local Ollama:** for small, well-bounded subtasks (draft a function, review a diff, write a doc section) where a local model suffices. Check live tags with `curl localhost:11434/api/tags`.
  - Never delegate to Claude Code subagents (the Agent tool) for limit-saving; they bill the same Claude plan as this session.
- Require a clean `git status` before launching a delegate so its diff is exactly attributable.

## 4. Independent Review & Reconciliation

- Generate independent reviewer briefs using the control plane:
  ```bash
  python -m src.control_plane briefs --task-file <task_spec.yaml> --diff-file <diff.patch>
  ```
- Run specialized reviewers (`correctness-reviewer`, `test-falsifier`, `regression-reviewer`, `security-reviewer`, `simplicity-reviewer`).
- Collect findings into `findings.yaml` and run reconciliation:
  ```bash
  python -m src.control_plane reconcile --findings-file <findings.yaml> --output <reconciliation_report.md>
  ```
- Remediate all confirmed and likely findings. Any dismissed blocker or high finding must have an explicit `resolution_reason` recorded in the reconciliation report.

## 5. Verification & Evidence Recording

- Execute deterministic project verification:
  ```bash
  python -m src.control_plane verify --project-dir . --task-id <ID>
  ```
- Record the outcome in the evidence ledger:
  ```bash
  python -m src.control_plane record --task-id <ID> --agent-id <agent_id> --action verification_executed --result <passed|failed>
  ```
- Run `python -m src.control_plane check-boundary --task-file <task_spec.yaml>`:
  - If a human authority boundary is triggered (`AWAITING_HUMAN`), present the structured decision packet and await human authorization.

## 6. Close the Loop

- Verify the change end to end using the staged strategy in
  `documentation/TESTING.md`: affected tests first, then the warranted
  subsystem and final regression gate. Record the Test Impact Assessment,
  commit as `<type>(<scope>): <description>`, set the item's Status to `Done
  (YYYY-MM-DD)` with a Done note, delete this task's journal in the final
  commit, and push.
- Record findings discovered during the work as new rows plus detail sections in `improvements.md` or `issues.md`.
- Promote anything durable learned this session (constraints, decisions, environment facts) into the backlog documents or `documentation/`.
- Housekeeping: delete any journals whose items are no longer outstanding, and clean up any completed worktrees.

Done means: clean `git status`, work pushed, no journal left for the finished item, evidence recorded, and new findings filed where the next session will see them.
