#!/usr/bin/env python3
"""
engine.py

Prompt-to-Product Synthesis Engine for HowlFrame applications.
Orchestrates the complete bounded synthesis loop:
Intent -> Structured ProductSpec -> Capability Negotiation -> Code/Artifact Synthesis ->
Deterministic Compilation & Checking -> Build -> Black-box Acceptance Tests ->
Independent Review -> Targeted Repair Loop -> Verified Product Bundle.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
)
from src.control_plane.atomic_io import atomic_write_json, atomic_write_text, atomic_write_yaml
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.reconciliation import ReviewFinding, ReconciliationResult, ReviewReconciler
from src.control_plane.review_runner import (
    build_reviewer_candidates,
    invoke_reviewer_with_failover,
    parse_and_validate_findings,
)
from src.control_plane.reviewers import get_reviewer_role
from src.control_plane.synthesis.acceptance_runner import ProductAcceptanceReport, ProductAcceptanceRunner
from src.control_plane.synthesis.capability_negotiator import (
    CapabilityNegotiator,
    FeasibilityStatus,
    FrameworkGap,
    NegotiationResult,
)
from src.control_plane.synthesis.product_spec import ProductSpec
from src.control_plane.synthesis.provider_pool import (
    LOCAL_PROVIDER_IDS,
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from src.control_plane.synthesis.spec_synthesizer import NaturalLanguageSynthesizer
from src.control_plane.task_spec import DataClassSerializationMixin, TaskSpec

SYNTHESIS_SCHEMA_VERSION = "howlplane.synthesis/v1"


@dataclass
class ProductBundle(DataClassSerializationMixin):
    """Encapsulates a verified runnable product artifact directory."""

    product_name: str
    directory: str
    entrypoint: str
    capabilities_required: List[str]
    created_at: str
    acceptance_passed: bool
    total_checks: int
    passed_checks: int
    manifest_path: str
    verification_summary_path: str
    schema: str = SYNTHESIS_SCHEMA_VERSION


@dataclass
class SynthesisResult(DataClassSerializationMixin):
    """Complete result packet from prompt-to-product synthesis."""

    product_name: str
    product_spec: ProductSpec
    success: bool
    status: str  # "VERIFIED_PRODUCT", "PRODUCT_BLOCKED", "REPAIR_BUDGET_EXHAUSTED", "PROVIDER_POOL_EXHAUSTED", "SYNTHESIS_FAILED"
    product_bundle: Optional[ProductBundle] = None
    negotiation: Optional[NegotiationResult] = None
    acceptance_report: Optional[ProductAcceptanceReport] = None
    reconciliation: Optional[ReconciliationResult] = None
    repair_cycles: int = 0
    duration_seconds: float = 0.0
    implementing_provider: Optional[str] = None
    reviewing_providers: List[str] = field(default_factory=list)
    reviewer_mapping: Dict[str, str] = field(default_factory=dict)
    # Per-role records of what actually ran. `reviewing_providers` above is the
    # role *mapping*; these are executions (#59.1 Phase 8).
    reviewer_invocations: List[Dict[str, Any]] = field(default_factory=list)
    diversity_achieved: bool = True
    # diversity_achieved (above) reflects the initial role->provider assignment;
    # completed_diversity (#59.2 Phase 5) reflects which providers actually
    # completed each role -- the signal that matters for verification, since
    # failover may substitute a different provider than originally assigned.
    completed_diversity: bool = True
    framework_gaps: List[FrameworkGap] = field(default_factory=list)
    error_message: Optional[str] = None
    schema: str = SYNTHESIS_SCHEMA_VERSION


class ProductSynthesizer:
    """
    Drives prompt-to-product synthesis, real AI provider execution, and bounded repair loops.
    """

    def __init__(
        self,
        provider_pool: Optional[ProviderPoolManager] = None,
        acceptance_runner: Optional[ProductAcceptanceRunner] = None,
        capability_negotiator: Optional[CapabilityNegotiator] = None,
        max_repair_cycles: int = 3,
        ledger: Optional[EvidenceLedger] = None,
        synthesis_mode: str = "auto",  # "auto", "real_ai", "deterministic_baseline"
        custom_backend: Optional[AgentBackend] = None,
    ):
        self.provider_pool = provider_pool or ProviderPoolManager()
        self.acceptance_runner = acceptance_runner or ProductAcceptanceRunner()
        self.capability_negotiator = capability_negotiator or CapabilityNegotiator()
        self.max_repair_cycles = max_repair_cycles
        self.ledger = ledger
        if custom_backend is not None:
            self.synthesis_mode = "real_ai"
        elif synthesis_mode != "auto":
            self.synthesis_mode = synthesis_mode
        else:
            self.synthesis_mode = os.environ.get("HOWLPLANE_SYNTHESIS_MODE", "auto")
        self.custom_backend = custom_backend

    def create_from_prompt(
        self,
        prompt: str,
        output_dir: Union[str, Path],
        avoid_provider: Optional[str] = None,
        preferred_agent: Optional[str] = None,
        port: int = 8088,
    ) -> SynthesisResult:
        """
        End-to-end entrypoint: natural language prompt -> runnable verified product bundle.
        """
        t0 = time.time()
        out_path = Path(output_dir).resolve()
        out_path.mkdir(parents=True, exist_ok=True)

        # 1. Natural Language -> Structured ProductSpec
        synthesizer = NaturalLanguageSynthesizer()
        spec = synthesizer.synthesize(prompt)
        spec.default_port = port

        # Save product specification
        spec_path = out_path / "product_spec.yaml"
        spec.save_to_file(spec_path)

        # 2. Capability Negotiation with HowlFrame
        neg_res = self.capability_negotiator.negotiate(spec)
        if not neg_res.is_feasible:
            elapsed = round(time.time() - t0, 3)
            return SynthesisResult(
                product_name=spec.name,
                product_spec=spec,
                success=False,
                status="PRODUCT_BLOCKED",
                negotiation=neg_res,
                framework_gaps=neg_res.framework_gaps,
                duration_seconds=elapsed,
                error_message=f"HowlFrame framework gaps block product: {neg_res.framework_gaps[0].required_behavior}",
            )

        # 3. Provider Selection with Fallback. Full product synthesis is a broad,
        # non-mechanical task and is never local-eligible (#58 Phase 9): gate via a
        # medium-risk probe task so `local_ollama` is excluded even when AVAILABLE,
        # keeping campaigns finite once cloud providers are exhausted.
        synthesis_task_probe = TaskSpec(
            task_id=f"SYNTH-{spec.name.upper()}",
            repository=spec.name,
            objective=f"Synthesize {spec.title} HowlFrame product",
            task_class="feature",
            risk_level="medium",
        )
        candidates = self.provider_pool.select_candidates(
            task_category="code_heavy",
            avoid_provider=avoid_provider,
            preferred_agent=preferred_agent,
            task=synthesis_task_probe,
        )
        if not candidates and not self.custom_backend:
            # Local Ollama has no subscription quota and is never eligible for full
            # product synthesis (see probe task above), so it must not keep this
            # perpetually "not exhausted" -- gate on cloud providers specifically.
            if self.provider_pool.is_all_cloud_exhausted() and self.synthesis_mode != "deterministic_baseline":
                return self._exhausted_result(spec, neg_res, t0, "All configured providers are currently exhausted or unavailable")
            candidates = ["deterministic_baseline"]

        if not candidates and self.custom_backend:
            candidates = ["fake_agent"]

        # 4. Attempt synthesis with provider fallback
        selected_provider: Optional[str] = None
        synthesis_prompt = self._build_synthesis_prompt(spec, out_path)

        if self.synthesis_mode == "deterministic_baseline" or candidates == ["deterministic_baseline"]:
            selected_provider = candidates[0] if candidates else "deterministic_baseline"
            self._synthesize_product_files(out_path, spec, repair_iteration=0)
        else:
            for candidate in candidates:
                backend = self.custom_backend or AgentBackendRegistry.get_backend(candidate)
                task_obj = TaskSpec(
                    task_id=f"SYNTH-{spec.name.upper()}",
                    repository=spec.name,
                    objective=f"Synthesize {spec.title} HowlFrame product",
                    task_class="feature",
                    risk_level="medium",
                    recommended_reasoning_tier="tier_2",
                    actual_agent=candidate,
                )

                # Invoke backend
                impl_res = backend.execute(
                    task=task_obj,
                    cwd=out_path,
                    role="implementation",
                    prompt_override=synthesis_prompt,
                    timeout_seconds=30,
                )

                # Check for provider exhaustion / rate limit
                exhaustion_event = self.provider_pool.detect_exhaustion(candidate, impl_res, task_id=task_obj.task_id)
                if exhaustion_event:
                    # Quota or availability failure -> advance to next provider
                    continue

                # Candidate succeeded or is non-exhausted
                selected_provider = candidate
                break

        if not selected_provider:
            return self._exhausted_result(spec, neg_res, t0, "All candidate providers failed due to exhaustion or unavailability")

        # Ensure required files are present; if backend did not write files directly (e.g. baseline mode / test fixture),
        # initialize files from deterministic generator
        if not (out_path / "app" / "backend.howl").exists() or not (out_path / "scripts" / "build.sh").exists():
            self._synthesize_product_files(out_path, spec, repair_iteration=0)

        # 5. Bounded Verification and Repair Loop
        repair_count = 0
        last_acceptance: Optional[ProductAcceptanceReport] = None
        last_reconciliation: Optional[ReconciliationResult] = None
        last_err: Optional[str] = None
        active_provider = selected_provider
        review_role_map: Dict[str, str] = {}
        review_invocations: List[Dict[str, Any]] = []
        diversity_achieved = True
        completed_diversity = True

        while repair_count <= self.max_repair_cycles:
            # Check compile via HowlFrame compiler
            compile_ok, compile_err = self._check_compiler(out_path, spec)
            if not compile_ok:
                repair_count += 1
                last_err = f"Compiler error: {compile_err}"
                if repair_count <= self.max_repair_cycles:
                    active_provider, fail_res = self._attempt_repair_or_exhaustion(
                        spec, "compilation", compile_err or "Build failed",
                        out_path, active_provider, avoid_provider, repair_count,
                        neg_res, last_acceptance, t0
                    )
                    if fail_res:
                        return fail_res
                continue

            # Run Black-Box Acceptance Verification
            accept_report = self.acceptance_runner.run_acceptance_suite(out_path, spec)
            last_acceptance = accept_report

            if not accept_report.all_passed:
                repair_count += 1
                failing_checks = [c for c in accept_report.checks if c.status != "passed"]
                fail_details = "; ".join(f"{c.name}: {c.error_message}" for c in failing_checks)
                last_err = f"Acceptance failure: {fail_details}"
                if repair_count <= self.max_repair_cycles:
                    active_provider, fail_res = self._attempt_repair_or_exhaustion(
                        spec, "acceptance_tests", fail_details,
                        out_path, active_provider, avoid_provider, repair_count,
                        neg_res, last_acceptance, t0
                    )
                    if fail_res:
                        return fail_res
                continue

            # Run independent multi-provider review
            reviewer_roles = ["test-falsifier", "security-reviewer", "architecture-reviewer"]
            review_findings, review_role_map, diversity_achieved, review_invocations, completed_diversity = (
                self._run_independent_reviews(
                    out_path, spec, reviewer_roles, implementing_provider=active_provider
                )
            )
            reconcile_res = ReviewReconciler.reconcile(review_findings)
            last_reconciliation = reconcile_res

            # Review completion is a verification gate (#59.2 Phase 3): absence
            # of findings is never evidence of a completed review. Every
            # required role must have at least one completed invocation before
            # VERIFIED_PRODUCT can be returned -- reconciliation findings alone
            # are not enough, since a reviewer that never ran has none to report.
            completed_roles = {inv["role"] for inv in review_invocations if inv.get("completed")}
            missing_roles = [r for r in reviewer_roles if r not in completed_roles]

            if self.synthesis_mode != "deterministic_baseline" and missing_roles:
                repair_count += 1
                last_err = f"Review incomplete: no completed invocation for role(s) {missing_roles}"
                if repair_count <= self.max_repair_cycles:
                    active_provider, fail_res = self._attempt_repair_or_exhaustion(
                        spec, "independent_review", last_err,
                        out_path, active_provider, avoid_provider, repair_count,
                        neg_res, last_acceptance, t0
                    )
                    if fail_res:
                        return fail_res
                continue

            if reconcile_res.unresolved_blockers > 0 and repair_count < self.max_repair_cycles:
                repair_count += 1
                blocker_findings = [f for f in reconcile_res.findings if f.severity in ("blocker", "high") and f.status in ("open", "confirmed", "likely")]
                first_title = blocker_findings[0].title if blocker_findings else "Unresolved blocker finding"
                last_err = f"Review blocker findings: {first_title}"
                active_provider, fail_res = self._attempt_repair_or_exhaustion(
                    spec, "independent_review", last_err,
                    out_path, active_provider, avoid_provider, repair_count,
                    neg_res, last_acceptance, t0
                )
                if fail_res:
                    return fail_res
                continue

            # Product successfully verified!
            bundle = self._package_product_bundle(out_path, spec, accept_report)
            elapsed = round(time.time() - t0, 3)

            if self.ledger:
                self.ledger.append_entry(EvidenceEntry(
                    task_id=f"SYNTH-{spec.name.upper()}",
                    agent_id=active_provider,
                    action="synthesis_verified",
                    command=f"howl create '{prompt[:60]}...'",
                    result="PASS",
                    artifact=str(out_path),
                    task_class="feature",
                    risk_level="medium",
                    reasoning_tier="tier_2",
                    implementing_agent=active_provider,
                    remediation_cycles=repair_count,
                    metadata={
                        "product_name": spec.name,
                        "passed_checks": accept_report.passed_count,
                        "reviewing_providers": list(review_role_map.values()),
                        "diversity_achieved": diversity_achieved,
                        "completed_diversity": completed_diversity,
                    },
                ))

            return SynthesisResult(
                product_name=spec.name,
                product_spec=spec,
                success=True,
                status="VERIFIED_PRODUCT",
                product_bundle=bundle,
                negotiation=neg_res,
                acceptance_report=accept_report,
                reconciliation=reconcile_res,
                repair_cycles=repair_count,
                duration_seconds=elapsed,
                implementing_provider=active_provider,
                **self._review_evidence_kwargs(review_role_map, review_invocations, diversity_achieved, completed_diversity),
            )

        # Exhausted repair budget
        elapsed = round(time.time() - t0, 3)
        return SynthesisResult(
            product_name=spec.name,
            product_spec=spec,
            success=False,
            status="REPAIR_BUDGET_EXHAUSTED",
            negotiation=neg_res,
            acceptance_report=last_acceptance,
            reconciliation=last_reconciliation,
            repair_cycles=repair_count,
            duration_seconds=elapsed,
            implementing_provider=active_provider,
            **self._review_evidence_kwargs(review_role_map, review_invocations, diversity_achieved, completed_diversity),
            error_message=f"Exhausted repair budget ({self.max_repair_cycles} cycles): {last_err}",
        )

    @staticmethod
    def _review_evidence_kwargs(
        review_role_map: Dict[str, str],
        review_invocations: List[Dict[str, Any]],
        diversity_achieved: bool,
        completed_diversity: bool,
    ) -> Dict[str, Any]:
        """Shared SynthesisResult review-evidence fields, common to both the
        VERIFIED_PRODUCT and REPAIR_BUDGET_EXHAUSTED terminal returns."""
        return {
            "reviewing_providers": list(review_role_map.values()),
            "reviewer_mapping": review_role_map,
            "reviewer_invocations": list(review_invocations),
            "diversity_achieved": diversity_achieved,
            "completed_diversity": completed_diversity,
        }

    def _exhausted_result(self, spec: ProductSpec, neg_res: NegotiationResult, t0: float, msg: str) -> SynthesisResult:
        elapsed = round(time.time() - t0, 3)
        return SynthesisResult(
            product_name=spec.name,
            product_spec=spec,
            success=False,
            status="PROVIDER_POOL_EXHAUSTED",
            negotiation=neg_res,
            duration_seconds=elapsed,
            error_message=msg,
        )

    def _attempt_repair_or_exhaustion(
        self,
        spec: ProductSpec,
        stage: str,
        details: str,
        out_path: Path,
        active_provider: str,
        avoid_provider: Optional[str],
        repair_count: int,
        neg_res: NegotiationResult,
        last_acceptance: Optional[ProductAcceptanceReport],
        t0: float,
    ) -> Tuple[Optional[str], Optional[SynthesisResult]]:
        active = self._execute_provider_repair(
            spec=spec,
            failure_stage=stage,
            error_details=details,
            out_path=out_path,
            current_provider=active_provider,
            avoid_provider=avoid_provider,
        )
        if not active:
            elapsed = round(time.time() - t0, 3)
            return None, SynthesisResult(
                product_name=spec.name,
                product_spec=spec,
                success=False,
                status="PROVIDER_POOL_EXHAUSTED",
                negotiation=neg_res,
                acceptance_report=last_acceptance,
                repair_cycles=repair_count,
                duration_seconds=elapsed,
                error_message=f"All providers exhausted during {stage} repair",
            )
        return active, None

    def _execute_provider_repair(
        self,
        spec: ProductSpec,
        failure_stage: str,
        error_details: str,
        out_path: Path,
        current_provider: str,
        avoid_provider: Optional[str] = None,
    ) -> Optional[str]:
        """
        Executes a targeted AI repair cycle using exact feedback diagnostics.
        Falls back to next available provider if current provider exhausts.
        """
        repair_prompt = self._build_repair_prompt(spec, failure_stage, error_details, out_path)
        if self.synthesis_mode == "deterministic_baseline" or current_provider == "deterministic_baseline":
            self._synthesize_product_files(out_path, spec, repair_iteration=1)
            return "deterministic_baseline"

        repair_task_probe = TaskSpec(
            task_id=f"REPAIR-{spec.name.upper()}",
            repository=spec.name,
            objective=f"Repair {spec.title} after {failure_stage} failure",
            task_class="bug_fix",
            risk_level="medium",
        )
        candidates = self.provider_pool.select_candidates(
            task_category="code_heavy",
            avoid_provider=avoid_provider,
            preferred_agent=current_provider,
            task=repair_task_probe,
        )

        for candidate in candidates:
            backend = self.custom_backend or AgentBackendRegistry.get_backend(candidate)
            task_obj = TaskSpec(
                task_id=f"REPAIR-{spec.name.upper()}",
                repository=spec.name,
                objective=f"Repair {spec.title} after {failure_stage} failure",
                task_class="bug_fix",
                risk_level="medium",
                recommended_reasoning_tier="tier_2",
                actual_agent=candidate,
            )

            res = backend.execute(
                task=task_obj,
                cwd=out_path,
                role="remediation",
                prompt_override=repair_prompt,
                timeout_seconds=90,
            )

            exhaustion = self.provider_pool.detect_exhaustion(candidate, res, task_id=task_obj.task_id)
            if exhaustion:
                continue

            return candidate

        return None

    def _resolve_entity_slugs(self, spec: ProductSpec) -> Tuple[str, str, str]:
        ent_name = list(spec.entities.keys())[0] if spec.entities else "Item"
        ent_slug = ent_name.lower()
        return ent_name, ent_slug, ent_slug + "s"

    def _build_synthesis_prompt(self, spec: ProductSpec, out_path: Path) -> str:
        ent_name, ent_slug, slug_plural = self._resolve_entity_slugs(spec)
        store_path = spec.persistence.storage_path
        port = spec.default_port

        return f"""You are an expert HowlFrame software engineer.
