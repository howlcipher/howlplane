#!/usr/bin/env python3
"""
howldream_runner.py

HowlDream exploration runner and native ecosystem integration adapter for HowlPlane.
Dispatches bounded DREAM/NIGHTMARE/WAKE explorations, coordinates HowlFrame invariant
assessments and HowlCreate sandbox prototyping, and enforces strict, fail-closed
authority boundaries: speculative outputs NEVER possess execution authority.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger, sanitize_value
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


class HowlDreamRunner:
    """Governance and dispatch adapter for HowlDream within HowlPlane."""

    def __init__(
        self,
        policy: ExplorationPolicy = DEFAULT_EXPLORATION_POLICY,
        breakers: Optional[CircuitBreakers] = None,
        ledger: Optional[EvidenceLedger] = None,
    ) -> None:
        self.policy = policy
        self.breakers = breakers or CircuitBreakers()
        self.ledger = ledger
        self._dream_run_count = 0

    @classmethod
    def is_howldream_available(cls) -> bool:
        """Checks whether howldream is importable or installed as a CLI binary."""
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

        # Check dev sibling locations
        dev_worktree_bc = Path(
            "/run/media/system/tallgeese/dev/worktrees/howlframe-milestone-four"
            "/apps/candidate_evaluator/candidate_evaluator.hfbc"
        )
        if dev_worktree_bc.is_file():
            return dev_worktree_bc

        dev_bc = Path(
            "/run/media/system/tallgeese/dev/howlframe"
            "/apps/candidate_evaluator/candidate_evaluator.hfbc"
        )
        if dev_bc.is_file():
            return dev_bc

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
                # If execution failed or had non-zero exit code, return REJECT assessment
                return {
                    "schema": "howl.assessment/v1",
                    "disposition": "REJECT",
                    "authority": {"type": "ADVISORY", "executable": False},
                    "explanation": f"HowlFrame evaluation error: {res.stderr.strip()}",
                }
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
                "schema": "howl.development_result/v1",
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
            from uuid import uuid4
            from howldream.contracts import ExplorationRequest
            from howldream.engine import explore

            req = ExplorationRequest(
                request_id=f"req-{uuid4().hex[:12]}",
                objective=objective,
                budget={
                    "max_candidates": active_budget.max_candidates,
                    "max_trials": active_budget.max_trials,
                    "max_tokens": min(active_budget.max_tokens, 4096),
                    "local_only": active_budget.local_only,
                },
                authority={"type": "ADVISORY", "executable": False},
            )

            run_dir, result = explore(req, root=work_dir)
            envelope_path = run_dir / "exploration_envelope.json"
            assert_no_execution_authority(result.model_dump())

            candidates = [c.model_dump() for c in result.candidates]
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

            dag_obj = getattr(result, "descent_dag", getattr(result, "lineage_dag", None))
            dag_dict = dag_obj.model_dump() if dag_obj else None

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
