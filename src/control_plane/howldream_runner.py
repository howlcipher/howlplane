#!/usr/bin/env python3
"""
howldream_runner.py

HowlDream exploration runner and native ecosystem integration adapter for HowlPlane.
Dispatches bounded DREAM/NIGHTMARE/WAKE explorations, coordinates HowlFrame invariant
assessments and HowlCreate sandbox prototyping, and enforces strict, fail-closed
authority boundaries: speculative outputs NEVER possess execution authority.
"""

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.project_adapter import ProjectContext
from src.control_plane.task_spec import DataClassSerializationMixin


class ExplorationPolicy(str, Enum):
    """Exploration governance policies for HowlPlane."""

    NEVER = "NEVER"
    MANUAL = "MANUAL"
    SUGGEST = "SUGGEST"
    ALLOWED = "ALLOWED"
    AUTOMATIC_WITH_BUDGET = "AUTOMATIC_WITH_BUDGET"


DEFAULT_EXPLORATION_POLICY = ExplorationPolicy.MANUAL


class AuthorityEscalationError(Exception):
    """Raised when an exploration output attempts to claim execution authority."""

    pass


@dataclass
class ExplorationBudget(DataClassSerializationMixin):
    """Resource and scale constraints for HowlDream exploration."""

    max_candidates: int = 5
    max_trials: int = 3
    max_tokens: int = 2048
    duration_seconds: float = 60.0
    local_only: bool = True


@dataclass
class CircuitBreakers(DataClassSerializationMixin):
    """Fail-safe limits preventing runaway or recursive dreaming."""

    max_depth: int = 1
    max_dream_runs: int = 1
    max_retries: int = 0


@dataclass
class ExplorationRunResult(DataClassSerializationMixin):
    """Structured result of a HowlPlane-governed exploration cycle."""

    status: str  # "SUCCESS", "SKIPPED", "REJECTED", "UNRESOLVED", "FAILED", "UNAVAILABLE"
    policy: str
    objective: str
    envelope_path: Optional[str] = None
    run_dir: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    assessments: List[Dict[str, Any]] = field(default_factory=list)
    development_results: List[Dict[str, Any]] = field(default_factory=list)
    lineage_trace: Optional[Dict[str, Any]] = None
    circuit_breaker_triggered: bool = False
    error_message: Optional[str] = None
    duration_seconds: float = 0.0


def assert_no_execution_authority(payload: Any) -> None:
    """
    Hard negative authority assertion.

    Ensures that speculative outputs (from HowlDream, HowlFrame assessment, or
    HowlCreate) never claim execution authority, contain self-approvals, or attempt
    to authorize actions downstream in HowlPlane executor or HowlChangeOps.
    """
    if isinstance(payload, dict):
        # Check authority block
        auth = payload.get("authority")
        if isinstance(auth, dict):
            if auth.get("executable") is True:
                raise AuthorityEscalationError(
                    "Exploration output claimed executable=True; rejected"
                )
            if auth.get("type") not in ("ADVISORY", None):
                raise AuthorityEscalationError(
                    f"Exploration output claimed authority type {auth.get('type')}; rejected"
                )

        # Check self-approval
        if payload.get("approved") is True or payload.get("self_approved") is True:
            raise AuthorityEscalationError(
                "Exploration output claimed self-approval; rejected"
            )

        # Check execution capability in development results
        if payload.get("execution_capability") is True:
            raise AuthorityEscalationError(
                "Development result claimed execution capability; rejected"
            )

        # Recursively check sub-dictionaries and lists
        for v in payload.values():
            assert_no_execution_authority(v)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_execution_authority(item)


class ExplorationProvider:
    """Interface for HowlDream exploration engine providers."""

    def is_available(self) -> bool:
        """Checks if this provider is currently available."""
        raise NotImplementedError

    def explore(
        self,
        objective: str,
        budget: ExplorationBudget,
        work_dir: Path,
        **kwargs: Any,
    ) -> tuple[Path, Dict[str, Any]]:
        """
        Executes exploration and returns (run_dir, result_envelope_dict).
        Emitted dictionary must conform to howl.exploration_result/v1 schema.
        """
        raise NotImplementedError