Synthesize a complete, verified, runnable HowlFrame product named '{spec.name}' in the current working directory.

Product Specification:
Title: {spec.title}
Description: {spec.description}
Entity: {ent_name} (fields: title, content, created_at)
Storage: {store_path} (Native persistent key-value storage)
Port: {port}
Capabilities: network, database, filesystem

Required File Structure to create in the current directory:
1. app/backend.howl:
   - Must contain (http_server {port} ...)
   - Routes:
     * "/health" -> JSON 200 {{"status": "ok", "service": "{spec.name}", "healthy": "true"}}
     * "/" -> HTML 200 serving static/index.html via (read_file "static/index.html")
     * "/static/app.js" -> JS 200 serving static/app.js via (read_file "static/app.js")
     * "/static/style.css" -> CSS 200 serving static/style.css via (read_file "static/style.css")
     * "/api/{slug_plural}" ->
       - OPTIONS: JSON 200 {{"status": "ok"}}
       - GET: (store_open kv "{store_path}") -> (store_get kv "1") -> JSON 200 {{"{slug_plural}": [items]}}
       - POST: (try_let (body (parse_json {ent_name}Input req.body)) (catch ... (res_json 400 {{"error": "invalid_json"}}))) -> check title required -> (store_put kv "1" item) -> JSON 201
       - DELETE: (store_open kv "{store_path}") -> (store_delete kv "1") -> JSON 200 {{"status": "deleted", "id": "1"}}
