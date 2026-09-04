# 📖 HowlPlane - User Guide & Reference

Welcome to the official User Guide for HowlPlane! This document outlines how to operate HowlPlane across your development repositories.

---

## 1. 🚀 Everyday Workflow: The Global Launcher (`ai`)

The primary way to interact with HowlPlane across any codebase on your machine is through the `ai` command:

```bash
# Stand inside any repository and execute an objective:
cd /path/to/project
howlplane work "fix the highest-value open bug"

# Inspect active project status, verification suites, and task runs:
howlplane status

# Deterministically route a task and generate reviewer assignments without mutations:
howlplane route "patch authentication vulnerability"

# Inspect the operator-enabled AI resource pool:
howlplane providers
howlplane providers --json

# Run system and toolchain preflight diagnostics:
howlplane doctor

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

## 3. 🤖 Querying the Knowledge & Skills Layer (RAG Interfaces)

You can explore and query the HowlPlane knowledge base interactively:

### 🖥️ Terminal UI (TUI)
* Fast, terminal-native chat interface built with Python `textual`.
* Allows hot-swapping configured LLM providers for interactive queries.

### 🌐 Web UI (Streamlit)
* Graphical browser interface built with Python `streamlit`.
* Useful for visually reviewing retrieved documentation snippets and telemetry.

---

## 4. 🛠️ Embedded Developer & Diagnostics Tools

HowlPlane includes verification, diagnostic, and automation utilities:

* **`howlplane doctor` (or `python src/infrastructure/doctor.py`)**: Runs complete health diagnostics on Python, Go, Git, evidence ledger integrity, operating mode egress enforcement, and non-generative provider readiness.
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
