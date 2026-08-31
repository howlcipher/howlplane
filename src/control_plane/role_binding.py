#!/usr/bin/env python3
"""
role_binding.py

Domain-neutral role execution, capability dispatch, and provider binding
framework. Allows any domain (software engineering, writing, research) to
bind specialized roles (e.g. humanizer, meaning_reviewer, writer, editor)
to real underlying model executors/providers while maintaining reviewer
independence, policy compliance, timeout budgets, and durable evidence.
"""


from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import yaml

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
)
from src.control_plane.agent_registry import AgentRegistry
from src.control_plane.task_spec import DataClassSerializationMixin

ROLE_BINDING_SCHEMA_VERSION = "howlplane.role_binding/v1"
ROLE_EXECUTION_SCHEMA_VERSION = "howlplane.role_execution/v1"


class IndependenceStatus(str, Enum):
    """Observable status of reviewer independence."""

    INDEPENDENT = "INDEPENDENT"
    SAME_PROVIDER = "SAME_PROVIDER"
    NOT_REVIEWED = "NOT_REVIEWED"
    UNAVAILABLE = "UNAVAILABLE"


class RoleExecutionError(RuntimeError):
    """Base error for role execution failures."""
    pass


class RoleNotConfiguredError(RoleExecutionError):
    """Raised when a requested domain role has no executor configured."""

    def __init__(self, domain: str, role: str):
        self.domain = domain
        self.role = role
        super().__init__(
            f"No executor or provider configured for role '{role}' "
            f"in domain '{domain}'."
        )


@dataclass
class RoleDescriptor(DataClassSerializationMixin):
    """Declarative description of a role across any domain."""

    domain: str
    role: str
    capability: str
    description: str = ""
    default_provider: Optional[str] = None
    default_model: Optional[str] = None
    requires_reviewer_independence: bool = False


@dataclass
class RoleBinding(DataClassSerializationMixin):
    """Explicit binding of a (domain, role) pair to a provider/executor."""

    domain: str
    role: str
    provider: str
    model: Optional[str] = None
    interface: Optional[str] = None
    capability: Optional[str] = None
    timeout_seconds: int = 300
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: str = ROLE_BINDING_SCHEMA_VERSION


@dataclass
class RoleExecutionRequest(DataClassSerializationMixin):
    """Structured invocation request for a domain role."""

    domain: str
    role: str
    prompt: str
    capability: Optional[str] = None
    system_instruction: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    avoid_provider: Optional[str] = None
    preferred_provider: Optional[str] = None
    timeout_seconds: int = 300
    cwd: Optional[Union[str, Path]] = None
    structured_output_schema: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: str = ROLE_EXECUTION_SCHEMA_VERSION


@dataclass
class RoleExecutionResult(DataClassSerializationMixin):
    """Durable, structured result from a domain role invocation."""

    domain: str
    role: str
    provider: str
    model: Optional[str] = None
    success: bool = True
    raw_output: str = ""
    structured_output: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0
    independence_status: str = IndependenceStatus.INDEPENDENT.value
    error_message: Optional[str] = None
    timed_out: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema: str = ROLE_EXECUTION_SCHEMA_VERSION