2. app/frontend.howl:
   - (web_app ...) definition.
3. static/index.html:
   - Clean HTML5 layout with form (input-title, input-content, btn-create), status banner, item list.
4. static/style.css:
   - Modern clean styling.
5. static/app.js:
   - Client JS making fetch requests to /api/{slug_plural}.
6. scripts/build.sh:
   - Deterministic build script compiling app/backend.howl -> build/backend.hfbc via howlframe -compile-bc.
7. scripts/run.sh:
   - Deterministic runner script starting howlframe -run-bc -allow-caps network,database,filesystem build/backend.hfbc.
8. scripts/test.sh:
   - Test execution script.
9. data/{ent_slug}.json:
   - Initial data file containing '{{}}'.

Generate all files cleanly and ensure scripts/build.sh passes.
"""

    def _build_repair_prompt(
        self,
        spec: ProductSpec,
        failure_stage: str,
        error_details: str,
        out_path: Path,
    ) -> str:
        ent_name = list(spec.entities.keys())[0] if spec.entities else "Item"
        slug_plural = ent_name.lower() + "s"
        return f"""You are repairing a HowlFrame product '{spec.name}'.
The previous build/verification attempt failed during {failure_stage}.

Exact Failure Diagnostics:
{error_details}

Current Files in Repository:
Inspect the files in app/, static/, scripts/, data/ and repair the implementation so that:
1. scripts/build.sh compiles cleanly without errors.
2. All endpoints (/health, /, /api/{slug_plural}) respond with correct status codes and JSON formats.
3. Persistence across restart works.
4. All acceptance tests and review constraints pass.

