#!/usr/bin/env python3
"""
experiment_evaluator.py

Deterministic evaluation of ReasoningExperiment results.

The proposing model does not decide whether its experiment succeeded. This
module applies explicit, reproducible comparison logic to baseline and candidate
trajectory summaries. Small samples produce WEAKLY_SUPPORTED, INCONCLUSIVE, or
NOT_YET_MEASURABLE rather than false certainty.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.reasoning.reasoning_experiment import ReasoningExperiment, VALID_EXPERIMENT_OUTCOMES

# Minimum observations before an experiment can be called SUPPORTED.
MIN_SAMPLES_FOR_SUPPORTED = 3
# Minimum observations before a directional signal can be WEAKLY_SUPPORTED.
MIN_SAMPLES_FOR_WEAKLY_SUPPORTED = 1

# Metrics where higher values are better for the candidate.
_HIGHER_IS_BETTER = {
    "verification_pass_rate",
    "first_pass_success_rate",
    "success_rate",
    "reproducibility_rate",
}

# Metrics where lower values are better for the candidate.
_LOWER_IS_BETTER = {
    "repair_cycles",
    "mean_repair_cycles",
    "provider_failures",
    "latency_if_available",
    "mean_latency",
    "cost_if_available",
    "mean_cost",
    "review_escape_rate",
    "confirmed_defects",
    "escaped_defects",
}


class EvaluationError(ValueError):
    """Raised when deterministic evaluation cannot proceed due to bad inputs."""
    pass


class CriterionParseError(EvaluationError):
    """Raised when a falsification criterion cannot be parsed."""
    pass


def _to_numeric(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    return None


def _compute_rate(summaries: List[Dict[str, Any]], predicate) -> Optional[float]:
    if not summaries:
        return None
    return round(sum(1 for s in summaries if predicate(s)) / len(summaries), 2)


def _quality_summaries(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Exclude provider availability events from engineering quality metrics."""
    return [
        summary for summary in summaries
        if summary.get("outcome") not in ("provider_exhausted", "provider_unavailable")
    ]


def _compute_mean(summaries: List[Dict[str, Any]], key: str) -> Optional[float]:
    values = [_to_numeric(s.get(key)) for s in summaries]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _get_aggregate(summaries: List[Dict[str, Any]], metric: str) -> Any:
    """Extract an aggregate or per-summary value for a metric."""
    if not summaries:
        return None

    if metric == "verification_pass_rate":
        return _compute_rate(
            _quality_summaries(summaries),
            lambda s: s.get("verification_status") == "passed",
        )

    if metric == "first_pass_success_rate":
        return _compute_rate(
            _quality_summaries(summaries),
            lambda s: s.get("verification_status") == "passed" and s.get("repair_cycles_count", 0) == 0,
        )

    if metric == "success_rate":
        return _compute_rate(
            _quality_summaries(summaries),
            lambda s: s.get("outcome") == "success",
        )

    if metric == "reproducibility_rate":
        return _compute_rate(summaries, lambda s: s.get("reproducible") is True)

    if metric == "mean_repair_cycles":
        return _compute_mean(summaries, "repair_cycles_count")

    if metric == "repair_cycles":
        return _compute_mean(summaries, "repair_cycles_count")

    if metric == "provider_failures":
        return sum(
            1
            for s in summaries
            if s.get("outcome") in ("provider_exhausted", "provider_unavailable")
            or str(s.get("final_status", "")).startswith("provider_")
        )

    if metric == "mean_latency":
        return _compute_mean(summaries, "latency_if_available")

    if metric == "mean_cost":
        return _compute_mean(summaries, "cost_if_available")

    if metric == "latency_if_available":
        return _compute_mean(summaries, "latency_if_available")

    if metric == "cost_if_available":
        return _compute_mean(summaries, "cost_if_available")

    if metric == "review_escape_rate":
        # Not directly observable from a single trajectory; reported if provided.
        return _compute_mean(summaries, "review_escape_rate")

    if metric in ("confirmed_defects", "escaped_defects"):
        return sum(_to_numeric(s.get(metric)) or 0 for s in summaries)

    # Direct per-summary field: fall back to first value for scalar comparison.
    values = [s.get(metric) for s in summaries]
    non_none = [v for v in values if v is not None]
    if not non_none:
        return None
    numeric = [_to_numeric(v) for v in non_none]
    if all(v is not None for v in numeric):
        return round(sum(numeric) / len(numeric), 3)
    return non_none[0]


_CRITERION_RE = re.compile(
    r"^(?P<target>baseline|candidate)\.(?P<metric>[a-zA-Z_][a-zA-Z0-9_]*)\s*"
    r"(?P<op>==|!=|<=|>=|<|>)\s*"
    r"(?P<rhs>.+)$"
)


def _parse_literal(raw: str) -> Any:
    raw = raw.strip()
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.lower() == "none":
        return None
    numeric = _to_numeric(raw)
    if numeric is not None:
        return numeric
    return raw


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    return False


