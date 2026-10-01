#!/usr/bin/env python3
"""HowlInstinct recommender: invokes the installed ``howlinstinct`` CLI.

Plane consumes the HowlInstinct decision contract, never a provider wire
protocol. Everything that comes back is untrusted until it has been parsed
strictly, checked against the vendored receipt schema, and matched to the exact
candidate set Plane offered.
"""

import hashlib
import json
import math
import re
import shutil
import subprocess  # nosec B404
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from jsonschema import Draft202012Validator

from howlplane.control_plane.agent_registry import AgentProfile
from howlplane.control_plane.evidence_ledger import redact_sensitive_data
from howlplane.control_plane.semantic.models import (
    SEMANTIC_QUESTION_ID,
    RecommendationContext,
    SemanticMode,
    SemanticRecommendation,
    SemanticStatus,
)
from howlplane.control_plane.semantic.recommender import candidate_ids
from howlplane.control_plane.task_spec import TaskSpec

DECIDE_OUTPUT_SCHEMA = "howlinstinct.decide_output/v1"
RECEIPT_SCHEMA_ID = "howlinstinct.decision_receipt/v1"
_RECEIPT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts"
    / "howlinstinct"
    / "howlinstinct.decision_receipt.v1.schema.json"
)
_OBJECTIVE_LIMIT = 1000
_DESCRIPTION_LIMIT = 240
_REASON_LIMIT = 200
_DISTRIBUTION_TOLERANCE = 1e-4
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_INSTRUCTIONS = (
    "Which already-eligible resource is best suited to perform this task? "
    "Choose by the demands of the task, not by its urgency."
)


class _Rejected(Exception):
    """Internal: carries the status for an untrusted or failed response."""

    def __init__(self, status: SemanticStatus, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@lru_cache(maxsize=1)
def _receipt_validator() -> Draft202012Validator:
    with open(_RECEIPT_SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return Draft202012Validator(json.load(handle))


def _truncate(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _describe(profile: AgentProfile, capacity: Dict[str, str]) -> str:
    rid = profile.resource_id or profile.agent_id
    parts = [
        f"reasoning_tier={profile.reasoning_tier}",
        f"economic_class={profile.economic_class or 'UNKNOWN'}",
        f"locality={profile.locality or 'unknown'}",
        f"capacity={capacity.get(rid, 'UNKNOWN')}",
        "capabilities=" + ",".join(sorted(profile.capabilities)),
    ]
    return _truncate("; ".join(parts), _DESCRIPTION_LIMIT)


def build_request(
    task: TaskSpec,
    candidates: Sequence[AgentProfile],
    role: str,
    context: RecommendationContext,
) -> Dict[str, Any]:
    """Builds the bounded, redacted `decide` request.

    Only a short redacted objective and routing facts are sent. No repository
    content, no credentials, and no resource Plane has not already approved.
    """
    by_id = {(p.resource_id or p.agent_id): p for p in candidates}
    state = {
        "task": {
            "objective": _truncate(redact_sensitive_data(task.objective), _OBJECTIVE_LIMIT),
            "task_class": task.task_class,
            "risk_level": task.risk_level,
            "reasoning_tier": task.recommended_reasoning_tier,
            "required_skills": sorted(task.required_skills or []),
            "role": role,
        }
    }
    request: Dict[str, Any] = {
        "state": json.dumps(state, sort_keys=True),
        "questions": {
            SEMANTIC_QUESTION_ID: {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "options": [
                    {"name": rid, "description": _describe(by_id[rid], context.capacity)}
                    for rid in candidate_ids(candidates)
                ],
            }
        },
    }
    if _ID_PATTERN.match(task.task_id or ""):
        request["correlation_id"] = task.task_id
    return request


def _number(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, f"{name} is not a number")
    if not math.isfinite(value) or not 0.0 <= float(value) <= 1.0:
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, f"{name} is outside [0, 1]")
    return float(value)


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token}")


def _validated_choice(judgment: Dict[str, Any], offered: Sequence[str]) -> str:
    choice = judgment.get("choice")
    if not isinstance(choice, str) or choice not in set(offered):
        raise _Rejected(
            SemanticStatus.UNKNOWN_RESOURCE,
            "the judgment named a resource Plane did not offer",
        )
    return choice


def _validated_distribution(
    judgment: Dict[str, Any], offered: Sequence[str]
) -> Optional[Dict[str, float]]:
    raw = judgment.get("probabilities")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "probabilities is not an object")
    if not set(raw) <= set(offered):
        raise _Rejected(
            SemanticStatus.UNKNOWN_RESOURCE,
            "the distribution named a resource Plane did not offer",
        )
    distribution = {
        key: _number(raw[key], f"probability {key}") or 0.0 for key in sorted(raw)
    }
    if abs(sum(distribution.values()) - 1.0) > _DISTRIBUTION_TOLERANCE:
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "distribution does not sum to 1")
    return distribution