Make the necessary file edits now.
"""

    def _run_independent_reviews(
        self,
        out_path: Path,
        spec: ProductSpec,
        roles: List[str],
        implementing_provider: str = "codex",
    ) -> Tuple[List[ReviewFinding], Dict[str, str], bool, List[Dict[str, Any]], bool]:
        """
        Executes independent multi-provider adversarial reviews over synthesized artifacts.
        Distributes reviewer roles across distinct available providers, with bounded
        failover (#59.2 Phase 4) to one independent fallback candidate per role when
        the preferred assignment is unavailable, exhausted, times out, or produces
        invalid/malformed output.

        Also returns a per-role invocation record distinguishing assigned from
        invoked from completed (#59.1 Phase 8) -- the role mapping alone says
        nothing about whether a reviewer actually ran -- plus `completed_diversity`
        (#59.2 Phase 5), which reflects which providers actually completed each
        role rather than which providers were merely assigned.
        """
        findings: List[ReviewFinding] = []
        invocations: List[Dict[str, Any]] = []
        rev_mapping, diversity_achieved = self.provider_pool.select_reviewers(
            implementing_provider, roles, allow_same_provider=True
        )

        backend_file = out_path / "app" / "backend.howl"
        backend_txt = backend_file.read_text(encoding="utf-8") if backend_file.exists() else ""

        # Deterministic heuristic verification checks (always run)
        if "eval" in backend_txt or "system_exec" in backend_txt:
            findings.append(ReviewFinding(
                id="F-SEC-001",
                reviewer_role="security-reviewer",
                title="Unsafe execution construct detected in backend",
                severity="high",
                category="security",
                location="app/backend.howl:1",
                description="Avoid using unrestricted system exec or eval in backend handlers.",
            ))

        scripts_present = [str(p.name) for p in (out_path / "scripts").glob("*")] if (out_path / "scripts").is_dir() else []
        if "build.sh" not in scripts_present:
            findings.append(ReviewFinding(
                id="F-TEST-001",
                reviewer_role="test-falsifier",
                title="Missing deterministic build script",
                severity="high",
                category="test_gap",
                location="scripts/",
                description="Product bundle requires scripts/build.sh for automated verification.",
            ))

        # Multi-provider reviewer execution (only in real AI / custom backend mode)
        if self.synthesis_mode != "deterministic_baseline":
            for role_id in roles:
                preferred = rev_mapping.get(role_id, implementing_provider)
                role_obj = get_reviewer_role(role_id)
                if not role_obj:
                    continue

                brief = role_obj.render_brief(
                    task=TaskSpec(
                        task_id=f"SYNTH-{spec.name.upper()}",
                        repository=spec.name,
                        objective=f"Review synthesized product {spec.title}",
                        task_class="feature",
                        risk_level="medium",
                        recommended_reasoning_tier="tier_2",
                    ),
                    diff_content=backend_txt[:4000],
                )
                review_task = TaskSpec(
                    task_id=f"SYNTH-REV-{role_id}",
                    repository=spec.name,
                    objective=f"Perform {role_id} review for {spec.name}",
                    task_class="other",
                    risk_level="low",
                    recommended_reasoning_tier="tier_2",
                )

                candidates = build_reviewer_candidates(role_id, preferred, self.provider_pool, review_task)

                if self.custom_backend:
                    backend_lookup: Callable[[str], Optional[AgentBackend]] = lambda _cid: self.custom_backend
                else:
                    backend_lookup = AgentBackendRegistry.get_backend

                winner, rev_res, attempts = invoke_reviewer_with_failover(
                    role_id=role_id,
                    candidates=candidates,
                    task=review_task,
                    cwd=out_path,
                    prompt_override=brief,
                    backend_lookup=backend_lookup,
                    provider_pool=self.provider_pool,
                )

                # Assignment is not execution (#59.1 Phase 8): a reviewer can be
                # mapped to a role and never run. Record what actually happened.
                invocation: Dict[str, Any] = {
                    "role": role_id,
                    "provider": winner or preferred,
                    "assigned": True,
                    "invoked": bool(attempts),
                    "completed": bool(winner),
                    "status": "completed" if winner else (attempts[-1]["outcome"] if attempts else "not_invoked"),
                    "duration_seconds": attempts[-1]["duration_seconds"] if attempts else 0.0,
                    "attempts": attempts,
                }
                if winner and rev_res is not None:
                    invocation.update(self._local_review_telemetry(winner, rev_res))
                    if rev_res.stdout and rev_res.stdout.strip():
                        parsed_findings, _parse_err, _is_valid = parse_and_validate_findings(rev_res.stdout, role_id)
                        findings.extend(parsed_findings)
                invocations.append(invocation)

        completed_by_role = {inv["role"]: inv["provider"] for inv in invocations if inv.get("completed")}
        completed_diversity = bool(completed_by_role) and len(set(completed_by_role.values())) == len(completed_by_role)

        return findings, rev_mapping, diversity_achieved, invocations, completed_diversity

    @staticmethod
    def _local_review_telemetry(provider: str, rev_res: AgentExecutionResult) -> Dict[str, Any]:
        """
        Captures the Tier-3 local provider's own telemetry for a review that
        genuinely ran (#59.1 Phase 8) -- model, RAM before/after/after-unload
        as the local backend reports them. Observability only: nothing here
        widens what the local model is allowed to do.
        """
        if provider not in LOCAL_PROVIDER_IDS:
            return {}
        meta = rev_res.metadata or {}
        return {
            "model": meta.get("model"),
            "ram_before_gib": meta.get("ram_before_gib"),
            "ram_after_gib": meta.get("ram_after_gib"),
            "ram_after_unload_gib": meta.get("ram_after_unload_gib"),
        }

    def _synthesize_product_files(
        self,
        out_path: Path,
        spec: ProductSpec,
        repair_iteration: int = 0,
        last_error: Optional[str] = None,
    ) -> None:
        """
        Synthesizes idiomatic, runnable HowlFrame application artifacts (deterministic baseline).
        """
        app_dir = out_path / "app"
        static_dir = out_path / "static"
        scripts_dir = out_path / "scripts"
        data_dir = out_path / "data"
        build_dir = out_path / "build"

        for d in (app_dir, static_dir, scripts_dir, data_dir, build_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Resolve primary entity names
        ent_name, ent_slug, slug_plural = self._resolve_entity_slugs(spec)
        store_path = spec.persistence.storage_path
        port = spec.default_port

        # Initialize data store file
        atomic_write_text(data_dir / f"{ent_slug}.json", "{}")
        clean_store_rel = store_path.replace("file://", "") if store_path.startswith("file://") else store_path
        if clean_store_rel.startswith("data/"):
            atomic_write_text(out_path / clean_store_rel, "{}")

        # 1. app/backend.howl
        backend_content = self._render_backend_howl(spec, ent_name, ent_slug, slug_plural, store_path, port)
        atomic_write_text(app_dir / "backend.howl", backend_content)

        # 2. app/frontend.howl
        if "browser_ui" in spec.interfaces:
            frontend_content = self._render_frontend_howl(spec, ent_name, slug_plural)
            atomic_write_text(app_dir / "frontend.howl", frontend_content)

            # 3. static/index.html & static/style.css
            html_content = self._render_index_html(spec, ent_name, slug_plural)
            atomic_write_text(static_dir / "index.html", html_content)
            css_content = self._render_style_css(spec)
            atomic_write_text(static_dir / "style.css", css_content)

        # 4. scripts/build.sh
        build_script = f"""#!/usr/bin/env bash
set -euo pipefail

# Deterministic HowlFrame compilation script
mkdir -p build static data

# Locate howlframe compiler
if [ -n "${{HOWLFRAME_BIN:-}}" ] && [ -x "${{HOWLFRAME_BIN}}" ]; then
    COMPILER="${{HOWLFRAME_BIN}}"
elif command -v howlframe >/dev/null 2>&1; then
    COMPILER="$(command -v howlframe)"
else
    echo "ERROR: HowlFrame compiler executable not found." >&2
    echo "Please install howlframe or set the HOWLFRAME_BIN environment variable." >&2
    exit 127
fi

echo "==> Building HowlFrame backend bytecode..."
"$COMPILER" -compile-bc app/backend.howl -o build/backend.hfbc

if [ -f app/frontend.howl ]; then
    echo "==> Validating HowlFrame frontend..."
    "$COMPILER" -validate app/frontend.howl
fi

echo "✓ Build complete."
"""
        atomic_write_text(scripts_dir / "build.sh", build_script)
        os.chmod(scripts_dir / "build.sh", 0o755)  # nosec B103

        # 5. scripts/run.sh
        run_script = f"""#!/usr/bin/env bash
set -euo pipefail

PORT="${{1:-${{PORT:-{port}}}}}"

# Locate howlframe runtime
if [ -n "${{HOWLFRAME_BIN:-}}" ] && [ -x "${{HOWLFRAME_BIN}}" ]; then
    HOWLFRAME_EXEC="${{HOWLFRAME_BIN}}"
elif command -v howlframe >/dev/null 2>&1; then
    HOWLFRAME_EXEC="$(command -v howlframe)"
else
    echo "ERROR: HowlFrame runtime executable not found." >&2
    echo "Please install howlframe or set the HOWLFRAME_BIN environment variable." >&2
    exit 127
fi

mkdir -p data build static
if [ ! -f data/{ent_slug}.json ]; then
    echo '{{}}' > data/{ent_slug}.json
fi

if [ ! -f build/backend.hfbc ]; then
    bash scripts/build.sh
fi

echo "==> Starting {spec.title} on port ${{PORT}}..."
export PORT="${{PORT}}"
exec "$HOWLFRAME_EXEC" -run-bc -allow-caps network,database,filesystem build/backend.hfbc
"""
        atomic_write_text(scripts_dir / "run.sh", run_script)
        os.chmod(scripts_dir / "run.sh", 0o755)  # nosec B103

        # 6. scripts/test.sh
        test_script = f"""#!/usr/bin/env bash
set -euo pipefail

echo "==> Running {spec.title} acceptance tests..."
bash scripts/build.sh
echo "✓ All tests verified."
"""
        atomic_write_text(scripts_dir / "test.sh", test_script)
        os.chmod(scripts_dir / "test.sh", 0o755)  # nosec B103

        # 7. static/app.js (Compiled client script for interactive browser UI)
        if "browser_ui" in spec.interfaces:
            app_js_content = self._render_client_js(spec, ent_name, slug_plural)
            atomic_write_text(static_dir / "app.js", app_js_content)

    def _render_backend_howl(
        self,
        spec: ProductSpec,
        ent_name: str,
        ent_slug: str,
        slug_plural: str,
        store_path: str,
        port: int,
    ) -> str:
        return f""";; =============================================================================
;; {spec.title} Backend HTTP Server
;; Generated deterministically by HowlPlane Prompt-to-Product Synthesizer
;; =============================================================================

(http_server {port}

  ;; Health probe endpoint
  (route "/health" (lambda (req)
    (do
      (res_header "Access-Control-Allow-Origin" "*")
      (res_header "Content-Type" "application/json")
      (res_json 200 (dict ("status" "ok") ("service" "{spec.name}") ("healthy" "true")))
    )
  ))

  ;; Static assets serving
  (route "/" (lambda (req)
    (do
      (res_header "Content-Type" "text/html; charset=utf-8")
      (res 200 "text/html" (read_file "static/index.html"))
    )
  ))

  (route "/static/app.js" (lambda (req)
    (do
      (res_header "Content-Type" "application/javascript; charset=utf-8")
      (res 200 "application/javascript" (read_file "static/app.js"))
    )
  ))

  (route "/static/style.css" (lambda (req)
    (do
      (res_header "Content-Type" "text/css; charset=utf-8")
      (res 200 "text/css" (read_file "static/style.css"))
    )
  ))

  ;; Primary REST CRUD Endpoints for {slug_plural}
  (route "/api/{slug_plural}" (lambda (req)
    (do
      (res_header "Access-Control-Allow-Origin" "*")
      (res_header "Access-Control-Allow-Methods" "GET, POST, PUT, DELETE, OPTIONS")
      (res_header "Access-Control-Allow-Headers" "Content-Type")
      (let (m (req_method req))
        (if (= m "OPTIONS")
          (res_json 200 (dict ("status" "ok")))
          (if (= m "GET")
            (do
              (store_open kv "{store_path}")
              (let (rec (store_get kv "1"))
                (if (is_nil rec)
                  (res_json 200 (dict ("{slug_plural}" (list))))
                  (res_json 200 (dict ("{slug_plural}" (list rec))))
                )
              )
            )
            (if (= m "POST")
              (try_let (body (parse_json {ent_name}Input req.body))
                (catch parse_err
                  (res_json 400 (dict ("error" "invalid_json")))
                )
                (let (title (map_get body "title"))
                  (if (or (is_nil title) (= title ""))
                    (res_json 400 (dict ("error" "title_required")))
                    (let (content (map_get body "content"))
                      (let (note (dict ("id" "1") ("title" title) ("content" content) ("created_at" "2026-08-20T00:00:00Z")))
                        (do
                          (store_open kv "{store_path}")
                          (store_put kv "1" note)
                          (res_json 201 note)
                        )
                      )
                    )
                  )
                )
              )
              (if (= m "DELETE")
                (do
                  (store_open kv "{store_path}")
                  (store_delete kv "1")
                  (res_json 200 (dict ("status" "deleted") ("id" "1")))
                )
                (res 405 "text/plain" "Method Not Allowed")
              )
            )
          )
        )
      )
    )
  ))
)
"""

    def _render_frontend_howl(self, spec: ProductSpec, ent_name: str, slug_plural: str) -> str:
        return f""";; =============================================================================
;; {spec.title} Frontend Web Application
;; Compiled to client JavaScript by HowlFrame Transpiler
;; =============================================================================

(web_app
  (defun load_items ()
    (do
      (let (status_banner (dom_query "#status"))
        (set_text status_banner "Loading {slug_plural}...")
      )
      (try_let (resp (fetch "/api/{slug_plural}" "GET"))
        (catch err
          (let (banner (dom_query "#status"))
            (set_text banner "Failed to connect to backend server.")
          )
        )
        (let (container (dom_query "#list-container"))
          (set_attr container "class" "loaded")
        )
      )
    )
  )
)
"""

    def _render_index_html(self, spec: ProductSpec, ent_name: str, slug_plural: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{spec.title}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <div class="container">
    <header class="header">
      <h1>{spec.title}</h1>
      <p class="subtitle">{spec.description}</p>
      <div id="status" class="status-banner">Ready</div>
    </header>

    <main class="main-content">
      <section class="form-section card">
        <h2>Create {ent_name}</h2>
        <form id="create-form">
          <div class="form-group">
            <label for="input-title">Title *</label>
            <input type="text" id="input-title" placeholder="Enter title..." required>
          </div>
          <div class="form-group">
            <label for="input-content">Content</label>
            <textarea id="input-content" rows="3" placeholder="Enter description or content..."></textarea>
          </div>
          <button type="submit" class="btn btn-primary" id="btn-create">Save {ent_name}</button>
        </form>
      </section>

      <section class="list-section card">
        <div class="section-header">
          <h2>Your {slug_plural.title()}</h2>
          <button class="btn btn-secondary" id="btn-refresh">Refresh</button>
        </div>
        <div id="list-container" class="items-list">
          <div class="empty-state">No {slug_plural} yet. Create your first one above!</div>
        </div>
      </section>
    </main>

    <footer class="footer">
      <p>Created by <strong>HowlPlane</strong> Prompt-to-Product Synthesizer &bull; Powered by HowlFrame</p>
    </footer>
  </div>

  <script src="/static/app.js"></script>
</body>
</html>
"""

    def _render_style_css(self, spec: ProductSpec) -> str:
        return """/* Clean, modern styling for synthesized Howl application */
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --primary: #238636;
  --primary-hover: #2ea043;
  --text: #f0f6fc;
  --text-muted: #8b949e;
  --danger: #da3633;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-color: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 2rem 1rem;
}

.container { max-width: 800px; margin: 0 auto; }
.header { margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }
.header h1 { font-size: 1.75rem; font-weight: 600; margin-bottom: 0.25rem; }
.subtitle { color: var(--text-muted); font-size: 0.95rem; }
.status-banner { margin-top: 0.75rem; font-size: 0.85rem; color: #58a6ff; }

.main-content { display: grid; gap: 1.5rem; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 1.25rem; }
.card h2 { font-size: 1.15rem; margin-bottom: 1rem; }

.form-group { margin-bottom: 1rem; }
.form-group label { display: block; font-size: 0.85rem; font-weight: 500; margin-bottom: 0.35rem; color: var(--text-muted); }
input[type="text"], textarea {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  padding: 0.5rem 0.75rem;
  font-size: 0.95rem;
}
input:focus, textarea:focus { outline: 2px solid #58a6ff; border-color: transparent; }

.btn {
  padding: 0.5rem 1rem;
  border-radius: 6px;
  border: none;
  font-weight: 500;
  cursor: pointer;
  font-size: 0.9rem;
}
.btn-primary { background: var(--primary); color: #ffffff; }
.btn-primary:hover { background: var(--primary-hover); }
.btn-secondary { background: var(--border); color: var(--text); }
.btn-danger { background: var(--danger); color: #ffffff; font-size: 0.8rem; padding: 0.25rem 0.5rem; }

.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.items-list { display: grid; gap: 0.75rem; }
.item-card { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem 1rem; display: flex; justify-content: space-between; align-items: flex-start; }
.empty-state { color: var(--text-muted); font-style: italic; text-align: center; padding: 1.5rem 0; }
.footer { margin-top: 3rem; text-align: center; font-size: 0.8rem; color: var(--text-muted); }
"""

    def _render_client_js(self, spec: ProductSpec, ent_name: str, slug_plural: str) -> str:
        return f"""// Client logic for {spec.title}
document.addEventListener('DOMContentLoaded', () => {{
  const form = document.getElementById('create-form');
  const titleInput = document.getElementById('input-title');
  const contentInput = document.getElementById('input-content');
  const listContainer = document.getElementById('list-container');
  const refreshBtn = document.getElementById('btn-refresh');
  const statusBanner = document.getElementById('status');

  async function loadItems() {{
    statusBanner.textContent = 'Loading...';
    try {{
      const res = await fetch('/api/{slug_plural}');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const items = data.{slug_plural} || [];
      renderItems(items);
      statusBanner.textContent = `Ready (${{items.length}} items)`;
    }} catch (err) {{
      statusBanner.textContent = 'Error loading items: ' + err.message;
    }}
  }}

  function renderItems(items) {{
    if (!items || items.length === 0) {{
      listContainer.innerHTML = '<div class="empty-state">No {slug_plural} yet. Create your first one above!</div>';
      return;
    }}
    listContainer.innerHTML = items.map(item => `
      <div class="item-card" data-id="${{item.id}}">
        <div>
          <h3>${{escapeHtml(item.title || 'Untitled')}}</h3>
          <p>${{escapeHtml(item.content || '')}}</p>
        </div>
        <button class="btn btn-danger btn-delete" data-id="${{item.id}}">Delete</button>
      </div>
    `).join('');

    document.querySelectorAll('.btn-delete').forEach(btn => {{
      btn.addEventListener('click', async (e) => {{
        const id = e.target.getAttribute('data-id');
        await deleteItem(id);
      }});
    }});
  }}

  async function deleteItem(id) {{
    try {{
      const res = await fetch('/api/{slug_plural}', {{ method: 'DELETE' }});
      if (res.ok) await loadItems();
    }} catch (err) {{
      alert('Failed to delete item: ' + err.message);
    }}
  }}

  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!title) return;

    statusBanner.textContent = 'Saving...';
    try {{
      const res = await fetch('/api/{slug_plural}', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ title, content }})
      }});
      if (res.ok) {{
        titleInput.value = '';
        contentInput.value = '';
        await loadItems();
      }} else {{
        const errData = await res.json();
        alert('Validation error: ' + (errData.message || res.statusText));
      }}
    }} catch (err) {{
      alert('Network error: ' + err.message);
    }}
  }});

  if (refreshBtn) refreshBtn.addEventListener('click', loadItems);
  function escapeHtml(str) {{
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }}

  loadItems();
}});
"""

    def _check_compiler(self, out_path: Path, spec: ProductSpec) -> Tuple[bool, Optional[str]]:
        build_script = out_path / "scripts" / "build.sh"
        if not build_script.exists():
            return False, "scripts/build.sh not found"
        try:
            res = subprocess.run(
                ["bash", str(build_script)],
                cwd=str(out_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                return True, None
            return False, (res.stderr + "\n" + res.stdout).strip()
        except Exception as exc:
            return False, str(exc)

    def _package_product_bundle(
        self,
        out_path: Path,
        spec: ProductSpec,
        report: ProductAcceptanceReport,
    ) -> ProductBundle:
        manifest_data = {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "version": "1.0.0",
            "entrypoint": "build/backend.hfbc",
            "capabilities": spec.capabilities_required,
            "interfaces": spec.interfaces,
            "port": spec.default_port,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "acceptance": {
                "passed": report.passed_count,
                "total": report.total_count,
                "all_passed": report.all_passed,
            },
        }

        manifest_path = out_path / "manifest.json"
        atomic_write_json(manifest_path, manifest_data)

        summary_path = out_path / "verification_summary.json"
        atomic_write_json(summary_path, report.to_dict())

        # Write README.md in product bundle
        readme_content = f"""# {spec.title}

{spec.description}

## Quick Start

Run the application with HowlFrame runtime:

```bash
bash scripts/run.sh
```

Or execute via HowlPlane:

```bash
ai run .
```

Open [http://localhost:{spec.default_port}](http://localhost:{spec.default_port}) in your browser.

## Verification

Acceptance verification report: {report.passed_count}/{report.total_count} checks passed.
"""
        atomic_write_text(out_path / "README.md", readme_content)

        return ProductBundle(
            product_name=spec.name,
            directory=str(out_path),
            entrypoint=str(out_path / "build" / "backend.hfbc"),
            capabilities_required=spec.capabilities_required,
            created_at=manifest_data["created_at"],
            acceptance_passed=report.all_passed,
            total_checks=report.total_count,
            passed_checks=report.passed_count,
            manifest_path=str(manifest_path),
            verification_summary_path=str(summary_path),
        )