def evaluate_falsification_criterion(
    criterion: str,
    baseline: List[Dict[str, Any]],
    candidate: List[Dict[str, Any]],
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates a single falsification criterion against baseline/candidate summaries.

    A falsification criterion is a condition that, if TRUE, would falsify the
    candidate strategy. Returns (passed, reason). `passed` is True when the
    falsification condition is FALSE (i.e. the experiment is not falsified by
    this criterion).
    """
    match = _CRITERION_RE.match(criterion.strip())
    if not match:
        raise CriterionParseError(f"Cannot parse falsification criterion: {criterion}")

    target = match.group("target")
    metric = match.group("metric")
    op = match.group("op")
    rhs_raw = match.group("rhs").strip()

    summaries = candidate if target == "candidate" else baseline
    left = _get_aggregate(summaries, metric)

    if rhs_raw.startswith("baseline."):
        right_metric = rhs_raw.split(".", 1)[1]
        right = _get_aggregate(baseline, right_metric)
    elif rhs_raw.startswith("candidate."):
        right_metric = rhs_raw.split(".", 1)[1]
        right = _get_aggregate(candidate, right_metric)
    else:
        right = _parse_literal(rhs_raw)

    condition_holds = _compare(op, left, right)
    if condition_holds:
        return False, f"Falsification criterion triggered: {criterion} (got {left} {op} {right})"
    return True, None


def evaluate_experiment(
    experiment: ReasoningExperiment,
    baseline_summaries: Optional[List[Dict[str, Any]]] = None,
    candidate_summaries: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Deterministically evaluates a reasoning experiment.

    Returns one of the VALID_EXPERIMENT_OUTCOMES plus a detail dict.
    """
    baseline = baseline_summaries if baseline_summaries is not None else experiment.baseline_results
    candidate = candidate_summaries if candidate_summaries is not None else experiment.candidate_results

    if not baseline or not candidate:
        return "NOT_YET_MEASURABLE", {"reason": "Missing baseline or candidate results."}

    if not experiment.verify_prediction_digest():
        raise EvaluationError("Prediction digest mismatch: experiment definition changed after execution started.")

    details: Dict[str, Any] = {
        "baseline_n": len(baseline),
        "candidate_n": len(candidate),
        "falsification_failures": [],
        "metric_comparisons": {},
        "primary_metric": None,
    }

    # 1. Evaluate explicit falsification criteria first.
    for criterion in experiment.falsification_criteria:
        try:
            passed, reason = evaluate_falsification_criterion(criterion, baseline, candidate)
        except CriterionParseError as exc:
            details["falsification_failures"].append(str(exc))
            continue
        if not passed:
            details["falsification_failures"].append(reason or criterion)

    if details["falsification_failures"]:
        return "FALSIFIED", details

    # 2. Compare configured metrics.
    metrics = experiment.metrics or ["verification_pass_rate", "mean_repair_cycles"]
    primary_metric = metrics[0]
    details["primary_metric"] = primary_metric

    comparisons: Dict[str, str] = {}
    for metric in metrics:
        b = _get_aggregate(baseline, metric)
        c = _get_aggregate(candidate, metric)
        if b is None or c is None:
            comparisons[metric] = "incomparable"
            continue
        if metric in _HIGHER_IS_BETTER:
            if c > b:
                comparisons[metric] = "better"
            elif c == b:
                comparisons[metric] = "equal"
            else:
                comparisons[metric] = "worse"
        elif metric in _LOWER_IS_BETTER:
            if c < b:
                comparisons[metric] = "better"
            elif c == b:
                comparisons[metric] = "equal"
            else:
                comparisons[metric] = "worse"
        else:
            comparisons[metric] = "equal" if c == b else "incomparable"
    details["metric_comparisons"] = comparisons

    # 3. Decide outcome.
    primary = comparisons.get(primary_metric, "incomparable")
    if primary == "worse":
        return "FALSIFIED", details

    non_primary = [comparisons[m] for m in metrics if m != primary_metric]
    any_worse = any(v == "worse" for v in non_primary)
    all_better_or_equal = all(v in ("better", "equal") for v in non_primary)

    candidate_n = len(candidate)
    baseline_n = len(baseline)

    if primary == "better" and all_better_or_equal and candidate_n >= MIN_SAMPLES_FOR_SUPPORTED and baseline_n >= MIN_SAMPLES_FOR_SUPPORTED:
        return "SUPPORTED", details

    if primary == "better" and not any_worse and candidate_n >= MIN_SAMPLES_FOR_WEAKLY_SUPPORTED:
        return "WEAKLY_SUPPORTED", details

    if primary == "better" and any_worse:
        return "INCONCLUSIVE", details

    if primary == "equal":
        return "INCONCLUSIVE", details

    # Directional signal on primary but insufficient sample.
    if primary == "better" and candidate_n < MIN_SAMPLES_FOR_SUPPORTED:
        return "NOT_YET_MEASURABLE", details

    return "INCONCLUSIVE", details


def finalize_experiment_outcome(experiment: ReasoningExperiment) -> None:
    """Runs deterministic evaluation and writes the result into the experiment."""
    outcome, details = evaluate_experiment(experiment)
    if outcome not in VALID_EXPERIMENT_OUTCOMES:
        outcome = "INCONCLUSIVE"
    confidence = (
        f"baseline_n={details.get('baseline_n')}, candidate_n={details.get('candidate_n')}, "
        f"primary_metric={details.get('primary_metric')}, comparisons={details.get('metric_comparisons')}"
    )
    experiment.finalize(outcome, confidence=confidence, evaluation_details=details)