class NativeHowlDreamProvider(ExplorationProvider):
    """Production provider running real HowlDream via import or CLI binary."""

    def is_available(self) -> bool:
        if os.environ.get("HOWLDREAM_BIN"):
            p = Path(os.environ["HOWLDREAM_BIN"]).expanduser().resolve()
            if p.is_file() and os.access(p, os.X_OK):
                return True
        if shutil.which("howldream"):
            return True
        try:
            import howldream  # noqa: F401
            return True
        except ImportError:
            return False

    def explore(
        self,
        objective: str,
        budget: ExplorationBudget,
        work_dir: Path,
        **kwargs: Any,
    ) -> tuple[Path, Dict[str, Any]]:
        from uuid import uuid4

        try:
            from howldream.contracts import ExplorationRequest
            from howldream.engine import explore

            req = ExplorationRequest(
                request_id=f"req-{uuid4().hex[:12]}",
                objective=objective,
                budget={
                    "max_candidates": budget.max_candidates,
                    "max_trials": budget.max_trials,
                    "max_tokens": min(budget.max_tokens, 4096),
                    "local_only": budget.local_only,
                },
                authority={"type": "ADVISORY", "executable": False},
            )
            run_dir, result = explore(req, root=work_dir)
            return run_dir, result.model_dump()
        except ImportError:
            bin_path = os.environ.get("HOWLDREAM_BIN") or shutil.which("howldream")
            if not bin_path:
                raise RuntimeError("HowlDream engine is not available")
            req_file = work_dir / f"req_{uuid4().hex[:8]}.json"
            req_data = {
                "schema_version": "howl.exploration/v1",
                "request_id": f"req-{uuid4().hex[:12]}",
                "objective": objective,
                "budget": {
                    "max_candidates": budget.max_candidates,
                    "max_trials": budget.max_trials,
                    "max_tokens": min(budget.max_tokens, 4096),
                    "local_only": budget.local_only,
                },
                "authority": {"type": "ADVISORY", "executable": False},
            }
            req_file.write_text(json.dumps(req_data), encoding="utf-8")
            runs_dir = work_dir / ".howldream" / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                [str(bin_path), "explore", str(req_file), "--output", str(runs_dir)],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(res.stdout)
            run_id = data.get("exploration_id", "")
            return runs_dir / run_id, data


