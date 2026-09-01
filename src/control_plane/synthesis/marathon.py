#!/usr/bin/env python3
"""
marathon.py

Marathon dogfooding engine for repeated, evidence-driven prompt-to-product synthesis.
Runs automated synthesis benchmarks, tracks real provider quotas, detects framework gaps,
delegates governed self-improvement tasks, integrates green work, retries benchmarks,
and persists durable campaign state across process boundaries.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import getpass
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.control_plane.agent_execution import AgentBackendRegistry, read_available_memory_gib
from src.control_plane.authority_envelope import (
    AuthorityEnvelope,
    EnvelopeNotFoundError,
    TamperedEnvelopeError,
    create_envelope,
    is_expired,
    load_envelope,
    save_envelope,
    ENVELOPE_FILENAME,
)
from src.control_plane.authority_profile import get_profile
from src.control_plane.decision_queue import (
    ParkedTaskRecord,
    already_parked,
    compute_blocks_other_work,
)
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.git_baseline import capture_baseline, capture_delta
from src.control_plane.git_integration import GitIntegrationExecutor, detect_repo_slug, run_git
from src.control_plane.human_boundary import HumanBoundaryGate
from src.control_plane.orchestrator import (
    FAILURE_CLASS_AUTHORITY_BLOCKED,
    FAILURE_CLASS_ENGINEERING,
    FAILURE_CLASS_PROVIDER_EXHAUSTED,
    FAILURE_CLASS_PROVIDER_UNAVAILABLE,
    GovernedTaskOrchestrator,
    OrchestrationConfig,
)
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.backlog_source import DEFAULT_BACKLOG_FILES
from src.control_plane.synthesis.campaign_state import (
    CAMPAIGN_STATE_SCHEMA_VERSION,
    DurableCampaignState,
    EngineeringAttempt,
    GitIntegrationRecord,
)
from src.control_plane.synthesis.capability_negotiator import FrameworkGap
from src.control_plane.synthesis.engine import ProductSynthesizer, SynthesisResult
from src.control_plane.synthesis.provider_pool import (
    LOCAL_PROVIDER_IDS,
    ProviderAvailabilityStatus,
    ProviderPoolManager,
    is_task_local_eligible,
)
from src.control_plane.task_spec import DataClassSerializationMixin, TaskSpec

MARATHON_SCHEMA_VERSION = "howlplane.marathon_dogfood/v1"


class FrameworkGapEvidence(str, Enum):
    """
    How strong the evidence is that a synthesis failure reflects a defect in
    HowlPlane/HowlFrame itself rather than one model's bad output (#59.1
    Phase 5).

    Campaign DOGFOOD-20260822-005043-16adca classified an agy repair-budget
    exhaustion as the framework gap HF_GAP_NOTES and opened a self-improvement
    task -- then devin_cli synthesized the same benchmark successfully with no
    framework change at all. One provider failing to generate working code is
    not evidence that the framework is broken.

    Deciding "I must modify HowlPlane" therefore requires a strictly higher
    evidence bar than deciding "this generated application needs another
    attempt". Only deterministic sources -- the capability negotiator, the
    compiler, the acceptance runner's own check identity -- can raise the bar.
    No amount of model opinion can.
    """

    # A deterministic component proved the framework cannot do what the spec
    # requires. Sufficient on its own.
    DETERMINISTIC_FRAMEWORK_PROOF = "DETERMINISTIC_FRAMEWORK_PROOF"
    # Ambiguous: a model produced output that did not work. Not yet actionable.
    PROVISIONAL_FAILURE = "PROVISIONAL_FAILURE"
    # An independent provider succeeded on the same benchmark: the framework
    # is fine, that provider is not. Never triggers self-modification.
    PROVIDER_SPECIFIC_FAILURE = "PROVIDER_SPECIFIC_FAILURE"
    # Independent providers reproduced the same deterministic symptom.
    VERIFIED_FRAMEWORK_GAP = "VERIFIED_FRAMEWORK_GAP"


# Failure classes that may open a governed self-improvement task against
# HowlPlane itself. Everything else is remediated as generated-application work.
_GAP_ACTIONABLE_EVIDENCE = frozenset({
    FrameworkGapEvidence.DETERMINISTIC_FRAMEWORK_PROOF,
    FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP,
})


# Canonical suite of prompt-to-product benchmarks
STANDARD_BENCHMARKS: Dict[str, str] = {
    "notes": (
        "Create a persistent notes application. Users should be able to create, view, "
        "edit, and delete notes. Provide a browser interface and a JSON HTTP API. "
        "Notes must survive application restart. Reject invalid note data."
    ),
    "todo": (
        "Create a persistent todo task management application with browser UI and REST API. "
        "Users can create tasks, mark them complete, and delete them. State persists across restarts."
    ),
    "status_api": (
        "Create a service status and health check API with a lightweight dashboard interface. "
        "Provide /health and /api/status endpoints with uptime and metric reporting."
    ),
    "inventory": (
        "Create a local inventory tracking application with browser UI and JSON endpoints. "
        "Track item quantities, categories, and persist changes locally."
    ),
    "json_transform": (
        "Create a JSON transformation utility with HTTP POST endpoint to normalize, "
        "validate, and store structured data records."
    ),
}


@dataclass
class DogfoodIterationResult(DataClassSerializationMixin):
    """Result of a single dogfood benchmark synthesis iteration."""

    benchmark_id: str
    prompt: str
    product_name: str
    success: bool
    status: str
    passed_checks: int
    total_checks: int
    repair_cycles: int
    duration_seconds: float
    implementing_provider: Optional[str] = None
    reviewing_providers: List[str] = field(default_factory=list)
    diversity_achieved: bool = True
    framework_gaps_count: int = 0
    error_message: Optional[str] = None
    bundle_path: Optional[str] = None
    retried_after_fix: bool = False


@dataclass
class MarathonSummaryReport(DataClassSerializationMixin):
    """Aggregated summary of a marathon dogfooding run."""

    campaign_id: str
    iterations_attempted: int
    iterations_succeeded: int
    iterations_failed: int
    total_duration_seconds: float
    benchmark_results: List[DogfoodIterationResult] = field(default_factory=list)
    provider_states: Dict[str, str] = field(default_factory=dict)
    provider_invocations: Dict[str, int] = field(default_factory=dict)
    framework_gaps: List[Dict[str, Any]] = field(default_factory=list)
    completed_engineering_tasks: List[Dict[str, Any]] = field(default_factory=list)
    git_records: List[Dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = "completed_all_benchmarks"
    next_action: str = "none"
    state_dir: Optional[str] = None
    schema: str = MARATHON_SCHEMA_VERSION

    def render_markdown(self) -> str:
        if self.state_dir:
            summary_file = Path(self.state_dir) / "campaign_summary.md"
            if summary_file.is_file():
                return summary_file.read_text(encoding="utf-8")
        state = DurableCampaignState(
            campaign_id=self.campaign_id,
            iterations_attempted=self.iterations_attempted,
            iterations_succeeded=self.iterations_succeeded,
            iterations_failed=self.iterations_failed,
            total_duration_seconds=self.total_duration_seconds,
            benchmark_history=[r.to_dict() for r in self.benchmark_results],
            provider_states=self.provider_states,
            provider_invocations=self.provider_invocations,
            framework_gaps=self.framework_gaps,
            completed_tasks=self.completed_engineering_tasks,
            git_records=self.git_records,
            stop_reason=self.stopped_reason,
            next_action=self.next_action,
        )
        return state.render_markdown()


class MarathonDogfoodEngine:
    """
    Drives continuous, evidence-backed dogfooding loops across product benchmarks.
    Handles real AI provider execution, exhaustion fallback, failure classification,
    governed self-improvement task delegation, git/PR integration, and durable state persistence.
    """

    def __init__(
        self,
        synthesizer: Optional[ProductSynthesizer] = None,
        provider_pool: Optional[ProviderPoolManager] = None,
        ledger: Optional[EvidenceLedger] = None,
        base_output_dir: Union[str, Path] = "output",
        campaign_dir: Optional[Union[str, Path]] = None,
        target_repo: Union[str, Path] = ".",
        repo_slug: Optional[str] = None,
        git_executor_factory: Optional[Callable[..., GitIntegrationExecutor]] = None,
        orchestrator_factory: Optional[Callable[[OrchestrationConfig], GovernedTaskOrchestrator]] = None,
        git_runner: Optional[Callable[..., Any]] = None,
        framework_gap_confirmations_required: int = 1,
    ):
        self.provider_pool = provider_pool or ProviderPoolManager()
        self.ledger = ledger
        self.synthesizer = synthesizer or ProductSynthesizer(provider_pool=self.provider_pool, ledger=self.ledger)
        self.base_output_dir = Path(base_output_dir).resolve()
        self.campaign_base_dir = (
            Path(campaign_dir).resolve()
            if campaign_dir
            else Path(".dogfood_runs").resolve()
        )
        # Real git/GitHub integration target (#59). Defaults to the current
        # working directory (the howlplane repo itself, for self-improvement
        # tasks). `repo_slug` is auto-detected from the `origin` remote when
        # not explicitly provided.
        self.target_repo = Path(target_repo).resolve()
        self.repo_slug = repo_slug or detect_repo_slug(self.target_repo) or ""
        self.authority_envelope: Optional[AuthorityEnvelope] = None
        # Injectable for tests; defaults to the real GitIntegrationExecutor.
        self._git_executor_factory = git_executor_factory or (
            lambda envelope, merges_so_far: GitIntegrationExecutor(
                self.target_repo, self.repo_slug, envelope, merges_so_far=merges_so_far
            )
        )
        self.git_executor: Optional[GitIntegrationExecutor] = None
        # Injectable for tests (mirrors GovernedTaskOrchestrator's own
        # custom_backend seam); defaults to constructing the real
        # orchestrator against self.target_repo. The orchestrator's own
        # lifecycle (locks, routing, review, verification) is exhaustively
        # covered by its dedicated test suite -- this seam lets marathon-
        # level tests exercise the real git/GitHub continuation logic
        # without re-driving that whole subsystem end-to-end.
        self._orchestrator_factory = orchestrator_factory or (
            lambda config: GovernedTaskOrchestrator(target_repo=self.target_repo, config=config)
        )
        # Raw git seam used only by attempt-state reconciliation (#59.1
        # Phase 3), kept separate from the authority-gated GitIntegrationExecutor:
        # reverting a failed attempt's own files is repository hygiene, not a
        # proposed action against the envelope.
        self._git_runner = git_runner or run_git
        # How many *independent* providers must reproduce an ambiguous synthesis
        # failure before it may be called a framework gap (#59.1 Phase 5).
        self.framework_gap_confirmations_required = max(0, framework_gap_confirmations_required)

    def _generate_campaign_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rand = hashlib.sha256(os.urandom(8)).hexdigest()[:6]
        return f"DOGFOOD-{ts}-{rand}"

    def _bind_authority_envelope(
        self,
        state_dir: Path,
        campaign_id: str,
        authority_profile_id: Optional[str],
        is_resume: bool,
    ) -> Optional[AuthorityEnvelope]:
        """
        Binds delegated overnight authority for this campaign run (#59
        Phases 8/15). `create_envelope`/`save_envelope` are only ever
        invoked from here, driven by the `authority_profile_id` value that
        arrived from the operator's CLI invocation -- never from AI-
        generated text or task output.
        """
        operator_origin = f"cli:{getpass.getuser()}@{socket.gethostname()}"

        if not is_resume:
            if not authority_profile_id:
                return None
            envelope = create_envelope(get_profile(authority_profile_id), campaign_id, operator_origin)
            save_envelope(envelope, state_dir)
            return envelope

        try:
            existing = load_envelope(state_dir)
        except EnvelopeNotFoundError:
            existing = None
        except TamperedEnvelopeError:
            # Fail closed: a tampered envelope grants nothing, regardless of
            # whether an explicit profile was also passed this call.
            return None

        if authority_profile_id:
            if existing is not None and existing.profile_id != authority_profile_id:
                raise ValueError(
                    "Requested authority profile does not match the saved "
                    f"envelope: {authority_profile_id!r} != {existing.profile_id!r}"
                )
            if existing is not None and not is_expired(existing):
                return existing
            # Reauthorization may renew the same profile, but cannot silently
            # substitute a different authority policy for the saved campaign.
            envelope_path = state_dir / ENVELOPE_FILENAME
            if envelope_path.is_file():
                envelope_path.unlink()
            envelope = create_envelope(
                get_profile(authority_profile_id), campaign_id, operator_origin
            )
            save_envelope(envelope, state_dir)
            return envelope

        if existing is None or is_expired(existing):
            # Expired (or never authorized) and no explicit reauthorization
            # on this resume call: never silently renew. The campaign
            # proceeds read/propose-only -- envelope-gated tasks will park.
            return None
        return existing

    def run_marathon(
        self,
        benchmarks: Optional[List[str]] = None,
        max_iterations: int = 5,
        until_providers_exhausted: bool = False,
        avoid_provider: Optional[str] = None,
        preferred_agent: Optional[str] = None,
        resume_campaign_id: Optional[str] = None,
        authority_profile_id: Optional[str] = None,
    ) -> MarathonSummaryReport:
        """
        Executes evidence-driven marathon dogfooding.
        Respects --until-providers-exhausted by running as long as providers remain available,
        with a high finite safety ceiling to avoid unbounded spins.
        """
        t0 = time.time()
        self.base_output_dir.mkdir(parents=True, exist_ok=True)
        self.campaign_base_dir.mkdir(parents=True, exist_ok=True)

        # 1. Initialize or Resume Durable Campaign State
        campaign_id = resume_campaign_id or self._generate_campaign_id()
        state_dir = self.campaign_base_dir / campaign_id
        state_dir.mkdir(parents=True, exist_ok=True)

        scope_extended: List[str] = []
        if resume_campaign_id and (state_dir / "campaign_state.json").is_file():
            campaign_state = DurableCampaignState.load(state_dir)
            # Re-check LIVE provider availability upon resume: previously exhausted
            # cloud providers may have refreshed quota (#58 Phase 16).
            self.provider_pool.reset_transient_exhaustion()

            # Restore the campaign's PERSISTED benchmark scope. A resume must NOT
            # silently expand into the full default benchmark suite (#58 Phase 1).
            # Campaigns persisted before #58 have no `requested_benchmarks`; fall
            # back to the full default suite once, for backward compatibility.
            persisted_scope = campaign_state.requested_benchmarks or list(STANDARD_BENCHMARKS.keys())
            if benchmarks:
                # Explicit --benchmarks on a resume is treated as an operator-driven
                # scope EXTENSION, not a silent expansion or an override.
                scope_extended = campaign_state.extend_benchmark_scope(benchmarks)
                benchmark_keys_scope = campaign_state.requested_benchmarks
            else:
                benchmark_keys_scope = persisted_scope
            campaign_state.requested_benchmarks = benchmark_keys_scope

            # Skip benchmarks already verified successful in a prior session; retry
            # any that previously failed, plus anything newly added to scope.
            already_succeeded = {
                b.get("benchmark_id") for b in campaign_state.benchmark_history if b.get("success")
            }
            benchmark_keys = [b for b in benchmark_keys_scope if b not in already_succeeded]
        else:
            campaign_state = DurableCampaignState(campaign_id=campaign_id)
            benchmark_keys = benchmarks or list(STANDARD_BENCHMARKS.keys())
            campaign_state.requested_benchmarks = list(benchmark_keys)

        is_resume = bool(resume_campaign_id and (state_dir / "campaign_state.json").is_file())
        self.authority_envelope = self._bind_authority_envelope(
            state_dir, campaign_id, authority_profile_id, is_resume
        )
        campaign_state.authority_envelope = self.authority_envelope.to_dict() if self.authority_envelope else None
        self.git_executor = self._git_executor_factory(self.authority_envelope, campaign_state.merges_this_campaign)

        if self.authority_envelope is not None:
            # Overnight-safe envelope bound: use its (more conservative)
            # local RAM threshold and keep_alive behavior for local Ollama
            # inference rather than the interactive default (#59 Phases
            # 16/17). Registered for the lifetime of this process; a fresh
            # process picks the interactive default again unless it too
            # binds an envelope.
            from src.control_plane.agent_execution import OllamaLocalBackend
            AgentBackendRegistry.register_backend(
                "local_ollama",
                OllamaLocalBackend(
                    min_available_ram_gib=self.authority_envelope.local_ram_threshold_gib,
                    keep_alive=self.authority_envelope.local_keep_alive,
                ),
            )
            lm = campaign_state.ensure_local_model_defaults()
            lm["resource_guard_min_ram_gib"] = self.authority_envelope.local_ram_threshold_gib

        # When --until-providers-exhausted is True, do not silently cap at 5;
        # set high safety ceiling (e.g. 100) to allow long autonomous campaigns
        if until_providers_exhausted:
            effective_max_iterations = max(max_iterations, 100)
        else:
            effective_max_iterations = max_iterations

        results: List[DogfoodIterationResult] = []
        stop_reason = "completed_all_benchmarks"

        iteration_idx = 0
        b_index = 0

        while iteration_idx < effective_max_iterations:
            if b_index >= len(benchmark_keys):
                stop_reason = "completed_all_benchmarks"
                break

            b_key = benchmark_keys[b_index]
            b_index += 1
            iteration_idx += 1

            prompt = STANDARD_BENCHMARKS.get(b_key, b_key)
            out_dir = self.base_output_dir / f"dogfood_{b_key}"

            campaign_state.active_benchmark = b_key
            campaign_state.provider_states = self.provider_pool.get_all_statuses()
            campaign_state.save(state_dir)

            # Check provider pool capacity. Full-app benchmark synthesis is a broad
            # task and never local-eligible (#58 Phase 9), so probe with a
            # medium-risk task to correctly exclude the quota-free local model from
            # this capacity check -- otherwise a machine with Ollama installed would
            # never observe "exhausted" and the campaign could run indefinitely.
            benchmark_task_probe = TaskSpec(
                task_id=f"BENCH-{b_key.upper()}", repository="dogfood",
                objective=prompt, task_class="feature", risk_level="medium",
            )
            available_candidates = self.provider_pool.select_candidates(
                task_category="code_heavy",
                avoid_provider=avoid_provider,
                preferred_agent=preferred_agent,
                task=benchmark_task_probe,
            )
            # `is_all_exhausted()` (true SESSION_EXHAUSTED/RATE_LIMITED quota
            # exhaustion) always stops, even in deterministic-baseline mode, same
            # as before #58. Local Ollama can never reach that state, so this
            # branch is unaffected by its presence. Lack of any *candidate*
            # (e.g. no real provider binaries installed) only stops real runs;
            # deterministic-baseline mode (used in CI/tests) synthesizes
            # regardless of provider availability, exactly as before #58.
            if self.provider_pool.is_all_exhausted() or (
                not available_candidates and self.synthesizer.synthesis_mode != "deterministic_baseline"
            ):
                stop_reason = "all_providers_exhausted"
                break

            # Execute Prompt-to-Product Synthesis
            synth_res = self._execute_synthesis(prompt, out_dir, avoid_provider, preferred_agent, iteration_idx)

            # Track provider invocation
            if synth_res.implementing_provider:
                campaign_state.record_provider_invocation(synth_res.implementing_provider)

            acc_passed = synth_res.acceptance_report.passed_count if synth_res.acceptance_report else 0
            acc_total = synth_res.acceptance_report.total_count if synth_res.acceptance_report else 0

            # Record reviewer diversity, distinguishing which reviewers were
            # merely assigned a role from which actually executed (#59.1
            # Phase 8). Counters only move for real executions.
            invocations = list(synth_res.reviewer_invocations)
            campaign_state.reviewer_diversity_records.append({
                "target": b_key,
                "implementer": synth_res.implementing_provider,
                "reviewers": synth_res.reviewer_mapping,
                "diversity_achieved": synth_res.diversity_achieved,
                "assigned": sorted({i["provider"] for i in invocations}),
                "invoked": sorted({i["provider"] for i in invocations if i.get("invoked")}),
                "completed": sorted({i["provider"] for i in invocations if i.get("completed")}),
                "invocations": invocations,
            })
            for inv in invocations:
                if not inv.get("invoked"):
                    continue
                campaign_state.record_provider_invocation(inv["provider"], role="review")
                if inv["provider"] in LOCAL_PROVIDER_IDS:
                    recorder = (
                        campaign_state.record_local_success if inv.get("completed")
                        else campaign_state.record_local_failure
                    )
                    recorder(inv.get("ram_after_gib"))

            # 3. Check for Framework Gap / Failure -> Trigger Self-Improvement Flywheel
            retried = False
            bundle_dir = out_dir
            if not synth_res.success:
                # Classify the observed failure from concrete evidence, and
                # grade that evidence. Only deterministic proof, or a symptom
                # independently reproduced by another provider, may open a
                # self-improvement task against HowlPlane itself (#59.1
                # Phase 5).
                gap_type, gap_desc, evidence = self._classify_failure(synth_res, b_key)
                falsification: List[Dict[str, Any]] = []
                if evidence == FrameworkGapEvidence.PROVISIONAL_FAILURE:
                    signature = self._failure_signature(synth_res, gap_type)
                    evidence, rescued, rescued_dir, falsification = self._falsify_framework_gap(
                        prompt, b_key, out_dir, synth_res.implementing_provider,
                        signature, iteration_idx,
                    )
                    if rescued is not None:
                        # An independent provider synthesized the same
                        # benchmark successfully: the framework was never the
                        # problem. Adopt that result -- and the directory it
                        # was actually built into -- as the outcome.
                        synth_res = rescued
                        bundle_dir = rescued_dir or bundle_dir
                        acc_passed = rescued.acceptance_report.passed_count if rescued.acceptance_report else 0
                        acc_total = rescued.acceptance_report.total_count if rescued.acceptance_report else 0
                        if rescued.implementing_provider:
                            campaign_state.record_provider_invocation(rescued.implementing_provider)

                campaign_state.record_gap_falsification({
                    "benchmark": b_key,
                    "gap_type": gap_type,
                    "evidence": evidence.value,
                    "failing_provider": synth_res.implementing_provider,
                    "attempts": falsification,
                })

            if not synth_res.success and evidence not in _GAP_ACTIONABLE_EVIDENCE:
                # Not enough evidence to modify HowlPlane. Record what was
                # observed for future provider routing and fall through to the
                # normal iteration result -- no framework gap, no governed
                # self-improvement task, no self-modification.
                campaign_state.record_provider_specific_failure({
                    "benchmark": b_key,
                    "gap_type": gap_type,
                    "evidence": evidence.value,
                    "failing_provider": synth_res.implementing_provider,
                    "required_behavior": gap_desc,
                    "falsification_attempts": falsification,
                })
                campaign_state.save(state_dir)

            elif not synth_res.success:
                gap_code = f"HF_GAP_{b_key.upper()}"
                campaign_state.record_framework_gap({
                    "code": gap_code,
                    "target_component": "howlframe_runtime" if "runtime" in gap_type else "howlplane_synthesis",
                    "required_behavior": gap_desc,
                    "impact": "blocks_product_synthesis",
                    "evidence": evidence.value,
                    "falsification_attempts": falsification,
                })

                # Create concrete, bounded engineering task
                eng_task_id = f"ENG-{b_key.upper()}-{iteration_idx:02d}"
                campaign_state.active_engineering_task = eng_task_id

                if already_parked(campaign_state.parked_tasks, eng_task_id):
                    # Never repeatedly re-select an already-parked task (#59 Phase 13).
                    campaign_state.save(state_dir)
                    continue

                # Execute governed engineering task to resolve gap
                task_success, git_rec = self._execute_governed_engineering_improvement(
                    task_id=eng_task_id,
                    benchmark_key=b_key,
                    gap_type=gap_type,
                    gap_desc=gap_desc,
                    avoid_provider=avoid_provider,
                    campaign_state=campaign_state,
                    state_dir=state_dir,
                )

                if task_success and git_rec:
                    campaign_state.record_task_completed({
                        "task_id": eng_task_id,
                        "objective": f"Resolve {gap_type} for {b_key}",
                        "provider": git_rec.get("provider", "codex"),
                        "remediations": 0,
                    })
                    campaign_state.record_git_integration(git_rec)

                    # RETRY ORIGINAL BENCHMARK after fix is integrated!
                    retry_res = self._execute_synthesis(prompt, out_dir, avoid_provider, preferred_agent, iteration_idx)
                    if retry_res.success:
                        synth_res = retry_res
                        acc_passed = synth_res.acceptance_report.passed_count if synth_res.acceptance_report else 0
                        acc_total = synth_res.acceptance_report.total_count if synth_res.acceptance_report else 0
                        retried = True
                elif git_rec and git_rec.get("integration_mode") == "parked":
                    # Parked awaiting human authority: not a capability
                    # failure. Preserve state and continue with other
                    # independent work unless this park blocks everything
                    # meaningful remaining (#59 Phases 12/13).
                    already_succeeded_benchmarks = [
                        b.get("benchmark_id") for b in campaign_state.benchmark_history if b.get("success")
                    ]
                    resolved_gap_codes = [
                        t.get("gap_type_resolved") for t in campaign_state.completed_tasks if t.get("gap_type_resolved")
                    ]
                    blocks_all = compute_blocks_other_work(
                        eng_task_id,
                        campaign_state.framework_gaps,
                        campaign_state.requested_benchmarks,
                        already_succeeded_benchmarks,
                        resolved_gap_codes,
                        campaign_state.parked_tasks,
                        current_gap_code=gap_code,
                    )
                    parked_record = git_rec.get("parked_record", {})
                    parked_record["blocks_other_work"] = blocks_all
                    campaign_state.record_park(parked_record)
                    campaign_state.save(state_dir)
                    if blocks_all:
                        stop_reason = "AWAITING_HUMAN"
                        break
                else:
                    # Report the engineering task's own provider and its
                    # concrete failure reason. Previously this recorded the
                    # *synthesis* provider and the fixed string "Engineering
                    # task failed or blocked", discarding the git record's
                    # failure_reason entirely (#59.1).
                    attempts = campaign_state.attempts_for_task(eng_task_id)
                    campaign_state.record_task_failed({
                        "task_id": eng_task_id,
                        "objective": f"Resolve {gap_type} for {b_key}",
                        "provider": (git_rec or {}).get("provider") or "unknown",
                        "error": (git_rec or {}).get("failure_reason") or "Engineering task failed or blocked",
                        "attempts": [
                            {"provider": a.get("provider"), "result": a.get("result")} for a in attempts
                        ],
                    })

            iter_res = DogfoodIterationResult(
                benchmark_id=b_key,
                prompt=prompt,
                product_name=synth_res.product_name,
                success=synth_res.success,
                status=synth_res.status,
                passed_checks=acc_passed,
                total_checks=acc_total,
                repair_cycles=synth_res.repair_cycles,
                duration_seconds=synth_res.duration_seconds,
                implementing_provider=synth_res.implementing_provider,
                reviewing_providers=synth_res.reviewing_providers,
                diversity_achieved=synth_res.diversity_achieved,
                framework_gaps_count=len(synth_res.framework_gaps),
                error_message=synth_res.error_message,
                bundle_path=str(bundle_dir) if synth_res.success else None,
                retried_after_fix=retried,
            )
            results.append(iter_res)
            campaign_state.record_benchmark_result(iter_res.to_dict())
            campaign_state.save(state_dir)

            if synth_res.status == "PROVIDER_POOL_EXHAUSTED":
                stop_reason = "all_providers_exhausted"
                break

        if iteration_idx >= effective_max_iterations and stop_reason == "completed_all_benchmarks" and len(results) < len(benchmark_keys):
            stop_reason = "campaign_safety_ceiling_reached"

        # Cloud exhausted: before stopping, use bounded LOCAL_ONLY_CONTINUATION to
        # mop up any outstanding, evidence-backed, local-eligible engineering gaps
        # rather than declaring the campaign done with unresolved fixable gaps
        # (#58 Phase 13). This never runs unbounded: capped by the durable
        # `local_only_iteration_limit` counter, and it never re-attempts full
        # benchmark synthesis (not local-eligible; see capacity probe above).
        if stop_reason == "all_providers_exhausted":
            local_backend = AgentBackendRegistry.get_backend("local_ollama")
            if local_backend.is_available():
                stop_reason = self._run_local_only_continuation(campaign_state, avoid_provider)

        total_elapsed = round(time.time() - t0, 3)
        succeeded = sum(1 for r in results if r.success)
        failed = len(results) - succeeded

        campaign_state.stop_reason = stop_reason
        campaign_state.total_duration_seconds = total_elapsed
        campaign_state.next_action = "none" if failed == 0 else f"ai dogfood --resume {campaign_id}"
        campaign_state.save(state_dir)

        return MarathonSummaryReport(
            campaign_id=campaign_id,
            iterations_attempted=len(results),
            iterations_succeeded=succeeded,
            iterations_failed=failed,
            total_duration_seconds=total_elapsed,
            benchmark_results=results,
            provider_states=self.provider_pool.get_all_statuses(),
            provider_invocations=campaign_state.provider_invocations,
            framework_gaps=campaign_state.framework_gaps,
            completed_engineering_tasks=campaign_state.completed_tasks,
            git_records=campaign_state.git_records,
            stopped_reason=stop_reason,
            next_action=campaign_state.next_action,
            state_dir=str(state_dir),
        )

    def run_acceptance_canary(self, authority_profile_id: str) -> Dict[str, Any]:
        """
        Live governed integration acceptance canary (#59.2 Phases 15-18).

        A first-class, explicitly labeled acceptance action -- not a
        fabricated defect -- whose only purpose is to exercise the real
        governed git/GitHub lifecycle end-to-end with no mocked boundary:
        GovernedTaskOrchestrator -> independent review -> deterministic
        verification -> real branch/commit/push/PR via GitIntegrationExecutor
        -> live GitHub Ruleset discovery -> CI poll to terminal -> delegated
        merge -> real gh merge verification -> remote-main reconciliation ->
        local-main sync. Reuses _execute_governed_engineering_improvement
        exactly as any other governed engineering task does -- no special
        shortcut, no direct git/gh call outside the production executor.

        The task is mechanically scoped via TaskSpec.allowed_paths (#59.2
        Phase 8) to touch only its designated acceptance evidence artifact
        under documentation/task_journals/ -- a fail-closed guarantee, not a
        prompt-only request. `risk_level="medium"` excludes the quota-free
        local model, since the canary's entire point is proving the
        cloud-governed path works.

        Returns a dict: campaign_id, task_id, journal_path, task_success,
        git_record. Callers should treat completion as verified only when
        git_record's merged / remote_main_contains_merge / local_main_synced
        are all true.
        """
        self.base_output_dir.mkdir(parents=True, exist_ok=True)
        self.campaign_base_dir.mkdir(parents=True, exist_ok=True)

        campaign_id = self._generate_campaign_id()
        state_dir = self.campaign_base_dir / campaign_id
        state_dir.mkdir(parents=True, exist_ok=True)

        campaign_state = DurableCampaignState(campaign_id=campaign_id)
        self.authority_envelope = self._bind_authority_envelope(
            state_dir, campaign_id, authority_profile_id, is_resume=False,
        )
        campaign_state.authority_envelope = (
            self.authority_envelope.to_dict() if self.authority_envelope else None
        )
        self.git_executor = self._git_executor_factory(
            self.authority_envelope, campaign_state.merges_this_campaign,
        )

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        journal_path = f"documentation/task_journals/{date_str}_live_autonomous_acceptance.md"
        task_id = f"ACCEPTANCE-{campaign_id}"

        start_sha_proc = self._git_runner(self.target_repo, ["rev-parse", "HEAD"], 15)
        start_sha = start_sha_proc.stdout.strip() if start_sha_proc.returncode == 0 else "unknown"
        digest = self.authority_envelope.policy_digest if self.authority_envelope else "unknown"

        task_success, git_rec = self._execute_governed_engineering_improvement(
            task_id=task_id,
            benchmark_key="LIVE-ACCEPTANCE-CANARY",
            gap_type="live_autonomous_acceptance",
            gap_desc=(
                f"Create or update {journal_path} to record that live acceptance canary "
                f"{campaign_id} was initiated: starting main SHA {start_sha}, authority "
                f"profile digest {digest}, and its purpose (exercising the real governed "
                f"branch/commit/push/PR/CI/merge/remote-verify/local-sync git lifecycle "
                f"end-to-end, with no mocked git/gh boundary). Do NOT assert that the merge "
                f"lifecycle has completed or succeeded -- at the time this file is written, "
                f"the branch/commit/PR/CI/merge steps that follow have not happened yet. The "
                f"merge outcome is independently verified by the production git integration "
                f"steps themselves, never asserted in this file's content."
            ),
            risk_level="medium",
            campaign_state=campaign_state,
            state_dir=state_dir,
            allowed_paths=[journal_path],
            reviewer_requirements=[
                "correctness-reviewer",
                "regression-reviewer",
                "simplicity-reviewer",
            ],
        )

        if task_success and git_rec:
            campaign_state.record_task_completed({
                "task_id": task_id,
                "objective": "Live autonomous acceptance canary",
                "provider": git_rec.get("provider", "unknown"),
                "remediations": 0,
            })
            campaign_state.record_git_integration(git_rec)
        campaign_state.stop_reason = "acceptance_canary_complete" if task_success else "acceptance_canary_incomplete"
        campaign_state.save(state_dir)

        return {
            "campaign_id": campaign_id,
            "task_id": task_id,
            "journal_path": journal_path,
            "task_success": task_success,
            "git_record": git_rec,
            "state_dir": str(state_dir),
        }

    def run_backlog_marathon(
        self,
        authority_profile_id: str,
        max_tasks: int = 3,
        max_runtime_hours: float = 8.0,
        resume_campaign_id: Optional[str] = None,
        roi_floor: float = 0.5,
        backlog_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Works a bounded sequence of a project's ranked backlog items.

        This is the marathon the control plane did not previously have.
        `run_marathon` iterates *product-synthesis benchmarks*; this iterates
        the target repository's own ranked backlog, which is what
        `select_next_evidence_backed_task` in the authority profile always
        meant and never had an implementation for.

        Every task goes through `_execute_governed_engineering_improvement`,
        so it gets the identical real lifecycle as any other governed task:
        branch, provider implementation with bounded failover, independent
        review, deterministic verification, commit, push, PR, CI observation,
        and merge only if the bound envelope actually delegates it. Nothing
        here is a shortcut around that.

        Bounded four ways, all of which stop the loop truthfully rather than
        letting it run on:

        * `max_tasks` -- how many items may be attempted at all
        * `max_runtime_hours` -- wall clock, checked before starting each task
          so a task is never begun that cannot plausibly finish
        * the eligible backlog -- when it empties, the run stops
        * the usable provider pool -- when it empties, the run parks

        A task whose authority boundary the envelope does not delegate is
        PARKED and the loop continues with other independent work, which is
        the `park_and_continue` action the profile grants. The campaign only
        stops for a human when nothing else can proceed.
        """
        from src.control_plane.backlog_source import BacklogSource

        started = time.time()
        deadline = started + max_runtime_hours * 3600.0
        self.base_output_dir.mkdir(parents=True, exist_ok=True)
        self.campaign_base_dir.mkdir(parents=True, exist_ok=True)

        campaign_id = resume_campaign_id or self._generate_campaign_id()
        state_dir = self.campaign_base_dir / campaign_id
        state_dir.mkdir(parents=True, exist_ok=True)

        if resume_campaign_id and (state_dir / "campaign_state.json").is_file():
            campaign_state = DurableCampaignState.load(state_dir)
            # A resumed run re-probes providers whose cooldown has elapsed
            # rather than inheriting a stale exhausted view of the pool.
            self.provider_pool.reset_transient_exhaustion()
        else:
            campaign_state = DurableCampaignState(campaign_id=campaign_id)

        self.authority_envelope = self._bind_authority_envelope(
            state_dir, campaign_id, authority_profile_id,
            is_resume=bool(resume_campaign_id),
        )
        campaign_state.authority_envelope = (
            self.authority_envelope.to_dict() if self.authority_envelope else None
        )
        self.git_executor = self._git_executor_factory(
            self.authority_envelope, campaign_state.merges_this_campaign,
        )

        source = BacklogSource(
            self.target_repo,
            backlog_files=backlog_files or list(DEFAULT_BACKLOG_FILES),
            roi_floor=roi_floor,
        )
        worked: List[str] = []
        attempted_ids: List[str] = [
            task.get("task_id") for task in campaign_state.completed_tasks
        ] + [task.get("task_id") for task in campaign_state.failed_tasks]
        attempted_ids += [park.get("task_id") for park in campaign_state.parked_tasks]
        attempted_ids = [task_id for task_id in attempted_ids if task_id]

        stop_reason = "max_tasks_reached"
        while len(worked) < max_tasks:
            if time.time() >= deadline:
                stop_reason = "max_runtime_reached"
                break
            if not self.provider_pool.has_available_providers():
                # Truthful park, not a loop: nothing usable remains to ask.
                stop_reason = "resources_unavailable"
                break

            selection = source.select()
            item = selection.next_item(skip=attempted_ids)
            if item is None:
                stop_reason = "backlog_exhausted"
                break

            attempted_ids.append(item.task_id)
            detail = source.item_detail(item)
            success, git_rec = self._execute_governed_engineering_improvement(
                task_id=item.task_id,
                benchmark_key=item.task_id,
                gap_type="backlog_item",
                gap_desc=item.title,
                objective_override=(
                    f"Resolve {item.source_file} item #{item.item_id}: {item.title}\n\n"
                    f"{detail}"
                ),
                task_class="bug_fix" if item.kind == "bug" else "feature",
                commit_message_override=(
                    f"fix({item.source_file.replace('.md', '')} #{item.item_id}): "
                    f"{item.title[:60]}"
                ),
                risk_level="medium",
                campaign_state=campaign_state,
                state_dir=state_dir,
            )
            worked.append(item.task_id)

            record = {
                "task_id": item.task_id,
                "objective": item.title,
                "source_file": item.source_file,
                "item_id": item.item_id,
                "score": item.score,
                "provider": (git_rec or {}).get("provider", "unknown"),
            }
            if (git_rec or {}).get("integration_mode") == "parked":
                campaign_state.record_park((git_rec or {}).get("parked_record", record))
            elif success:
                campaign_state.record_task_completed(record)
            else:
                campaign_state.record_task_failed(record)
            if git_rec and "parked_record" not in git_rec:
                campaign_state.record_git_integration(git_rec)
            campaign_state.save(state_dir)

        campaign_state.stop_reason = stop_reason
        campaign_state.total_duration_seconds = round(time.time() - started, 3)
        campaign_state.save(state_dir)

        return {
            "campaign_id": campaign_id,
            "state_dir": str(state_dir),
            "target_repo": str(self.target_repo),
            "repo_slug": self.repo_slug,
            "authority_profile": authority_profile_id,
            "tasks_attempted": worked,
            "tasks_completed": [t.get("task_id") for t in campaign_state.completed_tasks],
            "tasks_failed": [t.get("task_id") for t in campaign_state.failed_tasks],
            "tasks_parked": [p.get("task_id") for p in campaign_state.parked_tasks],
            "git_records": campaign_state.git_records,
            "stop_reason": stop_reason,
            "duration_seconds": campaign_state.total_duration_seconds,
        }

    def _run_local_only_continuation(self, campaign_state: DurableCampaignState, avoid_provider: Optional[str]) -> str:
        """
        Bounded local-only engineering continuation, entered only once every cloud
        provider is exhausted/unavailable (#58 Phase 13). Attempts to resolve
        outstanding, evidence-backed framework gaps (recorded during this
        campaign) using the quota-free local model, one gap per iteration, up to
        `local_only_iteration_limit` consecutive iterations. Never retried
        indefinitely: a local failure on a gap escalates immediately rather than
        looping, since no cloud provider remains available to escalate to right
        now -- that gap is simply left unresolved for a future resumed session.
        Returns the final stop_reason.
        """
        lm = campaign_state.ensure_local_model_defaults()
        resolved_codes = {t.get("gap_type_resolved") for t in campaign_state.completed_tasks if t.get("gap_type_resolved")}
        pending_gaps = [g for g in campaign_state.framework_gaps if g.get("code") not in resolved_codes]

        state_dir = self.campaign_base_dir / campaign_state.campaign_id

        for gap in pending_gaps:
            if campaign_state.local_only_budget_reached():
                campaign_state.save(state_dir)
                return "local_only_budget_reached"

            if lm.get("overnight_ram_exhausted"):
                # #59 Phase 17: RAM remained dangerously low after a prior
                # unload this session -- stop attempting further local work
                # rather than repeatedly reloading the model.
                campaign_state.save(state_dir)
                return "local_resource_constrained"

            gap_task_id = f"LOCAL-{gap.get('code', 'GAP')}"
            if already_parked(campaign_state.parked_tasks, gap_task_id):
                continue

            ram_before = read_available_memory_gib()
            lm["last_available_ram_gib"] = ram_before
            iteration_no = campaign_state.increment_local_only_iteration()

            task_id = f"LOCAL-{gap.get('code', 'GAP')}-{iteration_no:02d}"
            task_success, git_rec = self._execute_governed_engineering_improvement(
                task_id=task_id,
                benchmark_key=gap.get("code", "unknown"),
                gap_type=gap.get("code", "UNKNOWN_GAP"),
                gap_desc=gap.get("required_behavior", ""),
                avoid_provider=avoid_provider,
                risk_level="low",
                campaign_state=campaign_state,
                state_dir=state_dir,
            )

            if (
                self.authority_envelope is not None and git_rec
                and git_rec.get("provider") == "local_ollama"
                and git_rec.get("integration_mode") != "parked"
            ):
                # #59 Phase 17: if available memory remains dangerously low
                # after this local inference's (possible) unload, stop
                # attempting further local work for the rest of this
                # session's local-only continuation rather than repeatedly
                # reloading the model against an already-thin margin.
                ram_after = read_available_memory_gib()
                lm["last_available_ram_gib"] = ram_after
                if ram_after is not None and ram_after < self.authority_envelope.local_ram_threshold_gib:
                    lm["overnight_ram_exhausted"] = True

            if not task_success and git_rec and git_rec.get("integration_mode") == "parked":
                # Parked awaiting human authority: not a local-capability
                # failure -- don't count it against the local-only budget's
                # failure/escalation accounting. Continue with other gaps.
                parked_record = git_rec.get("parked_record", {})
                parked_record["task_id"] = gap_task_id
                campaign_state.record_park(parked_record)
                campaign_state.save(state_dir)
                continue

            if task_success and git_rec and git_rec.get("provider") == "local_ollama":
                campaign_state.record_local_success(ram_before)
                git_rec["gap_type_resolved"] = gap.get("code")
                campaign_state.record_task_completed({
                    "task_id": task_id,
                    "objective": f"[local_ollama] Resolve {gap.get('code')}",
                    "provider": "local_ollama",
                    "remediations": 0,
                    "gap_type_resolved": gap.get("code"),
                })
                campaign_state.record_git_integration(git_rec)
            elif task_success and git_rec:
                # A cloud provider became available mid-continuation; that's fine,
                # but it means local-only continuation is no longer necessary.
                campaign_state.record_task_completed({
                    "task_id": task_id, "objective": f"Resolve {gap.get('code')}",
                    "provider": git_rec.get("provider"), "remediations": 0,
                })
                campaign_state.record_git_integration(git_rec)
                campaign_state.reset_local_only_iterations()
            else:
                campaign_state.record_local_failure(ram_before)
                campaign_state.record_local_escalation()
                campaign_state.record_task_failed({
                    "task_id": task_id, "objective": f"Resolve {gap.get('code')}",
                    "provider": "local_ollama", "error": "LOCAL_CAPABILITY_INSUFFICIENT: no eligible provider remained",
                })

            campaign_state.save(state_dir)

        lm["current_availability"] = "AVAILABLE"
        return "all_providers_exhausted"

    def _execute_synthesis(
        self,
        prompt: str,
        out_dir: Path,
        avoid_provider: Optional[str],
        preferred_agent: Optional[str],
        iteration_idx: int,
    ) -> SynthesisResult:
        return self.synthesizer.create_from_prompt(
            prompt=prompt,
            output_dir=out_dir,
            avoid_provider=avoid_provider,
            preferred_agent=preferred_agent,
            port=8088 + (iteration_idx % 50),
        )

    def _classify_failure(
        self, synth_res: SynthesisResult, benchmark_key: str
    ) -> Tuple[str, str, FrameworkGapEvidence]:
        """
        Classifies an observed synthesis failure from concrete evidence, and
        grades how strong that evidence is (#59.1 Phase 5).

        The three deterministic sources -- the capability negotiator declaring
        a gap, the negotiator blocking the product outright, and the compiler
        rejecting required syntax -- prove something about the framework
        independently of whatever any model generated, so they are actionable
        immediately. A failing acceptance run or a general orchestration
        failure only proves that *this attempt* did not work, so it starts out
        provisional and must survive cross-provider falsification first.
        """
        proof = FrameworkGapEvidence.DETERMINISTIC_FRAMEWORK_PROOF
        provisional = FrameworkGapEvidence.PROVISIONAL_FAILURE
        if synth_res.framework_gaps:
            return "HOWLFRAME_RUNTIME_GAP", synth_res.framework_gaps[0].required_behavior, proof
        if synth_res.status == "PRODUCT_BLOCKED":
            desc = synth_res.error_message or "Product blocked by capability negotiator"
            return "HOWLFRAME_CAPABILITY_GAP", desc, proof
        if synth_res.error_message and "Compiler error" in synth_res.error_message:
            return "HOWLFRAME_COMPILER_GAP", synth_res.error_message, proof
        if synth_res.error_message and "Acceptance failure" in synth_res.error_message:
            return "SYNTHESIS_REPAIR_FAIL", synth_res.error_message, provisional
        desc = synth_res.error_message or f"General synthesis failure on {benchmark_key}"
        return "HOWLPLANE_ORCHESTRATION_GAP", desc, provisional

    @staticmethod
    def _failure_signature(synth_res: SynthesisResult, gap_type: str) -> str:
        """
        A normalized, deterministic identity for a synthesis failure, used to
        decide whether two providers failed the *same* way.

        Deliberately built from structural facts -- the classified gap type and
        the identity of the acceptance check that failed -- rather than
        free-text message equality, which would vary with model wording,
        timings and paths.
        """
        detail = ""
        message = synth_res.error_message or ""
        marker = "Acceptance failure:"
        if marker in message:
            # "...Acceptance failure: server_health_probe: Server did not..."
            remainder = message.split(marker, 1)[1].strip()
            detail = remainder.split(":", 1)[0].strip()
        elif synth_res.framework_gaps:
            detail = synth_res.framework_gaps[0].code
        return f"{gap_type}|{detail}"

    def _falsify_framework_gap(
        self,
        prompt: str,
        benchmark_key: str,
        out_dir: Path,
        failing_provider: Optional[str],
        signature: str,
        iteration_idx: int,
    ) -> Tuple[FrameworkGapEvidence, Optional[SynthesisResult], Optional[Path], List[Dict[str, Any]]]:
        """
        Tries to falsify a provisional framework gap by re-synthesizing the
        same benchmark with independent providers (#59.1 Phase 5).

        Bounded by `framework_gap_confirmations_required` (default 1), so this
        never becomes an unbounded retry loop, and it runs inside the current
        benchmark iteration rather than consuming iteration budget.

        Returns (evidence, succeeding_result, its_output_dir, attempt_records):
          * an independent provider SUCCEEDS -> PROVIDER_SPECIFIC_FAILURE, plus
            the successful result and the directory it was built into, which
            the caller uses as the benchmark's outcome and bundle path. No
            framework gap, no self-modification.
          * an independent provider reproduces the SAME deterministic
            signature -> VERIFIED_FRAMEWORK_GAP.
          * it fails differently, or no independent provider exists ->
            PROVISIONAL_FAILURE. Still not enough to modify HowlPlane.
        """
        attempts: List[Dict[str, Any]] = []
        confirmations = 0
        tried = {failing_provider} if failing_provider else set()

        # Falsification re-runs full product synthesis, which is never
        # local-eligible (#58 Phase 9). Exclude local providers here so the
        # attempt is attributed to the provider actually asked to do the work
        # rather than silently reassigned inside the synthesizer.
        probe = TaskSpec(
            task_id=f"FALSIFY-{benchmark_key.upper()}", repository="howlplane",
            objective=f"Independently re-synthesize {benchmark_key}",
            task_class="feature", risk_level="medium",
        )
        while confirmations < self.framework_gap_confirmations_required:
            candidates = [
                c for c in self.provider_pool.select_candidates(
                    task_category="code_heavy", task=probe,
                )
                if c not in tried and c not in LOCAL_PROVIDER_IDS
            ]
            if not candidates:
                return FrameworkGapEvidence.PROVISIONAL_FAILURE, None, None, attempts

            provider = candidates[0]
            tried.add(provider)
            confirm_dir = out_dir.parent / f"{out_dir.name}_falsify{len(attempts) + 1}"
            confirm_res = self._execute_synthesis(
                prompt, confirm_dir, failing_provider, provider, iteration_idx,
            )
            if confirm_res.success:
                attempts.append({"provider": provider, "outcome": "SUCCESS"})
                return (
                    FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE, confirm_res, confirm_dir, attempts,
                )

            confirm_type, _desc, confirm_evidence = self._classify_failure(confirm_res, benchmark_key)
            confirm_signature = self._failure_signature(confirm_res, confirm_type)
            same_symptom = confirm_signature == signature
            attempts.append({
                "provider": provider,
                "outcome": "FAILED_SAME_SYMPTOM" if same_symptom else "FAILED_DIFFERENT_SYMPTOM",
                "signature": confirm_signature,
            })
            if confirm_evidence == FrameworkGapEvidence.DETERMINISTIC_FRAMEWORK_PROOF:
                # An independent provider hit a deterministic framework wall.
                return FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP, None, None, attempts
            if not same_symptom:
                # Two providers failing differently is evidence about the
                # providers, not about the framework.
                return FrameworkGapEvidence.PROVIDER_SPECIFIC_FAILURE, None, None, attempts
            confirmations += 1

        return FrameworkGapEvidence.VERIFIED_FRAMEWORK_GAP, None, None, attempts

    def _persist_git_record(
        self,
        git_rec: "GitIntegrationRecord",
        campaign_state: Optional[DurableCampaignState],
        state_dir: Optional[Path],
    ) -> None:
        """
        Incrementally persists a GitIntegrationRecord's current state
        (#59 Phase 2/22): saved after each real step, not just once at the
        end, so crash-resume reconciliation has an accurate last-known
        state to check against observable remote truth.
        """
        if campaign_state is None or state_dir is None:
            return
        campaign_state.record_git_integration(git_rec.to_dict())
        campaign_state.save(state_dir)

    def _authorize_and_execute_git_step(
        self,
        action_type: str,
        arguments: Dict[str, Any],
        task_id: str,
        run_dir: Path,
        git_rec: "GitIntegrationRecord",
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates then executes a single real git/GitHub step via
        self.git_executor. Returns the step's metadata dict on success, or
        None (with git_rec.failure_reason set) on denial/failure -- never
        fabricates a success record for a step that did not actually
        complete.
        """
        action = ProposedAction(
            action_type=action_type,
            target_repo=self.repo_slug or self.target_repo.name,
            arguments=arguments,
        )
        verdict, decision_id, reason = self.git_executor.evaluate(action, self.target_repo, run_dir)
        if verdict != "ALLOW":
            git_rec.failure_reason = f"{action_type}: {reason or verdict}"
            return None
        if action_type in {"create_pull_request", "merge_pull_request"}:
            status, receipt, reconciliation_reason = self.git_executor.query_execution_status(
                decision_id or "", self.target_repo, run_dir, action, task_id
            )
            if status == "already_executed":
                if receipt is not None:
                    return receipt.native_receipt
                git_rec.failure_reason = reconciliation_reason
                return {}
        result = self.git_executor.execute(decision_id, self.target_repo, run_dir, action, task_id)
        if result.status != "success":
            git_rec.failure_reason = f"{action_type}: {result.error_message}"
            return None
        return result.metadata

    def _run_step_or_bail(
        self,
        action_type: str,
        arguments: Dict[str, Any],
        task_id: str,
        run_dir: Path,
        git_rec: "GitIntegrationRecord",
        campaign_state: Optional[DurableCampaignState],
        state_dir: Optional[Path],
        apply_metadata: Callable[[Dict[str, Any]], None],
    ) -> Optional[Dict[str, Any]]:
        """
        Runs one real git/GitHub step, applies its metadata to `git_rec` on
        success, and incrementally persists either way. Every step in the
        real integration sequence shares this exact bail-and-persist shape
        (#59 Phase 2/22); centralizing it here means the sequence in
        _execute_governed_engineering_improvement is a series of one-line
        calls rather than repeated boilerplate per step.
        """
        meta = self._authorize_and_execute_git_step(action_type, arguments, task_id, run_dir, git_rec)
        if meta is not None:
            apply_metadata(meta)
        self._persist_git_record(git_rec, campaign_state, state_dir)
        return meta

    def _persist_provider_states(
        self,
        campaign_state: Optional[DurableCampaignState],
        state_dir: Optional[Path],
    ) -> None:
        """
        Snapshots the pool's live provider statuses into durable campaign
        state immediately (#59.1 Blocker 5). Previously statuses were only
        written once per benchmark iteration, so a campaign could report a
        provider AVAILABLE after that provider had already returned a quota
        error.
        """
        if campaign_state is None:
            return
        campaign_state.provider_states = self.provider_pool.get_all_statuses()
        if state_dir is not None:
            campaign_state.save(state_dir)

    def _record_attempt(
        self,
        campaign_state: Optional[DurableCampaignState],
        state_dir: Optional[Path],
        task_id: str,
        provider: str,
        attempt_index: int,
        *,
        result: str,
        failure_class: Optional[str],
        exit_code: Optional[int],
        error_digest: str,
        delta_reconciled: bool,
        duration: float,
    ) -> None:
        """Appends and immediately persists one provider attempt (#59.1 Phase 4)."""
        if campaign_state is None:
            return
        attempt = EngineeringAttempt(
            task_id=task_id,
            provider=provider,
            attempt_index=attempt_index,
            result=result,
            failure_class=failure_class,
            exit_code=exit_code,
            error_digest=EngineeringAttempt.digest(error_digest),
            delta_reconciled=delta_reconciled,
            duration_seconds=round(duration, 3),
        )
        campaign_state.record_engineering_attempt(attempt.to_dict())
        if state_dir is not None:
            campaign_state.save(state_dir)

    def _reconcile_attempt_state(self, baseline: Any, task_id: str, attempt_index: int) -> bool:
        """
        Restores the repository to an attempt's baseline before handing it to
        the next provider (#59.1 Phase 3).

        A provider can exhaust its quota *after* it has already written files.
        Handing those partial, unreviewed changes to the next provider would
        make the fallback attempt's delta a mixture of two providers' work.
        Equally, discarding them wholesale would violate the #56 recovery
        invariants.

        So: capture the delta, checkpoint it as attempt evidence, then revert
        *only the paths that delta names*. Tracked paths are restored with
        `git checkout --`; paths the attempt newly added are removed. Any file
        the attempt did not touch -- including concurrent user work and
        unrelated dirty state -- is never inspected and never modified. There
        is deliberately no `git reset --hard` and no `git clean` anywhere on
        this path.

        Returns True when there was a task-owned delta that had to be reverted.
        """
        try:
            delta = capture_delta(self.target_repo, baseline)
        except Exception:  # noqa: BLE001 - reconciliation must never crash the campaign
            return False
        if delta is None or delta.is_empty:
            return False

        attempt_dir = self.target_repo / ".task_runs" / task_id / "attempts" / f"{attempt_index:02d}"
        try:
            attempt_dir.mkdir(parents=True, exist_ok=True)
            (attempt_dir / "delta.json").write_text(json.dumps(delta.to_dict(), indent=2), encoding="utf-8")
            (attempt_dir / "delta.patch").write_text(delta.diff_content or "", encoding="utf-8")
        except OSError:
            pass

        added = list(delta.files_added)
        restorable = list(delta.files_modified) + list(delta.files_deleted)
        for rel_path in restorable:
            self._git_runner(self.target_repo, ["checkout", "--", rel_path], 30)
        for rel_path in added:
            candidate = (self.target_repo / rel_path).resolve()
            # Never step outside the repository, whatever the delta claims.
            if not str(candidate).startswith(str(self.target_repo.resolve())):
                continue
            try:
                if candidate.is_file():
                    candidate.unlink()
            except OSError:
                pass
        return True

    @staticmethod
    def _attempt_result_for(event: Optional[Any], final_state: str) -> Tuple[str, str]:
        """
        Maps one attempt's observed outcome onto (attempt_result, failure_class).

        Only a ProviderExhaustionEvent -- produced by detect_exhaustion() from
        the provider's own execution result -- can yield a provider-availability
        class. Bad generated code, failing tests and rejected reviews all stay
        ENGINEERING_FAILURE and must never mark a provider exhausted (#59.1).
        """
        if event is not None:
            status = ProviderAvailabilityStatus.RATE_LIMITED if event.failure_type == "rate_limit" else (
                ProviderAvailabilityStatus.SESSION_EXHAUSTED if event.failure_type == "session_limit"
                else ProviderAvailabilityStatus.QUOTA_EXHAUSTED if event.failure_type == "quota_exhausted"
                else ProviderAvailabilityStatus.UNAVAILABLE
            )
            failure_class = (
                FAILURE_CLASS_PROVIDER_UNAVAILABLE
                if event.failure_type in ("unavailable", "authentication_required")
                else FAILURE_CLASS_PROVIDER_EXHAUSTED
            )
            return status.value, failure_class
        if final_state in ("awaiting_human", "blocked"):
            return "AUTHORITY_BLOCKED", FAILURE_CLASS_AUTHORITY_BLOCKED
        return "ENGINEERING_FAILURE", FAILURE_CLASS_ENGINEERING

    def execute_factory_work_item(
        self,
        work_item: Any,
        risk_level: str = "medium",
        task_class: str = "feature",
        files_changed: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Public adapter to run a single factory WorkItem through the governed engineering lifecycle.

        The factory supervisor selects portfolio-level work; this method hands
        the selected item to the same bounded lifecycle used by backlog and
        marathon tasks, instead of duplicating it.
        """
        objective = f"{work_item.title}\n\n{work_item.description}".strip()
        return self._execute_governed_engineering_improvement(
            task_id=work_item.work_item_id,
            benchmark_key=work_item.work_item_id,
            gap_type="factory_work_item",
            gap_desc=objective,
            objective_override=objective,
            risk_level=risk_level,
            task_class=task_class,
            files_changed=files_changed,
            repository_override=work_item.repository,
        )

    def _execute_governed_engineering_improvement(
        self,
        task_id: str,
        benchmark_key: str,
        gap_type: str,
        gap_desc: str,
        avoid_provider: Optional[str] = None,
        risk_level: str = "medium",
        campaign_state: Optional[DurableCampaignState] = None,
        state_dir: Optional[Path] = None,
        allowed_paths: Optional[List[str]] = None,
        reviewer_requirements: Optional[List[str]] = None,
        objective_override: Optional[str] = None,
        task_class: str = "bug_fix",
        commit_message_override: Optional[str] = None,
        files_changed: Optional[List[str]] = None,
        repository_override: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Executes a bounded engineering task through the REAL governed
        lifecycle (#59 Phase 2): task branch -> real provider implementation
        via GovernedTaskOrchestrator -> independent review -> deterministic
        verification -> real commit/push/PR/CI-observe/merge via
        GitIntegrationExecutor -> independent remote verification.

        No fabricated Git values are ever produced. Returns (True, git_rec)
        only after the full real lifecycle -- through remote-verified
        merge -- has completed. Returns (False, git_rec_with_integration_mode_parked)
        when a boundary requires human authority the campaign's envelope
        does not delegate; the caller records the park and continues with
        other independent work rather than treating it as a failure.

        `risk_level="low"` (used by bounded LOCAL_ONLY_CONTINUATION) makes the
        quota-free local model an eligible candidate for this specific fix;
        the default "medium" keeps normal in-campaign gap fixes on cloud
        providers, consistent with Tier-3 local eligibility rules (#58 Phase 9).

        `allowed_paths` (#59.2 Phase 8), when given, mechanically restricts
        the eventual commit to exactly those repo-relative paths --
        GitIntegrationExecutor.stage_and_commit rejects any other path before
        staging anything. Used by the live acceptance canary to guarantee it
        can touch only its designated evidence artifact.
        """
        objective = objective_override or f"Resolve {gap_type} for {benchmark_key}: {gap_desc}"
        # The repository is the one this engine was pointed at, not a constant.
        # It was hardcoded to "howlplane", which is correct for self-improvement
        # dogfooding and wrong for every other target -- including the HowlFrame
        # backlog marathon, whose whole purpose is to work another repository.
        repository = repository_override or self.target_repo.name
        target_identities = {
            self.target_repo.name,
            str(self.target_repo),
            self.repo_slug,
        }
        if repository_override and repository_override not in target_identities:
            return False, {
                "task_id": task_id,
                "target_repo": repository_override,
                "integration_mode": "parked",
                "failure_reason": "TARGET_REPOSITORY_MISMATCH",
                "failure_class": FAILURE_CLASS_AUTHORITY_BLOCKED,
                "failure_code": "target_repository_mismatch",
            }
        gap_probe = TaskSpec(
            task_id=task_id, repository=repository,
            objective=objective, task_class=task_class, risk_level=risk_level,
            allowed_paths=list(allowed_paths or []),
            reviewer_requirements=list(reviewer_requirements or []),
        )
        candidates = self.provider_pool.select_candidates(
            task_category="code_heavy",
            avoid_provider=avoid_provider,
            task=gap_probe,
        )
        if not candidates:
            return False, {
                "task_id": task_id,
                "target_repo": repository,
                "failure_reason": "NO_ELIGIBLE_PROVIDER",
                "failure_class": FAILURE_CLASS_PROVIDER_UNAVAILABLE,
                "failure_code": "no_eligible_provider",
            }

        provider = candidates[0]
        gap_probe.preferred_agent = provider
        commit_message = commit_message_override or (
            f"fix({benchmark_key}): resolve {gap_type} found during marathon dogfooding"
        )

        git_rec = GitIntegrationRecord(
            task_id=task_id, target_repo=repository, provider=provider,
            commit_message=commit_message,
        )

        planned_actions = [
            f"create branch fix/{task_id}", "commit task-owned files", "push branch",
            "open pull request", "merge when required checks green",
        ]

        boundary = HumanBoundaryGate.evaluate_with_delegated_authority(
            gap_probe, planned_actions, self.authority_envelope, self.target_repo,
            repo_slug=self.repo_slug, files_changed=files_changed,
        )
        if boundary.requires_human_approval:
            packet = boundary.decision_packet
            parked = ParkedTaskRecord(
                task_id=task_id,
                objective=objective,
                boundary_type=boundary.triggered_boundaries[0] if boundary.triggered_boundaries else "unknown",
                requested_action="governed_engineering_improvement",
                repository=self.repo_slug or repository,
                evidence=list(packet.evidence) if packet else [],
                risks=list(packet.risks) if packet else [],
                verification_state="not_reached",
                why_delegated_authority_did_not_apply="; ".join(boundary.triggered_boundaries),
                decision_packet=packet.to_dict() if packet else {},
            )
            git_rec.failure_reason = "AWAITING_HUMAN"
            return False, {
                "task_id": task_id, "target_repo": repository, "integration_mode": "parked",
                "provider": provider, "parked_record": parked.to_dict(),
            }

        if self.git_executor is None:
            git_rec.failure_reason = "NO_GIT_EXECUTOR_CONFIGURED"
            self._persist_git_record(git_rec, campaign_state, state_dir)
            return False, git_rec.to_dict()

        # --------------------------------------------------------------------
        # Bounded provider failover (#59.1 Phase 2).
        #
        # A single governed engineering task may be attempted by several
        # providers in turn. A provider that reports quota/session exhaustion
        # or unavailability is marked in the pool -- so it is not handed the
        # next task either -- and the next eligible provider is tried. A
        # provider that merely produced bad code, failed tests or a rejected
        # review is NOT marked exhausted and does NOT trigger failover: that is
        # a normal engineering failure and follows normal remediation policy.
        #
        # Attempts are bounded by the finite eligible provider set: `attempted`
        # only grows, and any provider already tried is filtered out of the
        # next selection, so the loop cannot spin.
        # --------------------------------------------------------------------
        orch_config = OrchestrationConfig(
            acquire_locks=True,
            record_evidence=bool(self.ledger),
            # Share this campaign's pool so governed reviews get bounded
            # alternate-provider failover and so review-time quota exhaustion
            # is visible to implementation failover (#59.2 Phases 4/9).
            provider_pool=self.provider_pool,
        )
        attempted: set = set()
        result = None
        attempt_index = 0
        previous_provider: Optional[str] = None

        while True:
            attempt_index += 1
            attempted.add(provider)
            gap_probe.preferred_agent = provider
            gap_probe.metadata["provider_attempt_index"] = attempt_index
            gap_probe.metadata["failover_from_resource_id"] = previous_provider
            git_rec.provider = provider
            attempt_started = time.time()
            attempt_baseline = None
            try:
                attempt_baseline = capture_baseline(self.target_repo)
            except Exception:  # noqa: BLE001 - baseline is best-effort attempt evidence
                attempt_baseline = None

            orchestrator = self._orchestrator_factory(orch_config)
            try:
                result = orchestrator.run(gap_probe, planned_actions)
            except Exception as exc:  # noqa: BLE001 - real provider/orchestrator failures must not crash the campaign
                git_rec.failure_reason = f"orchestrator_exception: {exc}"
                self._record_attempt(
                    campaign_state, state_dir, task_id, provider, attempt_index,
                    result="ENGINEERING_FAILURE", failure_class=FAILURE_CLASS_ENGINEERING,
                    exit_code=None, error_digest=str(exc), delta_reconciled=False,
                    duration=time.time() - attempt_started,
                )
                self._persist_git_record(git_rec, campaign_state, state_dir)
                return False, git_rec.to_dict()

            if campaign_state is not None and result.trajectory_id:
                campaign_state.record_trajectory(result.trajectory_id)
                campaign_state.save(state_dir)

            exec_res = result.provider_execution
            if exec_res is not None and campaign_state is not None:
                campaign_state.record_provider_invocation(provider, role="implementation")

            if result.final_state == "complete":
                self._record_attempt(
                    campaign_state, state_dir, task_id, provider, attempt_index,
                    result="COMPLETE", failure_class=None,
                    exit_code=exec_res.exit_code if exec_res else 0,
                    error_digest="", delta_reconciled=False,
                    duration=time.time() - attempt_started,
                )
                break

            # Ask the pool -- from the provider's own execution result, never
            # from summary text -- whether this was an availability failure.
            event = None
            if exec_res is not None:
                if result.failure_class in {
                    FAILURE_CLASS_PROVIDER_EXHAUSTED,
                    FAILURE_CLASS_PROVIDER_UNAVAILABLE,
                }:
                    state = self.provider_pool.get_resource_status(provider)
                    event = state.exhaustion_event if state is not None else None
                else:
                    event = self.provider_pool.detect_exhaustion(
                        provider, exec_res, task_id=task_id
                    )
            attempt_result, failure_class = self._attempt_result_for(event, result.final_state)

            reconciled = False
            if event is not None and attempt_baseline is not None:
                reconciled = self._reconcile_attempt_state(attempt_baseline, task_id, attempt_index)

            self._record_attempt(
                campaign_state, state_dir, task_id, provider, attempt_index,
                result=attempt_result, failure_class=failure_class,
                exit_code=exec_res.exit_code if exec_res else None,
                error_digest=(exec_res.stderr if exec_res else "") or (result.error_message or ""),
                delta_reconciled=reconciled,
                duration=time.time() - attempt_started,
            )
            # The pool's updated view of provider availability must be durable
            # before the next selection -- a campaign that crashes here must
            # not resume believing an exhausted provider is AVAILABLE (#59.1
            # Blocker 5).
            self._persist_provider_states(campaign_state, state_dir)

            if event is None:
                # Engineering failure or authority block: do not fail over.
                git_rec.failure_reason = f"orchestrator_final_state:{result.final_state}"
                self._persist_git_record(git_rec, campaign_state, state_dir)
                record = git_rec.to_dict()
                record["failure_class"] = result.failure_class or failure_class
                record["failure_code"] = "orchestrator_terminal_state"
                return False, record

            next_candidates = [
                c for c in self.provider_pool.select_candidates(
                    task_category="code_heavy", avoid_provider=avoid_provider, task=gap_probe,
                )
                if c not in attempted
            ]
            if not next_candidates:
                git_rec.failure_reason = (
                    f"NO_ELIGIBLE_PROVIDER_REMAINING: tried {', '.join(sorted(attempted))}"
                )
                self._persist_git_record(git_rec, campaign_state, state_dir)
                record = git_rec.to_dict()
                record["failure_class"] = FAILURE_CLASS_PROVIDER_UNAVAILABLE
                record["failure_code"] = "no_eligible_provider_remaining"
                return False, record
            previous_provider = provider
            provider = next_candidates[0]

        delta = result.final_delta
        if delta is None or delta.is_empty:
            git_rec.failure_reason = "EMPTY_DELTA: governed implementation produced no changes to integrate"
            self._persist_git_record(git_rec, campaign_state, state_dir)
            return False, git_rec.to_dict()

        run_dir = Path(result.run_dir) if result.run_dir else (self.target_repo / ".task_runs" / task_id)
        paths = list(delta.files_added) + list(delta.files_modified) + list(delta.files_deleted)

        # The pre-execution estimate is not an authority grant for unknown
        # output. Evaluate the observed final delta before any git operation.
        final_boundary = HumanBoundaryGate.evaluate_with_delegated_authority(
            gap_probe,
            planned_actions,
            self.authority_envelope,
            self.target_repo,
            repo_slug=self.repo_slug,
            files_changed=paths,
        )
        if final_boundary.requires_human_approval:
            packet = final_boundary.decision_packet
            git_rec.integration_mode = "parked"
            git_rec.failure_reason = "AWAITING_HUMAN_FINAL_DELTA"
            parked = ParkedTaskRecord(
                task_id=task_id,
                objective=objective,
                boundary_type=(
                    final_boundary.triggered_boundaries[0]
                    if final_boundary.triggered_boundaries
                    else "unknown"
                ),
                requested_action="integrate_observed_final_delta",
                repository=repository,
                evidence=list(packet.evidence) if packet else paths,
                risks=list(packet.risks) if packet else [],
                verification_state="complete",
                why_delegated_authority_did_not_apply="; ".join(
                    final_boundary.triggered_boundaries
                ),
                decision_packet=packet.to_dict() if packet else {},
            )
            record = git_rec.to_dict()
            record.update({
                "integration_mode": "parked",
                "parked_record": parked.to_dict(),
                "failure_class": FAILURE_CLASS_AUTHORITY_BLOCKED,
                "failure_code": "final_delta_authority_boundary",
            })
            self._persist_git_record(git_rec, campaign_state, state_dir)
            return False, record

        baseline_sha: Optional[str] = None
        baseline_file = run_dir / "baseline.json"
        if baseline_file.is_file():
            try:
                baseline_sha = json.loads(baseline_file.read_text(encoding="utf-8")).get("initial_commit_sha")
            except (json.JSONDecodeError, OSError):
                baseline_sha = None

        def _apply_branch(meta: Dict[str, Any]) -> None:
            git_rec.branch = meta.get("branch")
            git_rec.branch_observed = True

        def _apply_commit(meta: Dict[str, Any]) -> None:
            git_rec.commit_sha = meta.get("commit_sha")
            git_rec.commit_observed = True

        def _apply_push(_meta: Dict[str, Any]) -> None:
            git_rec.push_observed = True

        def _apply_pr(meta: Dict[str, Any]) -> None:
            git_rec.pr_number = meta.get("pr_number")
            git_rec.pr_url = meta.get("pr_url")
            git_rec.pr_observed = True

        if self._run_step_or_bail(
            "create_task_branch", {}, task_id, run_dir, git_rec, campaign_state, state_dir, _apply_branch,
        ) is None:
            return False, git_rec.to_dict()

        if self._run_step_or_bail(
            "commit_task_changes",
            {
                "paths": paths, "message": commit_message, "baseline_sha": baseline_sha,
                "allowed_paths": gap_probe.allowed_paths or None,
            },
            task_id, run_dir, git_rec, campaign_state, state_dir, _apply_commit,
        ) is None:
            return False, git_rec.to_dict()

        if self._run_step_or_bail(
            "push_task_branch", {"branch": git_rec.branch},
            task_id, run_dir, git_rec, campaign_state, state_dir, _apply_push,
        ) is None:
            return False, git_rec.to_dict()

        if self._run_step_or_bail(
            "create_pull_request",
            {
                "branch": git_rec.branch,
                "title": f"fix({benchmark_key}): {gap_type}",
                "body": f"Automated marathon dogfooding fix.\n\nGap: {gap_type}\n{gap_desc}",
            },
            task_id, run_dir, git_rec, campaign_state, state_dir, _apply_pr,
        ) is None:
            return False, git_rec.to_dict()

        # An unattended merge requires live CI policy AND terminal green
        # required checks. Every other outcome -- unreadable policy, a policy
        # enforcing no checks at all, still-pending jobs, a poll timeout --
        # fails closed (#59.1 Blockers 3/4).
        ci_obs = self.git_executor.observe_required_checks(git_rec.pr_number)
        policy = ci_obs.policy
        git_rec.required_checks = ci_obs.checks
        git_rec.required_checks_observed = ci_obs.all_required_observed
        git_rec.required_checks_green = ci_obs.all_required_green
        if policy is not None and not policy.available:
            git_rec.ci_status = "policy_unavailable"
        elif policy is not None and not policy.contexts:
            git_rec.ci_status = "no_required_checks"
        elif ci_obs.failed_jobs:
            git_rec.ci_status = "failed"
        elif ci_obs.timed_out:
            git_rec.ci_status = "timeout"
        elif not ci_obs.all_required_observed or not ci_obs.all_required_terminal:
            git_rec.ci_status = "pending"
        else:
            git_rec.ci_status = "passed" if ci_obs.all_required_green else "failed"
        self._persist_git_record(git_rec, campaign_state, state_dir)

        if not ci_obs.authorizes_merge():
            reason = (policy.reason if policy is not None and policy.reason else git_rec.ci_status)
            git_rec.failure_reason = f"CI_NOT_GREEN: {reason}"
            self._persist_git_record(git_rec, campaign_state, state_dir)
            return False, git_rec.to_dict()

        def _apply_merge(meta: Dict[str, Any]) -> None:
            git_rec.merge_sha = meta.get("merge_sha")
            git_rec.merge_observed = True

        if self._run_step_or_bail(
            "merge_pull_request", {"pr_number": git_rec.pr_number},
            task_id, run_dir, git_rec, campaign_state, state_dir, _apply_merge,
        ) is None:
            return False, git_rec.to_dict()
        if campaign_state is not None:
            campaign_state.increment_merge_count()
            # Persist immediately after the irreversible merge operation so a
            # crash before remote verification cannot reset the envelope budget.
            if state_dir is not None:
                campaign_state.save(state_dir)

        remote_ok = self.git_executor.verify_remote_main_contains(git_rec.merge_sha)
        if not remote_ok:
            git_rec.failure_reason = "MERGE_NOT_YET_REACHABLE_FROM_REMOTE_MAIN"
            self._persist_git_record(git_rec, campaign_state, state_dir)
            return False, git_rec.to_dict()
        git_rec.remote_main_contains_merge = True
        git_rec.merged = True
        git_rec.merged_at = datetime.now(timezone.utc).isoformat()
        self._persist_git_record(git_rec, campaign_state, state_dir)

        sync_meta = self._authorize_and_execute_git_step("sync_local_main", {}, task_id, run_dir, git_rec)
        git_rec.local_main_synced = bool(sync_meta and sync_meta.get("local_main_synced"))
        git_rec.integration_mode = "real"
        self._persist_git_record(git_rec, campaign_state, state_dir)

        if self.ledger:
            self.ledger.append_entry(EvidenceEntry(
                task_id=task_id,
                agent_id=provider,
                action="engineering_gap_resolved",
                command=f"gh pr merge {git_rec.pr_number} --squash",
                result="PASS" if git_rec.is_fully_integrated() else "PARTIAL",
                artifact=f"commit:{git_rec.commit_sha}",
                task_class="bug_fix",
                risk_level="low",
                reasoning_tier="tier_2",
                implementing_agent=provider,
                metadata={"gap_type": gap_type, "benchmark": benchmark_key, "git_record": git_rec.to_dict()},
            ))

        return git_rec.is_fully_integrated(), git_rec.to_dict()
