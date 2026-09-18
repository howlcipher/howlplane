#!/usr/bin/env python3
"""
capability_negotiator.py

Negotiates product specifications against verified HowlFrame language, runtime,
and compiler capabilities. Prevents hallucinations of unsupported features and
returns precise, structured framework gaps when a requested outcome exceeds
current HowlFrame capabilities.
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, List, Optional

from howlplane.control_plane.synthesis.product_spec import ProductSpec
from howlplane.control_plane.task_spec import DataClassSerializationMixin

CAPABILITY_NEGOTIATOR_SCHEMA_VERSION = "howlplane.capability_negotiation/v1"


class FeasibilityStatus(str, Enum):
    FEASIBLE = "FEASIBLE"
    PARTIALLY_FEASIBLE = "PARTIALLY_FEASIBLE"
    INFEASIBLE = "INFEASIBLE"


@dataclass
class FrameworkGap(DataClassSerializationMixin):
    """Represents a concrete gap between product requirements and HowlFrame capabilities."""

    code: str
    required_behavior: str
    current_support: str
    suggested_enhancement: str
    blocking: bool = True

    def render_text(self) -> str:
        status_tag = "PRODUCT_BLOCKED" if self.blocking else "CAPABILITY_DEGRADED"
        return (
            f"{status_tag}:\n"
            f"  Code: {self.code}\n"
            f"  Required behavior: {self.required_behavior}\n"
            f"  Current HowlFrame support: {self.current_support}\n"
            f"  Suggested framework gap: {self.suggested_enhancement}"
        )


@dataclass
class NegotiationResult(DataClassSerializationMixin):
    """Complete result packet from capability negotiation."""

    status: FeasibilityStatus
    product_name: str
    granted_capabilities: List[str]
    supported_interfaces: List[str]
    supported_behaviors: List[str]
    unsupported_behaviors: List[str]
    framework_gaps: List[FrameworkGap] = field(default_factory=list)
    recommended_target: str = "bytecode"  # "bytecode", "gogen", "javascript"
    recommended_architecture: Dict[str, Any] = field(default_factory=dict)
    schema: str = CAPABILITY_NEGOTIATOR_SCHEMA_VERSION

    @property
    def is_feasible(self) -> bool:
        return self.status in (FeasibilityStatus.FEASIBLE, FeasibilityStatus.PARTIALLY_FEASIBLE)


class HowlFrameCapabilityRegistry:
    """
    Authoritative registry of supported HowlFrame constructs, runtime primitives,
    interfaces, and persistence mechanisms.
    """

    # Supported root forms in HowlFrame
    SUPPORTED_ROOT_FORMS = {"http_server", "web_app", "cli_app", "module"}

    # Supported execution targets
    SUPPORTED_TARGETS = {"bytecode", "gogen", "javascript", "wasm"}

    # Supported capabilities in HowlFrame VM
    SUPPORTED_CAPABILITIES = {"network", "database", "filesystem", "process", "environment"}

    # Supported persistence schemes
    SUPPORTED_STORE_SCHEMES = {"file://", "memory://"}

    # Explicitly known unsupported constructs and architectural limitations
    KNOWN_LIMITATIONS = {
        "atomic_shared_counter": (
            "Single-threaded record store does not support lock-free cross-process atomic mutations.",
            "Add atomic integer increment opcode (STORE_ATOMIC_INCR) to HowlFrame VM and store runtime.",
        ),
        "raw_websocket_server": (
            "HowlFrame http_server primitive currently supports HTTP 1.1 request/response cycles only.",
            "Introduce (websocket_server ...) root form and event loop in internal/backend/gogen and VM.",
        ),
        "distributed_database": (
            "HowlFrame store_open only connects to local JSON file or memory stores.",
            "Implement native SQL / Postgres store adapter under database capability.",
        ),
        "multi_threaded_background_worker": (
            "HowlFrame execution threads are synchronous within a single VM instance.",
            "Add actor-based background task scheduler with channel communication.",
        ),
    }


class CapabilityNegotiator:
    """
    Evaluates a ProductSpec against HowlFrame toolchain and runtime capabilities.
    """

    def __init__(self, registry: Optional[HowlFrameCapabilityRegistry] = None):
        self.registry = registry or HowlFrameCapabilityRegistry()

    def negotiate(self, spec: ProductSpec) -> NegotiationResult:
        """
        Performs fail-closed negotiation of product specification against
        HowlFrame language and runtime capabilities.
        """
        gaps: List[FrameworkGap] = []
        supported_ifaces: List[str] = []
        supported_behaviors: List[str] = []
        unsupported_behaviors: List[str] = []
        granted_caps: List[str] = []

        # 1. Evaluate Interfaces
        for iface in spec.interfaces:
            if iface in ("browser_ui", "http_api", "cli"):
                supported_ifaces.append(iface)
            else:
                gaps.append(FrameworkGap(
                    code="HF_UNSUPPORTED_INTERFACE",
                    required_behavior=f"Interface '{iface}'",
                    current_support="Only 'browser_ui', 'http_api', and 'cli' are supported.",
                    suggested_enhancement=f"Add compiler/backend support for interface '{iface}'.",
                    blocking=True,
                ))

        # 2. Evaluate Persistence
        p_type = spec.persistence.type
        if p_type == "local_store":
            if not spec.persistence.storage_path.startswith("file://") and not spec.persistence.storage_path.endswith(".json"):
                # Normalizable to file://
                pass
            granted_caps.extend(["database", "filesystem"])
        elif p_type == "memory_store":
            granted_caps.append("database")
        elif p_type == "none":
            pass
        else:
            gaps.append(FrameworkGap(
                code="HF_UNSUPPORTED_PERSISTENCE",
                required_behavior=f"Persistence type '{p_type}'",
                current_support="Only 'local_store' (file://*.json) and 'memory_store' (memory://) are supported.",
                suggested_enhancement="Extend native store adapter for requested persistence engine.",
                blocking=True,
            ))

        if "http_api" in spec.interfaces or "browser_ui" in spec.interfaces:
            granted_caps.append("network")

        # 3. Evaluate Behaviors & Entities for Known Limitations
        desc_lower = (spec.description + " " + " ".join(b.description for b in spec.behaviors)).lower()

        gap_rules = [
            (
                lambda s: "atomic" in s and ("counter" in s or "increment" in s or "mutation" in s),
                "atomic_shared_counter",
                "HF_GAP_ATOMIC_MUTATION",
                "Atomic shared counter mutation across concurrent requests",
            ),
            (
                lambda s: any(k in s for k in ("websocket", "socket.io", "real-time stream")),
                "raw_websocket_server",
                "HF_GAP_WEBSOCKET",
                "Raw WebSocket streaming server",
            ),
            (
                lambda s: any(k in s for k in ("postgres", "mysql", "redis cluster", "mongodb", "distributed sql")),
                "distributed_database",
                "HF_GAP_DISTRIBUTED_DB",
                "External distributed database connection",
            ),
            (
                lambda s: any(k in s for k in ("background thread", "celery", "async worker")),
                "multi_threaded_background_worker",
                "HF_GAP_ASYNC_WORKER",
                "Multi-threaded asynchronous background worker",
            ),
        ]
        for matcher, reg_key, code, required_behavior in gap_rules:
            if matcher(desc_lower):
                cur, sugg = self.registry.KNOWN_LIMITATIONS[reg_key]
                gaps.append(FrameworkGap(
                    code=code,
                    required_behavior=required_behavior,
                    current_support=cur,
                    suggested_enhancement=sugg,
                    blocking=True,
                ))

        # Classify behaviors
        for b in spec.behaviors:
            if any(g.blocking and (b.name in g.required_behavior.lower() or b.description in g.required_behavior) for g in gaps):
                unsupported_behaviors.append(b.name)
            else:
                supported_behaviors.append(b.name)

        # Classify Overall Feasibility
        blocking_gaps = [g for g in gaps if g.blocking]
        if blocking_gaps:
            status = FeasibilityStatus.INFEASIBLE
        elif gaps:
            status = FeasibilityStatus.PARTIALLY_FEASIBLE
        else:
            status = FeasibilityStatus.FEASIBLE

        # Recommended Architecture
        arch: Dict[str, Any] = {
            "root_form": "http_server" if ("http_api" in spec.interfaces or "browser_ui" in spec.interfaces) else "cli_app",
            "frontend_form": "web_app" if "browser_ui" in spec.interfaces else None,
            "target": "bytecode",
            "persistence_scheme": "file://" if spec.persistence.type == "local_store" else "memory://",
            "required_capabilities": sorted(list(set(granted_caps + spec.capabilities_required))),
            "port": spec.default_port,
        }

        return NegotiationResult(
            status=status,
            product_name=spec.name,
            granted_capabilities=arch["required_capabilities"],
            supported_interfaces=supported_ifaces,
            supported_behaviors=supported_behaviors,
            unsupported_behaviors=unsupported_behaviors,
            framework_gaps=gaps,
            recommended_target="bytecode",
            recommended_architecture=arch,
        )
