# 📖 HowlPlane - User Guide & Reference

Welcome to the official User Guide for HowlPlane! This document outlines how to operate HowlPlane across your development repositories.

---

## 0. First run

```bash
cd /path/to/project
howlplane setup            # check the repo, workers and workspace trust; prepare if you confirm
howlplane start            # start working
howlplane status           # health, current work, and your next action
howlplane logs --follow    # the details
```

To intervene: `howlplane approve ID` or `howlplane reject ID`. To pause: `howlplane stop`, and `howlplane start` continues. To troubleshoot: `howlplane doctor`. Bare `howlplane` shows where the repository stands.

`howlplane setup --json` is the non-interactive form. When status says `OWNER REQUIRED`, run the command it prints (`howlplane approve ID`; the id can name a task, a parked work item or a repository proposal). `howlplane config show`
and `howlplane config explain <setting>` show effective configuration and where
each value came from. The sections below are the full reference.

### Operating the Factory day to day

| You want to know | Run | Look for |
| --- | --- | --- |
| Is it healthy, what is it doing, who is working? | `howlplane status` | the state in the heading, `Worker`, `Elapsed`, `Next` |
| Why is it waiting, and will it recover? | `howlplane status` | `Reason`, `Recovery`, `Retry` |
| What happened? | `howlplane logs` | compact `time LEVEL message` lines |
| Only the problems | `howlplane logs --errors --since 1h` | ERROR lines |
| One work item or provider | `howlplane logs --work-item WI-042` / `--provider codex` | |
| Is everything ready? Which workers work? | `howlplane doctor` (`--ready`, `--agents`, `--factory`, `--system`, `--json`) | the sections and `Fix` list |
| Pause and continue | `howlplane stop`, then `howlplane start` | state is preserved |

The `howlplane factory ...` commands, `howlplane agents doctor` and the old
`--work-item/--proposal/--state-dir` approval flags still work for scripts and
advanced use.

`factory logs` shows the operator event log (what HowlPlane did). Worker
transcripts and run evidence stay under the target repository's
`.task_runs/<task>/` directories and are never mixed into it; `--raw` shows the
process output instead. Every line is redacted before display, including under
`--follow` and for the systemd journal.

Output adapts to where it goes: color and Unicode only on an interactive
terminal, plain ASCII when piped or in CI, never in `--json`. Use `NO_COLOR=1`
or `--color never` to disable color. A recommended command is printed only when
it exists and is valid for the situation. If something unexpected fails you get
an `INTERNAL_ERROR` with a diagnostic id (`HP-...`); re-run with `--debug` for
the traceback, which is also saved, redacted, in
`~/.local/state/howlplane/diagnostics/`.

---

## 1. 🚀 Everyday Workflow: The `howlplane` Command

The primary way to interact with HowlPlane across any codebase on your machine is through the `howlplane` command (the older `ai` launcher remains as a deprecated alias):

```bash
# Stand inside any repository and execute an objective:
cd /path/to/project
howlplane work "fix the highest-value open bug"

# See what HowlPlane is doing and what, if anything, you need to do:
howlplane status
# Project diagnostics: verification suites, locks, and task runs:
howlplane status --verbose

# Deterministically route a task and generate reviewer assignments without mutations:
howlplane route "patch authentication vulnerability"

# Inspect the operator-enabled AI resource pool:
howlplane providers
howlplane providers --json

# Run system and toolchain preflight diagnostics:
howlplane doctor

# Verify each agent CLI (Codex, Claude Code, Cursor, AGY, Devin); --live sends one tiny prompt each:
howlplane agents doctor
howlplane agents doctor --live --json
# Workspace readiness (trust) for one repository, then authorize it for unattended Factory use:
howlplane agents doctor --repo /path/to/repo
howlplane factory prepare --repo /path/to/repo

# Run a finite queue of human-APPROVED tasks, one orchestration session each
# (see documentation/FACTORY_QUEUE.md); --dry-run shows what would happen:
howlplane factory queue queue.json --dry-run
howlplane factory queue queue.json

# Execute deterministic verification on the current project:
howlplane verify
```

---

## 2. 🎛️ The Setup & Management Tool (`ai_installer`)

HowlPlane provides a standalone, cross-platform binary installer featuring an interactive Terminal User Interface (TUI):

```bash
./ai_installer
```

Available actions:
* **Install / Setup Environment:** Runs initial setup, builds dependencies, and links global agent rules and skills to Gemini CLI / Antigravity, Claude Code, Codex, and Devin CLI.
* **Customize Profile:** Launches an interactive wizard to generate or update `USER_PROFILE.md` for profile grounding.
* **Launch RAG Interface:** Boot up either the terminal UI or web UI to query the local knowledge layer.
* **Sync / Update Repository:** Pull the latest rules, skills, and prompts from GitHub.
* **Uninstall Global Links:** Cleanly detaches global links from your system environment.

---

## 3. 🤖 Visual Work Surface

HowlPlane is the governed engine; it does not ship a terminal or web dashboard.
The rich visual work surface is **HowlBoard**, which reads the stable contracts
HowlPlane publishes (for example `howlplane factory status --json` and the
redacted remote status snapshot). Use `howlplane factory status` for a concise
terminal view and `--verbose` for full supervisor detail.

---

## 4. 🛠️ Embedded Developer & Diagnostics Tools

HowlPlane includes verification, diagnostic, and automation utilities:

* **`howlplane doctor` (or `python src/infrastructure/doctor.py`)**: The one diagnostic front door. It reports System (Python, Go, Git, evidence ledger integrity, operating mode egress enforcement), Workers, Repository and Factory readiness, then an overall `READY` or `NOT READY` with a numbered `Fix` list. `--ready` prints only `READY` or `NOT READY` plus fixes and exits non-zero when not ready; `--json` is the structured form (`howlplane.doctor/v1`); `--system`, `--agents` and `--factory` show one section in full (`--agents` and `--factory` are `agents doctor` and `factory doctor`).
* **`howlplane route`**: Explains role-aware selection without provider probes, generation, or capacity mutation.
* **`howlplane providers`**: Shows the versioned resource inventory; `howlplane providers reset <resource-id>` re-probes only current state without deleting history.
* **`python src/infrastructure/build_vector_index.py`**: Scans the knowledge layer and builds a localized ChromaDB vector index for offline semantic retrieval.
* **`python src/core/adversarial_tester.py`**: Runs prompt-injection and adversarial negative tests against the ruleset.
* **`scripts/generate_skills_manifest.py`**: Auto-generates the skills manifest table in `AGENTS.md` and rebuilds agent symlinks.

---

## 5. ✅ Deterministic Verification & CI/CD Guardrails

When contributing to HowlPlane, run local verification suites:

```bash
make test lint build docs
```

This verifies all Python tests, Go packages, linting rules, and documentation generators.

---

*Need help troubleshooting? Check out the [Troubleshooting Guide](troubleshooting.md).*
