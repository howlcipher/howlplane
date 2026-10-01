#!/usr/bin/env python3
"""The semantic recommendation seam used by the provider pool."""

from typing import Callable, Dict, List, Protocol, Sequence

from howlplane.control_plane.agent_registry import AgentProfile
from howlplane.control_plane.semantic.models import (
    RecommendationContext,
    SemanticRecommendation,
    SemanticStatus,
)
from howlplane.control_plane.task_spec import TaskSpec


class SemanticRecommender(Protocol):
    """Ranks already-eligible resources. Must never add or remove candidates."""

    def recommend(
        self,
        *,
        task: TaskSpec,
        candidates: Sequence[AgentProfile],
        role: str,
        context: RecommendationContext,
    ) -> SemanticRecommendation:
        ...


class DisabledSemanticRecommender:
    """Default recommender: never judges, never calls anything."""

    def recommend(
        self,
        *,
        task: TaskSpec,
        candidates: Sequence[AgentProfile],
        role: str,
        context: RecommendationContext,
    ) -> SemanticRecommendation:
        return SemanticRecommendation(
            status=SemanticStatus.DISABLED, reason="semantic recommendation is off"
        )


class MockSemanticRecommender:
    """Scripted recommender for tests. Records every call it receives."""

    def __init__(
        self,
        responder: Callable[[Sequence[str]], SemanticRecommendation],
    ):
        self._responder = responder
        self.calls: List[Dict[str, object]] = []

    def recommend(
        self,
        *,
        task: TaskSpec,
        candidates: Sequence[AgentProfile],
        role: str,
        context: RecommendationContext,
    ) -> SemanticRecommendation:
        ids = [profile.resource_id or profile.agent_id for profile in candidates]
        self.calls.append({"task_id": task.task_id, "role": role, "candidate_ids": ids})
        return self._responder(ids)


def candidate_ids(candidates: Sequence[AgentProfile]) -> List[str]:
    """Stable, sorted resource ids: the only options a judge may see."""
    return sorted({profile.resource_id or profile.agent_id for profile in candidates})


__all__ = [
    "DisabledSemanticRecommender",
    "MockSemanticRecommender",
    "SemanticRecommender",
    "candidate_ids",
]
