# Local AI Worker (Ollama) — Milestone #58

HowlPlane can use a local, quota-free, private model (`qwen2.5-coder:7b-instruct`
via [Ollama](https://ollama.com)) as a Tier-3 provider for bounded, low-risk
mechanical engineering work, so cloud AI is not required for every small action.

Ollama is registered as `resource_id=local_ollama` with
`provider_id=ollama`, `interface_id=local_runtime`, and a model-aware capability
profile. It is one runtime in the generic
[AI resource pool](AI_RESOURCE_POOL.md), not a permanent fallback. Additional
Ollama models or future llama.cpp, vLLM, and self-hosted adapters can register
separate resource/model profiles without changing the generic selector.

The local model is:
- cheap, private, quota-free
- slower and weaker than frontier cloud providers
- useful for bounded mechanical work (log analysis, failure classification,
  compiler diagnostic interpretation, documentation, small isolated fixes)

The local model is **not**:
- an authority — it never approves its own work
- a replacement for deterministic verification, independent review, or human
  approval
- a reason to run a campaign forever
- appropriate for security, authorization, destructive-operation, or
  ambiguous/architectural work

## Target hardware

Designed for conservative operation on modest consumer hardware (reference:
AMD Ryzen 5 1600 / 24 GB DDR4 / Radeon RX 580 / Bazzite Linux). CPU-only
operation is fully supported; GPU acceleration (ROCm/Vulkan) is optional and
never required. **System stability always wins over model performance** — the
defaults below exist specifically to avoid destabilizing the host machine.

## Setup

```bash
# One-time: install Ollama (official installer) and pull the canonical model,
# then run a real smoke-test inference. Never installs any other model.
ai local setup

# Equivalent manual steps:
ollama pull qwen2.5-coder:7b-instruct
ollama list
ollama ps
ollama run qwen2.5-coder:7b-instruct "Respond only with LOCAL_OK"
```

`ai local setup` will not install Ollama itself unless you either install it
yourself first or set `HOWLPLANE_LOCAL_AUTO_INSTALL=1`, in which case it runs
the official installer (`curl -fsSL https://ollama.com/install.sh | sh`). It
never installs unofficial packages, custom ROCm stacks, GPU drivers, or
alters boot/kernel configuration, and never pulls a second model.

## Defaults (all configurable, but conservative by design)

| Setting | Default |
| --- | --- |
| Model | `qwen2.5-coder:7b-instruct` |
| Context window | 8192 tokens |
| Max concurrent local inference | 1 (machine-wide lock) |
| Max loaded local models | 1 |
| Minimum available RAM before inference | 8 GiB (interactive) / **9 GiB (overnight-safe, #59)** |
| Local attempts per engineering problem | 1 implementation + 1 targeted repair |
| Consecutive local-only campaign iterations after cloud exhaustion | 10 (fixed; delegated authority can never raise this) |
| `keep_alive` sent to Ollama | `"5m"` (interactive, matches Ollama's own default) / **`0` (overnight-safe: unload immediately after each inference)** |

Overnight-safe campaigns (`ai dogfood --authority-profile overnight-safe`, see
[`OVERNIGHT_AUTHORITY.md`](OVERNIGHT_AUTHORITY.md)) register a more
conservative `OllamaLocalBackend` instance for the process's lifetime: a 9 GiB
launch floor instead of the interactive 8 GiB, and `keep_alive=0` so the model
unloads immediately after each response rather than lingering for Ollama's
default 5-minute idle window. RAM is measured before inference, immediately
after the response, and (when `keep_alive=0`) again after the requested
unload — recorded in `AgentExecutionResult.metadata` as `ram_before_gib`,
`ram_after_gib`, `ram_after_unload_gib`. If available memory remains
dangerously low even after unload, the campaign stops routing to local Ollama
for the rest of that session (`campaign_state.local_model["overnight_ram_exhausted"]`)
rather than repeatedly reloading the model against an already-thin margin. No
process is ever killed — only Ollama's own supported `keep_alive` behavior is
used.

Recommended conservative Ollama environment (not required, but documented for
operators who want it): `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`.
HowlPlane's own `LocalInferenceLock` (`src/control_plane/locking.py`) is
authoritative regardless of Ollama's own concurrency behavior.

## Discovery and availability

`diagnose_ollama()` (`src/control_plane/agent_execution.py`) deterministically
distinguishes:

- `OLLAMA_NOT_INSTALLED` — the `ollama` binary is not on `PATH`.
- `OLLAMA_SERVICE_UNAVAILABLE` — the binary exists but the local API
  (`http://127.0.0.1:11434`) does not respond.
- `MODEL_NOT_INSTALLED` — the service responds but `qwen2.5-coder:7b-instruct`
  has not been pulled.
- `RESOURCE_CONSTRAINED` — everything above is fine, but `MemAvailable` (read
  from `/proc/meminfo`) is below the configured minimum right now.
- `AVAILABLE` — safe to run inference.

These map into `ProviderAvailabilityStatus` (`UNAVAILABLE` for the first
three, a dedicated `RESOURCE_CONSTRAINED` status for the fourth). Local Ollama
is never marked `SESSION_EXHAUSTED`/`RATE_LIMITED` — it has no subscription
quota, so those statuses would be a lie.

## Routing and risk gating

`is_task_local_eligible(task)` (`src/control_plane/synthesis/provider_pool.py`)
gates every candidate-selection call: the local model is only ever considered
for `risk_level == "low"` tasks that don't touch security/authorization/
infrastructure skills or task classes. `ProviderPoolManager.select_candidates`
accepts an optional `task=` probe and excludes local providers from the
ranked candidate list whenever that task is not local-eligible — this holds
even when local Ollama is the only `AVAILABLE` provider, so cloud exhaustion
never silently expands local participation into risky work.

Routing for eligible Tier-3 tasks: local implementation attempt → deterministic
verification → PASS continues the governed lifecycle; FAIL triggers one
targeted local repair; a second failure escalates to a cloud provider.
Failure is never conflated with exhaustion:

| Local outcome | Classification |
| --- | --- |
| Bad/incomplete output | `ENGINEERING_FAILURE` |
| Fails twice on the same bounded problem | `LOCAL_CAPABILITY_INSUFFICIENT` (escalate) |
| Ollama not installed/service down/model missing | `LOCAL_PROVIDER_UNAVAILABLE` / specific reason |
| Memory guard blocks launch | `RESOURCE_CONSTRAINED` (never launched) |
| Prompt exceeds the context budget | `LOCAL_CONTEXT_INSUFFICIENT` (escalate) |
| A second inference is already running | `LOCAL_PROVIDER_BUSY` |

## Bounded local-only continuation

When every cloud provider is exhausted/unavailable
(`ProviderPoolManager.is_all_cloud_exhausted()`), `MarathonDogfoodEngine`
attempts to resolve any outstanding, evidence-backed, local-eligible
engineering gaps recorded during the campaign
(`MarathonDogfoodEngine._run_local_only_continuation`) before stopping. This
is capped at `local_only_iteration_limit` (default 10) consecutive
iterations, persisted durably in `campaign_state.local_model`, and is never
reset merely because a local task succeeds — only when cloud capacity
returns. Local Ollama's quota-free nature can therefore never make a campaign
run forever.

## Dogfood campaign UX

`ai dogfood --resume <campaign-id>` restores the campaign's **persisted**
benchmark scope; it no longer silently expands into the full default
benchmark suite. To explicitly widen scope, pass `--benchmarks` on resume —
this is recorded as an explicit scope extension
(`campaign_state.scope_extensions`), not a silent change. Resuming also
re-probes live provider availability, so previously exhausted cloud providers
regain eligibility once their quota resets.

`ai dogfood --status <campaign-id>` is a strictly read-only inspection: it
loads durable state directly from disk, never constructs a provider pool,
never probes agent binaries/services, never invokes a provider, and never
mutates campaign scope.

## Real local dogfood evidence (2026-08-21)

A real end-to-end exercise on the target machine, using the genuine
`OllamaLocalBackend` (no fakes), verifying the full contract:

1. **Setup:** `ollama pull qwen2.5-coder:7b-instruct` (4.7 GB), `ollama list`
   confirmed, `ollama run qwen2.5-coder:7b-instruct "Respond only with
   LOCAL_OK"` → `LOCAL_OK` in ~5.6s.
2. **`ai local setup`** (through the actual CLI, not a test double): reported
   `PASS` with a real inference (`stdout='LOCAL_OK'`, `duration=4.886s`).
3. **Real low-risk local task:** a genuine HowlFrame compiler diagnostic was
   produced from a deliberately invalid `.howl` file (raw JSON object literal
   instead of an S-expression), then handed to a `risk_level="low"`,
   `task_class="docs"` `TaskSpec` via the real `local_ollama` backend to
   explain the failure for a troubleshooting note:

   - Backend: `local_ollama` (genuinely invoked; `output_sha256` recorded)
   - Duration: 8.365s; context length: 8192
   - Available RAM: 9.287 GiB before → 8.817 GiB after (well above the 8 GiB
     floor throughout)
   - Model's explanation (verbatim): *"The source code is using standard
     JSON syntax for an object literal, which HowlFrame does not support.
     Instead, HowlFrame requires S-expression forms for object literals. To
     fix it, replace the standard JSON object literal with the HowlFrame
     equivalent S-expression, such as `(hash "status" "ok")`."*

   The diagnosis (raw `{...}` JSON literals are invalid; HowlFrame needs an
   S-expression form) was **correct**. The suggested replacement syntax,
   `(hash "status" "ok")`, was **not** — real HowlFrame sources use
   `(dict ("status" "ok"))` (see `res_json`/`dict` usage across this repo's
   generated products). This is exactly why local output is never trusted
   without deterministic verification and review (#58 Phase 12): the model
   correctly localized and explained the failure, an appropriate Tier-3 task,
   but got a specific syntactic detail wrong that a compiler/test check would
   have caught immediately had this been an actual code change.

## Recommended first overnight campaign command

Only after local unit tests, a real Ollama smoke test, and a short bounded
multi-provider campaign have all been verified green:

```bash
ai dogfood --until-providers-exhausted --authority-profile overnight-safe
```

See [`OVERNIGHT_AUTHORITY.md`](OVERNIGHT_AUTHORITY.md) (#59) for the full
delegated-authority model this flag binds: exact permissions, denied actions,
TTL, merge budget, repository allowlist, parked-task/decision-queue behavior,
and resume semantics. Real git/GitHub integration (branch → commit → push →
PR → CI → merge) is only ever performed for real, and only within that
envelope's scope — anything outside it parks for the operator's morning
review rather than either fabricating success or freezing the whole campaign.