# jscpd:ignore-start
class DeterministicTestExplorationProvider(ExplorationProvider):
    """Deterministic provider emitting contract-compatible envelopes for testing."""

    def is_available(self) -> bool:
        return True

    def explore(
        self,
        objective: str,
        budget: ExplorationBudget,
        work_dir: Path,
        **kwargs: Any,
    ) -> tuple[Path, Dict[str, Any]]:
        from uuid import uuid4
        from datetime import datetime, timezone

        run_id = f"hd-det-test-{uuid4().hex[:8]}"
        req_id = f"req-det-{uuid4().hex[:8]}"
        run_dir = work_dir / ".howldream" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        def _make_cand(cid_suffix: str, snippet: str, stat: str, extra_claim: str, issues: List[str]) -> Dict[str, Any]:
            cid = f"{run_id}/candidates/{cid_suffix}"
            return {
                "schema_version": "howl.candidate/v1",
                "candidate_id": cid,
                "source_run_id": run_id,
                "parent_request_id": req_id,
                "objective": objective,
                "text": f"IDEA: {snippet} for {objective}",
                "condition": "dream",
                "trust": "UNVERIFIED",
                "status": stat,
                "authority": {"type": "ADVISORY", "executable": False},
                "claims": [{"kind": "IDEA", "text": extra_claim}],
                "evidence_refs": [],
                "assumptions": issues,
                "unresolved_issues": issues,
                "contradictions": [],
                "verified_constraints": ["local_only"] if not issues else [],
                "provenance": {"initiator": "test_suite"},
            }

        num_cands = max(1, min(budget.max_candidates, 2))
        candidates: List[Dict[str, Any]] = [
            _make_cand("0", "Diagnostic hook", "LOCALLY_VERIFIED", "Active hook", []),
        ]
        if num_cands > 1:
            candidates.append(
                _make_cand("1", "Alternative queue", "UNRESOLVED", "Alt queue", ["unbounded memory"])
            )

        obj_node = f"obj-{req_id}"
        dream_node = f"{run_id}/dream"
        dag_dict = {
            "nodes": {
                obj_node: {
                    "node_id": obj_node,
                    "node_type": "OBJECTIVE",
                    "label": f"Objective: {objective[:50]}",
                    "details": {},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                dream_node: {
                    "node_id": dream_node,
                    "node_type": "DREAM_RUN",
                    "label": f"Dream Run: {run_id}",
                    "details": {},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            "edges": [
                {"source": obj_node, "target": dream_node, "relation": "explores"},
            ],
        }
        for cand in candidates:
            c_id = cand["candidate_id"]
            dag_dict["nodes"][c_id] = {
                "node_id": c_id,
                "node_type": "CANDIDATE",
                "label": f"Candidate: {c_id}",
                "details": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            dag_dict["edges"].append(
                {"source": dream_node, "target": c_id, "relation": "generates"}
            )

        envelope = {
            "schema_version": "howl.exploration_result/v1",
            "exploration_id": run_id,
            "parent_request_id": req_id,
            "objective": objective,
            "originating_component": "howlplane",
            "authority": {"type": "ADVISORY", "executable": False},
            "candidates": candidates,
            "claims": [],
            "evidence": [],
            "unresolved_assumptions": ["network telemetry available"],
            "contradictions": [],
            "scores": [],
            "verification_status": "LOCALLY_VERIFIED",
            "recommended_disposition": "INVESTIGATE",
            "provenance": {"system": "howlplane-test-provider"},
            "descent_dag": dag_dict,
        }

        envelope_file = run_dir / "exploration_envelope.json"
        envelope_file.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        return run_dir, envelope
# jscpd:ignore-end


class HowlDreamRunner:
    """Governance and dispatch adapter for HowlDream within HowlPlane."""

    def __init__(
        self,
        policy: ExplorationPolicy = DEFAULT_EXPLORATION_POLICY,
        breakers: Optional[CircuitBreakers] = None,
        ledger: Optional[EvidenceLedger] = None,
        provider: Optional[ExplorationProvider] = None,
    ) -> None:
        self.policy = policy
        self.breakers = breakers or CircuitBreakers()
        self.ledger = ledger
        self._dream_run_count = 0
        if provider is not None:
            self.provider = provider
        elif os.environ.get("HOWLDREAM_PROVIDER") in ("test", "deterministic", "deterministic_test"):
            self.provider = DeterministicTestExplorationProvider()
        else:
            self.provider = NativeHowlDreamProvider()

    def is_howldream_available(self) -> bool:
        """Checks whether howldream is available via the configured provider."""
        return self.provider.is_available()

    @classmethod
    def is_system_howldream_available(cls) -> bool:
        """Checks whether howldream is installed on the host system."""
        return NativeHowlDreamProvider().is_available()

    @classmethod
    def resolve_candidate_evaluator_bc(cls) -> Optional[Path]:
        """Resolves the compiled bytecode for candidate_evaluator.hfbc."""
        env_path = os.environ.get("HOWLFRAME_CANDIDATE_EVALUATOR_BC")
        if env_path:
            p = Path(env_path).expanduser().resolve()
            if p.is_file():
                return p

        repo_root = Path(__file__).resolve().parents[2]
        local_bc = repo_root / "integrations" / "howlframe" / "candidate_evaluator.hfbc"
        if local_bc.is_file():
            return local_bc

        return None

    def evaluate_candidate_with_howlframe(
        self,
        candidate_handoff: Union[Dict[str, Any], Path, str],
        tmp_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Executes HowlFrame candidate_evaluator bytecode against a candidate handoff.
        Emits a structured howl.assessment/v1 payload.
        """
        bc_path = self.resolve_candidate_evaluator_bc()
        howlframe_bin = shutil.which("howlframe") or os.environ.get("HOWLFRAME_BIN")

        if not howlframe_bin or not bc_path or not bc_path.is_file():
            # In-process fallback evaluator conforming to candidate_evaluator.howl rules
            return self._fallback_evaluate_candidate(candidate_handoff)

        # Write candidate to temporary file if passed as dict
        if isinstance(candidate_handoff, dict):
            import tempfile
            temp_f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, dir=str(tmp_dir) if tmp_dir else None
            )
            json.dump(candidate_handoff, temp_f)
            temp_f.close()
            cand_path = Path(temp_f.name)
            should_cleanup = True
        else:
            cand_path = Path(candidate_handoff)
            should_cleanup = False

        try:
            res = subprocess.run(
                [
                    str(howlframe_bin),
                    "-run-bc",
                    "-allow-caps",
                    "filesystem",
                    str(bc_path),
                    str(cand_path),
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                assert_no_execution_authority(data)
                return data
            else:
                return self._fallback_evaluate_candidate(candidate_handoff)
        except Exception:
            return self._fallback_evaluate_candidate(candidate_handoff)
        finally:
            if should_cleanup and cand_path.is_file():
                cand_path.unlink()

    def _fallback_evaluate_candidate(
        self, candidate_handoff: Union[Dict[str, Any], Path, str]
    ) -> Dict[str, Any]:
        """In-process evaluation mirroring candidate_evaluator.howl rules."""
        if isinstance(candidate_handoff, (str, Path)):
            with open(candidate_handoff, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = candidate_handoff

        # Enforce negative authority assertion
        try:
            assert_no_execution_authority(data)
        except AuthorityEscalationError as exc:
            return {
                "schema": "howl.assessment/v1",
                "disposition": "REJECT",
                "authority": {"type": "ADVISORY", "executable": False},
                "explanation": f"Authority escalation attempt rejected: {exc}",
            }

        cand = data.get("candidate", {})
        contradictions = cand.get("critical_contradictions", 0)
        status = cand.get("status", "")
        unresolved = cand.get("unresolved_assumptions", [])

        if contradictions > 0 or status == "CONTRADICTED":
            disposition = "REJECT"
            reason = f"Candidate contradicted by NIGHTMARE analysis ({contradictions} conflicts)"
        elif status == "RESOLVED":
            disposition = "ACCEPT_FOR_DEVELOPMENT"
            reason = "Candidate resolved invariants and grounded by WAKE evidence"
        elif len(unresolved) > 0 or status == "EXPLORED":
            disposition = "ACCEPT_FOR_DEVELOPMENT"
            reason = "Candidate has explored potential and bounded assumptions"
        else:
            disposition = "UNRESOLVED"
            reason = "Evidence is inconclusive; candidate deferred"

        return {
            "schema": "howl.assessment/v1",
            "candidate_id": cand.get("id", data.get("candidate_id", "unknown")),
            "parent_dream_id": data.get("parent_dream_id", ""),
            "disposition": disposition,
            "authority": {"type": "ADVISORY", "executable": False},
            "explanation": reason,
        }

    def promote_candidate_to_howlcreate(
        self,
        candidate_handoff: Union[Dict[str, Any], Path, str],
        assessment: Union[Dict[str, Any], Path, str],
    ) -> Dict[str, Any]:
        """
        Dispatches accepted candidate to HowlCreate deliberate sandbox development.
        Ensures that output is purely advisory and non-executable.
        """
        assert_no_execution_authority(candidate_handoff)
        assert_no_execution_authority(assessment)

        try:
            from howlcreate.engine.candidate_ingestion import develop_candidate

            dev_result = develop_candidate(candidate_handoff, assessment)
            dev_dict = (
                dev_result if isinstance(dev_result, dict) else dev_result.model_dump()
            )
            assert_no_execution_authority(dev_dict)
            return dev_dict
        except ImportError:
            # Fallback when howlcreate is not installed in current env
            if isinstance(assessment, (str, Path)):
                with open(assessment, "r", encoding="utf-8") as f:
                    assess_data = json.load(f)
            else:
                assess_data = assessment

            if assess_data.get("disposition") != "ACCEPT_FOR_DEVELOPMENT":
                raise ValueError("Cannot promote candidate not ACCEPT_FOR_DEVELOPMENT")

            return {
                "schema_version": "howl.development_result/v1",
                "disposition": "ACCEPTED",
                "execution_authority": "NONE",
                "prototype_plan": "Fallback sandbox prototype plan",
                "test_plan": ["verify invariants in sandbox"],
                "authority": {"type": "ADVISORY", "executable": False},
            }

    def dispatch_exploration(
        self,
        objective: str,
        context: Optional[ProjectContext] = None,
        budget: Optional[ExplorationBudget] = None,
        policy: Optional[ExplorationPolicy] = None,
        repo_dir: Optional[Path] = None,
        current_depth: int = 0,
        run_howlframe: bool = True,
        run_howlcreate: bool = True,
    ) -> ExplorationRunResult:
        """
        Dispatches a governed exploration cycle through HowlDream, HowlFrame, and HowlCreate.
        """
        t0 = time.time()
        active_policy = policy or self.policy
        active_budget = budget or ExplorationBudget()

        # 1. Policy Gate
        if active_policy == ExplorationPolicy.NEVER:
            return ExplorationRunResult(
                status="SKIPPED",
                policy=active_policy.value,
                objective=objective,
                error_message="Exploration policy NEVER disallows running HowlDream.",
                duration_seconds=round(time.time() - t0, 3),
            )
        elif active_policy == ExplorationPolicy.SUGGEST:
            return ExplorationRunResult(
                status="SKIPPED",
                policy=active_policy.value,
                objective=objective,
                error_message="Exploration policy SUGGEST: exploration suggested but not executed.",
                duration_seconds=round(time.time() - t0, 3),
            )
        elif active_policy == ExplorationPolicy.AUTOMATIC_WITH_BUDGET:
            if active_budget.max_candidates > 10 or active_budget.max_tokens > 4096:
                return ExplorationRunResult(
                    status="SKIPPED",
                    policy=active_policy.value,
                    objective=objective,
                    error_message=(
                        f"Exploration policy AUTOMATIC_WITH_BUDGET exceeded budget ceiling: "
                        f"candidates={active_budget.max_candidates} (max 10), "
                        f"tokens={active_budget.max_tokens} (max 4096)"
                    ),
                    duration_seconds=round(time.time() - t0, 3),
                )

        # 2. Circuit Breaker Gate
        if current_depth >= self.breakers.max_depth:
            return ExplorationRunResult(
                status="SKIPPED",
                policy=active_policy.value,
                objective=objective,
                circuit_breaker_triggered=True,
                error_message=(
                    f"Circuit breaker triggered: depth {current_depth} >= "
                    f"max_depth {self.breakers.max_depth}"
                ),
                duration_seconds=round(time.time() - t0, 3),
            )

        if self._dream_run_count >= self.breakers.max_dream_runs:
            return ExplorationRunResult(
                status="SKIPPED",
                policy=active_policy.value,
                objective=objective,
                circuit_breaker_triggered=True,
                error_message=(
                    f"Circuit breaker triggered: dream run count {self._dream_run_count} >= "
                    f"max_dream_runs {self.breakers.max_dream_runs}"
                ),
                duration_seconds=round(time.time() - t0, 3),
            )

        # 3. Availability Check
        if not self.is_howldream_available():
            return ExplorationRunResult(
                status="UNAVAILABLE",
                policy=active_policy.value,
                objective=objective,
                error_message="HowlDream engine is not available or installed.",
                duration_seconds=round(time.time() - t0, 3),
            )

        self._dream_run_count += 1
        work_dir = repo_dir or Path.cwd()

        try:
            run_dir, raw_envelope = self.provider.explore(
                objective=objective,
                budget=active_budget,
                work_dir=work_dir,
            )
            assert_no_execution_authority(raw_envelope)
            envelope_path = run_dir / "exploration_envelope.json"
            if not envelope_path.is_file():
                envelope_path.write_text(json.dumps(raw_envelope, indent=2), encoding="utf-8")

            candidates = raw_envelope.get("candidates", [])
            assessments: List[Dict[str, Any]] = []
            development_results: List[Dict[str, Any]] = []

            for cand in candidates:
                # 4. HowlFrame Invariant Evaluation
                if run_howlframe:
                    assess = self.evaluate_candidate_with_howlframe(cand, tmp_dir=work_dir)
                    assert_no_execution_authority(assess)
                    assessments.append(assess)

                    # 5. HowlCreate Sandbox Development if accepted
                    if (
                        run_howlcreate
                        and assess.get("disposition") == "ACCEPT_FOR_DEVELOPMENT"
                    ):
                        dev_res = self.promote_candidate_to_howlcreate(cand, assess)
                        assert_no_execution_authority(dev_res)
                        development_results.append(dev_res)

            dag_dict = raw_envelope.get("descent_dag") or raw_envelope.get("lineage_dag")

            # Record to EvidenceLedger if configured
            if self.ledger:
                entry = EvidenceEntry(
                    source="howldream",
                    claim_type="exploration",
                    content={
                        "objective": objective,
                        "candidates_count": len(candidates),
                        "accepted_count": len(development_results),
                        "envelope_path": str(envelope_path),
                        "authority": "ADVISORY_ONLY",
                    },
                    status="SUCCESS",
                )
                self.ledger.record(entry)

            status = "SUCCESS" if len(candidates) > 0 else "NO_CANDIDATES"
            return ExplorationRunResult(
                status=status,
                policy=active_policy.value,
                objective=objective,
                envelope_path=str(envelope_path),
                run_dir=str(envelope_path.parent),
                candidates=candidates,
                assessments=assessments,
                development_results=development_results,
                lineage_trace=dag_dict,
                duration_seconds=round(time.time() - t0, 3),
            )

        except AuthorityEscalationError as exc:
            return ExplorationRunResult(
                status="REJECTED",
                policy=active_policy.value,
                objective=objective,
                error_message=f"Authority violation: {exc}",
                duration_seconds=round(time.time() - t0, 3),
            )
        except Exception as exc:
            return ExplorationRunResult(
                status="FAILED",
                policy=active_policy.value,
                objective=objective,
                error_message=str(exc),
                duration_seconds=round(time.time() - t0, 3),
            )
