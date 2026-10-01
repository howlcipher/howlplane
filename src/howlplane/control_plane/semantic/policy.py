#!/usr/bin/env python3
"""Plane-owned acceptance policy for a semantic judgment.

HowlInstinct has no threshold. The only cutoff in this feature is the operator's
``minimum_instinct_margin``, applied here and nowhere else.
"""

from dataclasses import replace
from typing import Optional

from howlplane.control_plane.semantic.models import SemanticRecommendation, SemanticStatus


def apply_policy(
    judged: SemanticRecommendation,
    *,
    minimum_instinct_margin: Optional[float],
) -> SemanticRecommendation:
    """Turns a JUDGED recommendation into ACCEPTED or LOW_CONFIDENCE.

    Any other status is returned unchanged. A margin that was not reported is
    LOW_CONFIDENCE; it is never treated as zero and never as a rejection of the
    resource itself.
    """
    if judged.status is not SemanticStatus.JUDGED:
        return judged
    if minimum_instinct_margin is None:
        return replace(
            judged,
            status=SemanticStatus.LOW_CONFIDENCE,
            reason="no minimum_instinct_margin is configured",
            accepted_by_policy=False,
        )
    if judged.instinct_margin is None:
        return replace(
            judged,
            status=SemanticStatus.LOW_CONFIDENCE,
            reason="instinct_margin was not reported",
            minimum_instinct_margin=minimum_instinct_margin,
            accepted_by_policy=False,
        )
    if judged.instinct_margin >= minimum_instinct_margin:
        return replace(
            judged,
            status=SemanticStatus.ACCEPTED,
            reason="instinct_margin meets the configured minimum",
            minimum_instinct_margin=minimum_instinct_margin,
            accepted_by_policy=True,
        )
    return replace(
        judged,
        status=SemanticStatus.LOW_CONFIDENCE,
        reason="instinct_margin is below the configured minimum",
        minimum_instinct_margin=minimum_instinct_margin,
        accepted_by_policy=False,
    )
