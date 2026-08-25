# Data Flows & Network Egress Reference

**Repository:** `howlcipher/howlplane`  
**Purpose:** Comprehensive map of network egress points, local-versus-remote data handling, telemetry, and third-party service integrations across the repository.

---

## 1. Overview & Privacy Principles

HowlPlane operates primarily as a **local-first control plane and knowledge base**.
- **Operating Mode Enforcement:** Controlled by `operating_mode: "local_only"` (default) or `"connected"` in `config/settings.yaml`. In `local_only` mode, network egress is mechanically prevented across providers, telemetry, and external document integrations.
- **Enforcement Scope:** Network isolation in `local_only` mode is enforced at the application runtime level (blocking hosted provider dispatches, suppressing telemetry, and failing preflight checks on cloud model ids). Note that application-level egress governance complements but is distinct from OS-level kernel network sandboxing.
- Core rulebooks (`AGENTS.md`), domain skills (`.agents/skills/`), and prompts (`.agents/prompts/`) are evaluated locally on disk.
- Embeddings and semantic search run against local ChromaDB (`.chroma/`) or optional local PostgreSQL pgvector.
- Telemetry is stored in local SQLite (`.telemetry/telemetry.db`) and never transmitted externally by default.

---

## 2. Network Egress Integration Points

| Integration Point | Trigger Condition | Data Sent | Destination | Configuration Gating | Default Status |
| --- | --- | --- | --- | --- | --- |
| **Hosted LLM Providers (LiteLLM)** | Explicit invocation of non-local model (`claude`, `gemini`, `gpt`) | Task instructions, prompts, diffs, context | Provider APIs (Anthropic, Google AI, OpenAI) | `operating_mode: connected` AND `tier_models` in `config/settings.yaml` | Off by default (hard-blocked in `local_only` mode) |
| **Local Ollama** | Standard generation passes | Prompts, task instructions | `localhost:11434` (Strictly local) | `provider: ollama` in `config/settings.yaml` | Active local backend (allowed in `local_only` and `connected`) |
| **Model Context Protocol (MCP) - `fetch`** | Tool call requiring web research or URL scraping | Target URL, HTTP request headers | Requested web domains | `active_mcps: [fetch]` in `config/settings.yaml` | Disabled unless configured in `active_mcps` |
| **Model Context Protocol (MCP) - `memory`** | Context indexing/retrieval | Memory keys and knowledge snippets | Subprocess `mcp-server-memory` (Local IPC) | `active_mcps: [memory]` in `config/settings.yaml` | Active (Local subprocess) |
| **LangSmith Tracing** | Optional LLM observability | Prompts, outputs, metadata | `api.smith.langchain.com` | `operating_mode: connected` AND `LANGCHAIN_TRACING_V2=true` | Disabled in `local_only` mode (hard-disabled) |
| **Google Drive / Docs API** | User runs `scripts/push_to_docs.py` or `scripts/pull_from_docs.py` | Exported markdown docs, OAuth tokens | Google Workspace APIs | `operating_mode: connected` AND `credentials.json` | Off by default (hard-blocked in `local_only` mode) |
| **GitHub Actions CI/CD** | Git push to remote repository | Repository commits, test execution | GitHub runners (`github.com`) | Standard Git push operations | CI only on push |

Resource selection applies `local_only` and per-task no-egress filters before
hosted adapter lookup. In local-only mode `ai providers`, `ai doctor`, governed
planning, implementation, review, remediation, synthesis, and dogfood perform
zero hosted readiness requests and zero hosted generation. `ai route` is
read-only in every mode and uses persisted capacity without probing. Local
Ollama traffic remains loopback-only. Operator configuration and current
capacity live outside the repository in `~/.config/howlplane/`; neither carries
credentials.

---

## 3. Local Data Stores

1. **Telemetry Database (`.telemetry/telemetry.db`)**
   - Stores API latency, token counts, gate attempts, and failure records.
   - Strictly local SQLite database; zero network transmission.
2. **Vector Index (`.chroma/`)**
   - Local ChromaDB vector database storing chunk embeddings for semantic retrieval.
   - Built locally via `sentence-transformers` running on CPU/GPU.
3. **Evidence Ledger (`logs/control_plane/evidence_ledger.jsonl`)**
   - Append-only structured log of control plane events and verification runs.
   - Automatically sanitized: all API keys, bearer tokens, credentials, and email addresses are redacted prior to persistence.