def parse_decide_output(
    stdout: str,
    *,
    offered: Sequence[str],
) -> SemanticRecommendation:
    """Strictly parses `decide --json` output into a JUDGED recommendation.

    Raises ``_Rejected`` with the precise integrity status on any defect.
    """
    try:
        document = json.loads(stdout, parse_constant=_reject_constant)
    except ValueError as exc:
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, f"stdout is not JSON: {exc}")
    if not isinstance(document, dict) or document.get("schema") != DECIDE_OUTPUT_SCHEMA:
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "unexpected output schema")
    judgments = document.get("judgments")
    if not isinstance(judgments, dict) or set(judgments) != {SEMANTIC_QUESTION_ID}:
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "unexpected judgment set")
    judgment = judgments[SEMANTIC_QUESTION_ID]
    if not isinstance(judgment, dict) or judgment.get("type") != "choice":
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "judgment is not a choice")
    outcome = judgment.get("outcome")
    if outcome in ("UNAVAILABLE", "ERROR"):
        raise _Rejected(SemanticStatus.UNAVAILABLE, f"provider outcome {outcome}")
    if outcome not in ("ACCEPTABLE_CONFIDENCE", "LOW_CONFIDENCE"):
        raise _Rejected(SemanticStatus.MALFORMED_RESPONSE, "unknown judgment outcome")

    choice = _validated_choice(judgment, offered)
    distribution = _validated_distribution(judgment, offered)
    provider_confidence = _number(judgment.get("provider_confidence"), "provider_confidence")
    instinct_margin = _number(judgment.get("instinct_margin"), "instinct_margin")

    receipt = _matching_receipt(document, judgment)
    return SemanticRecommendation(
        status=SemanticStatus.JUDGED,
        attempted=True,
        selected_resource_id=choice,
        distribution=distribution,
        provider_confidence=provider_confidence,
        instinct_margin=instinct_margin,
        receipt=receipt,
        receipt_digest=_digest(receipt),
        provider=_text(document.get("provider")),
        model=_text(document.get("model")),
        candidate_ids=list(offered),
    )


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _digest(receipt: Dict[str, Any]) -> str:
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matching_receipt(document: Dict[str, Any], judgment: Dict[str, Any]) -> Dict[str, Any]:
    receipts = document.get("receipts")
    if not isinstance(receipts, list):
        raise _Rejected(SemanticStatus.INVALID_RECEIPT, "no receipts were returned")
    matches = [
        r for r in receipts
        if isinstance(r, dict) and r.get("question_id") == SEMANTIC_QUESTION_ID
    ]
    if len(matches) != 1:
        raise _Rejected(SemanticStatus.INVALID_RECEIPT, "expected exactly one receipt")
    receipt = dict(matches[0])
    try:
        validator = _receipt_validator()
    except (OSError, ValueError) as exc:
        # Without the contract Plane cannot verify the receipt, so it does not trust it.
        raise _Rejected(
            SemanticStatus.CONFIG_ERROR, f"vendored receipt schema unavailable: {exc}"
        )
    errors = sorted(validator.iter_errors(receipt), key=lambda e: list(e.path))
    if errors:
        raise _Rejected(
            SemanticStatus.INVALID_RECEIPT,
            "receipt failed schema validation: " + _truncate(errors[0].message, _REASON_LIMIT),
        )
    if receipt.get("schema") != RECEIPT_SCHEMA_ID or receipt.get("decision_type") != "choice":
        raise _Rejected(SemanticStatus.INVALID_RECEIPT, "receipt identity mismatch")
    for key in ("choice", "provider_confidence", "instinct_margin"):
        if receipt.get(key) != judgment.get(key):
            raise _Rejected(SemanticStatus.INVALID_RECEIPT, f"receipt disagrees on {key}")
    receipt.pop("state", None)  # raw state is never retained in Plane evidence
    return receipt


_EXIT_STATUS: Dict[int, SemanticStatus] = {
    2: SemanticStatus.CONFIG_ERROR,
    3: SemanticStatus.CONFIG_ERROR,
    4: SemanticStatus.UNAVAILABLE,
    5: SemanticStatus.MALFORMED_RESPONSE,
    6: SemanticStatus.TIMEOUT,
}


class HowlInstinctSemanticRecommender:
    """Asks the installed HowlInstinct CLI one bounded `choice` question."""

    def __init__(self, *, command: str = "howlinstinct", mode: str = SemanticMode.SHADOW.value):
        self.command = command
        self.mode = mode

    def recommend(
        self,
        *,
        task: TaskSpec,
        candidates: Sequence[AgentProfile],
        role: str,
        context: RecommendationContext,
    ) -> SemanticRecommendation:
        offered = candidate_ids(candidates)

        def failed(status: SemanticStatus, reason: str) -> SemanticRecommendation:
            return SemanticRecommendation(
                status=status,
                mode=self.mode,
                reason=_truncate(redact_sensitive_data(reason), _REASON_LIMIT),
                attempted=True,
                candidate_ids=offered,
            )

        executable = shutil.which(self.command)
        if executable is None:
            return failed(SemanticStatus.UNAVAILABLE, "howlinstinct executable not found")
        payload = json.dumps(build_request(task, candidates, role, context))
        try:
            # Fixed interpreter path and argv list: no shell, no task text in argv.
            completed = subprocess.run(  # nosec B603
                [executable, "decide", "--input", "-", "--json"],
                input=payload,
                capture_output=True,
                text=True,
                timeout=context.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return failed(SemanticStatus.TIMEOUT, "howlinstinct timed out")
        except OSError as exc:
            return failed(SemanticStatus.UNAVAILABLE, f"howlinstinct could not start: {exc}")
        if completed.returncode != 0:
            status = _EXIT_STATUS.get(completed.returncode, SemanticStatus.UNAVAILABLE)
            detail = (completed.stderr or "").strip().splitlines()
            return failed(
                status,
                f"howlinstinct exited {completed.returncode}"
                + (f": {detail[-1]}" if detail else ""),
            )
        try:
            judged = parse_decide_output(completed.stdout, offered=offered)
        except _Rejected as exc:
            return failed(exc.status, exc.reason)
        return replace(judged, mode=self.mode)
