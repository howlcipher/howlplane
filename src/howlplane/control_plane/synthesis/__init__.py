#!/usr/bin/env python3
"""
synthesis

Prompt-to-Product Synthesis Package for HowlPlane.
"""

from howlplane.control_plane.synthesis.acceptance_runner import (
    AcceptanceCheckResult,
    ProductAcceptanceReport,
    ProductAcceptanceRunner,
)
from howlplane.control_plane.synthesis.capability_negotiator import (
    CapabilityNegotiator,
    FeasibilityStatus,
    FrameworkGap,
    HowlFrameCapabilityRegistry,
    NegotiationResult,
)
from howlplane.control_plane.synthesis.engine import (
    ProductBundle,
    ProductSynthesizer,
    SynthesisResult,
)
from howlplane.control_plane.synthesis.marathon import (
    DogfoodIterationResult,
    MarathonDogfoodEngine,
    MarathonSummaryReport,
    STANDARD_BENCHMARKS,
)
from howlplane.control_plane.synthesis.product_spec import (
    BehaviorSpec,
    EntitySpec,
    FieldSpec,
    PersistenceSpec,
    ProductSpec,
    ValidationRuleSpec,
)
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderExhaustionEvent,
    ProviderPoolManager,
    ProviderStatus,
)
from howlplane.control_plane.synthesis.campaign_state import (
    CAMPAIGN_STATE_SCHEMA_VERSION,
    DurableCampaignState,
    GitIntegrationRecord,
)
from howlplane.control_plane.synthesis.spec_synthesizer import NaturalLanguageSynthesizer

__all__ = [
    "AcceptanceCheckResult",
    "BehaviorSpec",
    "CAMPAIGN_STATE_SCHEMA_VERSION",
    "CapabilityNegotiator",
    "DogfoodIterationResult",
    "DurableCampaignState",
    "EntitySpec",
    "FeasibilityStatus",
    "FieldSpec",
    "FrameworkGap",
    "GitIntegrationRecord",
    "HowlFrameCapabilityRegistry",
    "MarathonDogfoodEngine",
    "MarathonSummaryReport",
    "NaturalLanguageSynthesizer",
    "NegotiationResult",
    "PersistenceSpec",
    "ProductAcceptanceReport",
    "ProductAcceptanceRunner",
    "ProductBundle",
    "ProductSpec",
    "ProductSynthesizer",
    "ProviderAvailabilityStatus",
    "ProviderExhaustionEvent",
    "ProviderPoolManager",
    "ProviderStatus",
    "STANDARD_BENCHMARKS",
    "SynthesisResult",
    "ValidationRuleSpec",
]
