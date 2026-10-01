#!/usr/bin/env python3
"""Replays routing cases through the provider pool and records observations.

Collection only. This never changes production ranking and never learns from
what it records. Outcome fields stay null until joined from a trajectory, so a
report cannot claim an outcome nobody measured. A lexical mock provider proves
plumbing, not routing quality.

Usage:
  python scripts/evaluate_semantic_routing.py --output observations.jsonl \
      --command /path/to/howlinstinct --min-margin 0.35 [--trajectories DIR]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from howlplane.control_plane.agent_registry import AgentRegistry  # noqa: E402
from howlplane.control_plane.config_loader import (  # noqa: E402
    ProviderPolicySettings,
    ProviderResourceSettings,
    SemanticRecommendationSettings,
)
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager  # noqa: E402
from howlplane.control_plane.task_spec import TaskSpec  # noqa: E402

CASES_PATH = REPO_ROOT / "evals" / "resource_routing" / "cases.json"
OBSERVATION_SCHEMA = "howlplane.routing_observation/v1"
OUTCOME_FIELDS = (
    "resource_used",
    "execution_outcome",
    "verification_result",
    "review_result",
    "remediation_count",
    "timeouts_or_failover",
)


def load_cases(path: Path = CASES_PATH) -> List[Dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "howlplane.routing_eval_cases/v1":
        raise ValueError(f"{path} is not a routing case file")
    return list(document["cases"])


def build_pool(candidates: Iterable[str], semantic: SemanticRecommendationSettings) -> ProviderPoolManager:
    """A read-only pool over exactly the named resources, probing nothing."""
    pool = ProviderPoolManager(
        registry=AgentRegistry(),
        resources={rid: ProviderResourceSettings(enabled=True) for rid in candidates},
        policy=ProviderPolicySettings(),
        operating_mode="connected",
        read_only=True,
        probe_on_start=False,
    )
    pool.configure_semantic(semantic)
    return pool


def trajectory_outcomes(directory: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Reads what trajectories actually recorded, by task id. Missing means null."""
    found: Dict[str, Dict[str, Any]] = {}
    if directory is None:
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if task_id:
            found[task_id] = {
                "resource_used": data.get("final_implementation_resource")
                or data.get("selected_agent"),
                "timeouts_or_failover": data.get("failover_summary"),
            }
    return found


def observe(
    cases: Iterable[Dict[str, Any]],
    semantic: SemanticRecommendationSettings,
    outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    outcomes = outcomes or {}
    records = []
    for case in cases:
        task = TaskSpec(
            task_id=case["id"],
            repository="eval-fixture",
            objective=case["objective"],
            task_class=case["task_class"],
            risk_level=case["risk_level"],
            recommended_reasoning_tier=case["reasoning_tier"],
            required_skills=list(case.get("required_skills", [])),
        )
        pool = build_pool(case["candidates"], semantic)
        decision = pool.select_resource(task, role="implementation")
        block = decision.semantic_recommendation or {}
        recorded = outcomes.get(case["id"], {})
        record = {
            "schema": OBSERVATION_SCHEMA,
            "case_id": case["id"],
            "task_class": case["task_class"],
            "candidate_set": [item.resource_id for item in decision.eligible_resources],
            "deterministic_recommendation": (
                decision.cognitive_recommendation.resource_id
                if decision.cognitive_recommendation else None
            ),
            "semantic_status": block.get("status"),
            "semantic_recommendation": block.get("selected_resource_id"),
            "instinct_margin": block.get("instinct_margin"),
            "provider_confidence": block.get("provider_confidence"),
            "receipt_digest": block.get("receipt_digest"),
            "final_selection": decision.selected.resource_id if decision.selected else None,
            "disagreement": block.get("disagreement"),
            "label": case.get("label"),
        }
        for name in OUTCOME_FIELDS:
            record[name] = recorded.get(name)
        records.append(record)
    return records


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--command", default="howlinstinct")
    parser.add_argument("--min-margin", required=True, type=float,
                        help="Plane minimum_instinct_margin used for acceptance")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--trajectories", type=Path, default=None)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    args = parser.parse_args(argv)
    semantic = SemanticRecommendationSettings(
        mode="shadow",
        command=args.command,
        minimum_instinct_margin=args.min_margin,
        timeout_seconds=args.timeout,
        allow_in_local_only=True,
    )
    records = observe(load_cases(args.cases), semantic, trajectory_outcomes(args.trajectories))
    args.output.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    print(f"wrote {len(records)} observations to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
