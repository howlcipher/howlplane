#!/usr/bin/env python3
"""Builds the configured semantic recommender."""

from howlplane.control_plane.config_loader import SemanticRecommendationSettings
from howlplane.control_plane.semantic.howlinstinct import HowlInstinctSemanticRecommender
from howlplane.control_plane.semantic.recommender import (
    DisabledSemanticRecommender,
    SemanticRecommender,
)


def build_semantic_recommender(settings: SemanticRecommendationSettings) -> SemanticRecommender:
    """Returns the Disabled recommender unless an operator enabled a mode."""
    if settings.mode == "off":
        return DisabledSemanticRecommender()
    return HowlInstinctSemanticRecommender(command=settings.command, mode=settings.mode)