def extract_structured_output(text: str) -> Optional[Dict[str, Any]]:
    """Extracts JSON or YAML object from model output text or code blocks."""
    if not text or not text.strip():
        return None

    clean = text.strip()

    # 1. Try fenced json or yaml code blocks
    matches = re.findall(r"```(?:yaml|json)?\s*\n([\s\S]*?)\n```", clean)
    for block in matches:
        candidate = block.strip()
        try:
            parsed = yaml.safe_load(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            try:
                parsed_json = json.loads(candidate)
                if isinstance(parsed_json, dict):
                    return parsed_json
            except Exception:
                pass

    # 2. Extract from Codex/CLI transcript if present
    codex_match = re.search(
        r"(?:^|\n)codex\s*\n([\s\S]*?)(?:\ntokens used|\Z)",
        clean,
        re.IGNORECASE,
    )
    if codex_match:
        sub_text = codex_match.group(1).strip()
        sub_matches = re.findall(
            r"```(?:yaml|json)?\s*\n([\s\S]*?)\n```", sub_text
        )
        for block in sub_matches:
            try:
                parsed = yaml.safe_load(block.strip())
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        try:
            parsed = yaml.safe_load(sub_text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 3. Try direct parse of entire text as JSON or YAML
    try:
        parsed_json = json.loads(clean)
        if isinstance(parsed_json, dict):
            return parsed_json
    except Exception:
        pass

    try:
        parsed_yaml = yaml.safe_load(clean)
        if isinstance(parsed_yaml, dict):
            return parsed_yaml
    except Exception:
        pass

    # 4. Try JSON substring if braced
    first_b = clean.find("{")
    last_b = clean.rfind("}")
    if first_b != -1 and last_b != -1 and last_b > first_b:
        try:
            parsed_json = json.loads(clean[first_b:last_b + 1])
            if isinstance(parsed_json, dict):
                return parsed_json
        except Exception:
            pass

    return None


class RoleBindingRegistry:
    """Central declarative registry mapping domain roles to providers."""

    def __init__(self) -> None:
        self._bindings: Dict[Tuple[str, str], RoleBinding] = {}
        self._descriptors: Dict[Tuple[str, str], RoleDescriptor] = {}

    def register_descriptor(self, descriptor: RoleDescriptor) -> None:
        key = (descriptor.domain.lower(), descriptor.role.lower())
        self._descriptors[key] = descriptor

    def register_binding(self, binding: RoleBinding) -> None:
        key = (binding.domain.lower(), binding.role.lower())
        self._bindings[key] = binding

    def get_binding(self, domain: str, role: str) -> Optional[RoleBinding]:
        key = (domain.lower(), role.lower())
        return self._bindings.get(key)

    def get_descriptor(
        self, domain: str, role: str
    ) -> Optional[RoleDescriptor]:
        key = (domain.lower(), role.lower())
        return self._descriptors.get(key)

    def list_bindings(
        self, domain: Optional[str] = None
    ) -> List[RoleBinding]:
        if domain is None:
            return list(self._bindings.values())
        return [
            b for b in self._bindings.values()
            if b.domain.lower() == domain.lower()
        ]

    def clear(self) -> None:
        self._bindings.clear()
        self._descriptors.clear()

    def load_from_config(self, config_data: Dict[str, Any]) -> None:
        """Parses role bindings from config structure."""
        roles_section = config_data.get("roles", {})
        if not isinstance(roles_section, dict):
            return

        for domain_key, domain_val in roles_section.items():
            if not isinstance(domain_val, dict):
                continue
            for role_key, role_spec in domain_val.items():
                if isinstance(role_spec, str):
                    # Shorthand: humanizer = "claude_code"
                    self.register_binding(
                        RoleBinding(
                            domain=domain_key,
                            role=role_key,
                            provider=role_spec,
                        )
                    )
                elif isinstance(role_spec, dict):
                    # Full spec: [roles.writing.humanizer] provider = "..."
                    provider = (
                        role_spec.get("provider")
                        or role_spec.get("agent_id")
                    )
                    if provider:
                        self.register_binding(
                            RoleBinding(
                                domain=domain_key,
                                role=role_key,
                                provider=provider,
                                model=role_spec.get("model"),
                                interface=role_spec.get("interface"),
                                capability=role_spec.get("capability"),
                                timeout_seconds=role_spec.get(
                                    "timeout_seconds", 300
                                ),
                                metadata=role_spec.get("metadata", {}),
                            )
                        )

    def load_from_env(self) -> None:
        """Loads bindings from HOWLPLANE_ROLE_<DOMAIN>_<ROLE> env vars."""
        prefix = "HOWLPLANE_ROLE_"
        for key, value in os.environ.items():
            if key.startswith(prefix) and value.strip():
                rest = key[len(prefix):].lower()
                parts = rest.split("_", 1)
                if len(parts) == 2:
                    domain, role = parts
                    self.register_binding(
                        RoleBinding(
                            domain=domain,
                            role=role,
                            provider=value.strip(),
                        )
                    )


_GLOBAL_ROLE_REGISTRY: Optional[RoleBindingRegistry] = None


def get_default_role_registry() -> RoleBindingRegistry:
    global _GLOBAL_ROLE_REGISTRY
    if _GLOBAL_ROLE_REGISTRY is None:
        _GLOBAL_ROLE_REGISTRY = RoleBindingRegistry()
        _GLOBAL_ROLE_REGISTRY.load_from_env()
    return _GLOBAL_ROLE_REGISTRY


class RoleDispatcher:
    """Resolves and executes domain roles through HowlPlane backends."""

    def __init__(
        self,
        binding_registry: Optional[RoleBindingRegistry] = None,
        agent_registry: Optional[AgentRegistry] = None,
    ) -> None:
        self.binding_registry = binding_registry or get_default_role_registry()
        self.agent_registry = agent_registry or AgentRegistry()

    def resolve_provider(
        self,
        domain: str,
        role: str,
        avoid_provider: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], str]:
        """Resolves target provider with honest independence calculation."""
        binding = self.binding_registry.get_binding(domain, role)
        candidate_provider = (
            preferred_provider or (binding.provider if binding else None)
        )
        model_id = binding.model if binding else None

        if not candidate_provider:
            return None, None, IndependenceStatus.UNAVAILABLE.value

        norm_candidate = AgentBackendRegistry.normalize_agent_id(
            candidate_provider
        )
        norm_avoid = (
            AgentBackendRegistry.normalize_agent_id(avoid_provider)
            if avoid_provider
            else None
        )

        if not norm_avoid:
            return (
                candidate_provider,
                model_id,
                IndependenceStatus.NOT_REVIEWED.value,
            )

        if norm_candidate != norm_avoid:
            return (
                candidate_provider,
                model_id,
                IndependenceStatus.INDEPENDENT.value,
            )

        # Candidate matches avoid_provider. Find available alternate.
        available_agents = self.agent_registry.list_agents(only_available=True)
        alternate = next(
            (
                a.agent_id
                for a in available_agents
                if AgentBackendRegistry.normalize_agent_id(a.agent_id)
                != norm_avoid
                and AgentBackendRegistry.get_backend(a.agent_id).is_available()
            ),
            None,
        )

        if alternate:
            return alternate, None, IndependenceStatus.INDEPENDENT.value

        return (
            candidate_provider,
            model_id,
            IndependenceStatus.SAME_PROVIDER.value,
        )

    def execute(
        self,
        request: RoleExecutionRequest,
        custom_backend: Optional[AgentBackend] = None,
        backend_resolver: Optional[Callable[[str], AgentBackend]] = None,
    ) -> RoleExecutionResult:
        """Executes a domain role request against the resolved backend."""
        start_time = time.time()

        if custom_backend is not None:
            provider_id = getattr(
                custom_backend, "agent_id", "custom_backend"
            )
            independence_status = (
                IndependenceStatus.INDEPENDENT.value
                if request.avoid_provider != provider_id
                else IndependenceStatus.SAME_PROVIDER.value
            )
            model_id = None
        else:
            provider_id, model_id, independence_status = self.resolve_provider(
                domain=request.domain,
                role=request.role,
                avoid_provider=request.avoid_provider,
                preferred_provider=request.preferred_provider,
            )

        if not provider_id:
            return RoleExecutionResult(
                domain=request.domain,
                role=request.role,
                provider="none",
                model=None,
                success=False,
                duration_seconds=0.0,
                independence_status=IndependenceStatus.UNAVAILABLE.value,
                error_message=(
                    f"No executor or provider configured for role "
                    f"'{request.role}' in domain '{request.domain}'."
                ),
                metadata={"request": request.to_dict()},
            )

        if custom_backend is not None:
            backend = custom_backend
        elif backend_resolver is not None:
            backend = backend_resolver(provider_id)
        else:
            backend = AgentBackendRegistry.get_backend(provider_id)

        target_cwd = Path(request.cwd).resolve() if request.cwd else Path.cwd()

        full_prompt = request.prompt
        if request.system_instruction:
            full_prompt = f"{request.system_instruction}\n\n{request.prompt}"

        try:
            agent_res: AgentExecutionResult = backend.execute(
                task=None,
                cwd=target_cwd,
                role=f"{request.domain}:{request.role}",
                prompt_override=full_prompt,
                timeout_seconds=request.timeout_seconds,
            )
        except Exception as exc:
            elapsed = round(time.time() - start_time, 3)
            return RoleExecutionResult(
                domain=request.domain,
                role=request.role,
                provider=provider_id,
                model=model_id,
                success=False,
                duration_seconds=elapsed,
                independence_status=independence_status,
                error_message=f"Backend execution exception: {exc}",
                metadata={"request": request.to_dict()},
            )

        elapsed = round(time.time() - start_time, 3)

        if not agent_res.success:
            err = (
                agent_res.error_message
                or agent_res.stderr
                or "Provider execution failed"
            )
            return RoleExecutionResult(
                domain=request.domain,
                role=request.role,
                provider=provider_id,
                model=model_id,
                success=False,
                raw_output=agent_res.stdout,
                duration_seconds=elapsed,
                independence_status=independence_status,
                error_message=err,
                timed_out=agent_res.timed_out,
                metadata={
                    "request": request.to_dict(),
                    "agent_result": agent_res.to_dict(),
                },
            )

        raw_output = agent_res.stdout
        structured = extract_structured_output(raw_output)

        return RoleExecutionResult(
            domain=request.domain,
            role=request.role,
            provider=provider_id,
            model=model_id,
            success=True,
            raw_output=raw_output,
            structured_output=structured,
            duration_seconds=elapsed,
            independence_status=independence_status,
            error_message=None,
            timed_out=agent_res.timed_out,
            metadata={
                "request": request.to_dict(),
                "agent_result": agent_res.to_dict(),
            },
        )
