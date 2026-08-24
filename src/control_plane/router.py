#!/usr/bin/env python3
"""
router.py

Deterministic routing layer that selects agent types based on task characteristics,
skills, reasoning tiers, risk levels, and human overrides.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.task_spec import TaskSpec


@dataclass
class RoutingDecision:
    """Represents the output of the task routing determination."""

    selected_agent_id: str
    selected_agent_name: str
    reasoning_tier: str
    rationale: str
    recommended_reviewers: List[str]
    reviewer_selection_reasons: Dict[str, str] = field(default_factory=dict)
    is_override: bool = False
    alternatives: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def render_text(self, task_id: str = "") -> str:
        lines = [
            "=" * 60,
            f"TASK ROUTING DECISION: {task_id}".strip(),
            "=" * 60,
            f"Selected Agent: {self.selected_agent_name} (`{self.selected_agent_id}`)",
            f"Reasoning Tier: {self.reasoning_tier}",
            f"Is Override:    {self.is_override}",
            f"Rationale:      {self.rationale}",
            f"Reviewers:      {', '.join(self.recommended_reviewers)}",
        ]
        if self.alternatives:
            lines.append(f"Alternatives:   {', '.join(self.alternatives)}")
        lines.append("=" * 60)
        return "\n".join(lines)


class TaskRouter:
    """
    Deterministic task router that matches task specifications to agent capabilities
    without making external API calls.
    """

    def __init__(self, registry: Optional[AgentRegistry] = None):
        self.registry = registry or AgentRegistry()

    def route(self, task: TaskSpec) -> RoutingDecision:
        """
        Calculates the recommended agent and reviewers for a given task specification.
        """
        reviewers, reviewer_reasons = self._select_reviewers(task)

        # 1. Check for human / policy override
        if task.preferred_agent:
            target = self.registry.get_agent(task.preferred_agent)
            if target:
                if target.availability == "available":
                    return RoutingDecision(
                        selected_agent_id=target.agent_id,
                        selected_agent_name=target.name,
                        reasoning_tier=target.reasoning_tier,
                        rationale=(
                            f"User/policy override explicitly requested agent '{target.agent_id}' ({target.name})."
                        ),
                        recommended_reviewers=reviewers,
                        reviewer_selection_reasons=reviewer_reasons,
                        is_override=True,
                        alternatives=[
                            a.agent_id
                            for a in self.registry.list_agents(only_available=True)
                            if a.agent_id != target.agent_id
                        ],
                    )

        # 2. Evaluate task risk, skills, and reasoning requirements
        available_agents = self.registry.list_agents(only_available=True)
        if not available_agents:
            raise RuntimeError("No available agents found in the registry.")

        skills = set(task.required_skills)
        is_security = bool(
            skills.intersection({"cyber_security", "blue_team", "red_team", "bug_bounty_hunter"})
        )
        is_arch = bool(
            skills.intersection({"architectural_guardrails", "systems_logic", "technical_writing"})
        )
        is_autonomous = "autonomous_workflow" in task.allowed_tools or task.risk_level in ("high", "critical")

        # 3. Apply deterministic decision matrix
        selected_agent: Optional[AgentProfile] = None
        rationale_lines: List[str] = []

        if task.recommended_reasoning_tier == "tier_1" or task.risk_level in ("high", "critical") or is_security or is_arch:
            # Tier 1 reasoning candidates
            tier_1_agents = [a for a in available_agents if a.reasoning_tier == "tier_1"]
            if tier_1_agents:
                # Prefer claude_code if orchestration/architecture, devin_cli if autonomous
                if is_autonomous and any(a.agent_id == "devin_cli" for a in tier_1_agents):
                    selected_agent = next(a for a in tier_1_agents if a.agent_id == "devin_cli")
                    rationale_lines.append(
                        "Selected Devin CLI for autonomous multi-step execution under high risk/tier 1 constraints."
                    )
                else:
                    selected_agent = tier_1_agents[0]
                    rationale_lines.append(
                        f"Selected {selected_agent.name} for Tier 1 reasoning, architectural depth, and risk handling."
                    )
            else:
                # Fall back to strongest Tier 2 agent
                tier_2_agents = [a for a in available_agents if a.reasoning_tier == "tier_2"]
                selected_agent = tier_2_agents[0] if tier_2_agents else available_agents[0]
                rationale_lines.append(
                    f"Tier 1 requested but unavailable; routed to highest available Tier 2 agent: {selected_agent.name}."
                )

        elif task.recommended_reasoning_tier == "tier_3" and task.risk_level == "low":
            # Local / low-cost candidates
            local_agents = [a for a in available_agents if a.cost_class == "free_local"]
            if local_agents:
                selected_agent = local_agents[0]
                rationale_lines.append(
                    f"Selected {selected_agent.name} to minimize cost and preserve session limits for low-risk Tier 3 work."
                )
            else:
                selected_agent = available_agents[0]
                rationale_lines.append(
                    f"Selected {selected_agent.name} for low-risk standard execution."
                )

        else:
            # Standard Tier 2 implementation
            tier_2_agents = [a for a in available_agents if a.reasoning_tier == "tier_2"]
            if tier_2_agents:
                # If headless delegation is allowed, prefer agy
                if "headless_cli" in [a.interface for a in tier_2_agents]:
                    selected_agent = next(
                        (a for a in tier_2_agents if a.interface == "headless_cli"),
                        tier_2_agents[0],
                    )
                else:
                    selected_agent = tier_2_agents[0]
                rationale_lines.append(
                    f"Selected {selected_agent.name} for balanced standard implementation (Tier 2)."
                )
            else:
                selected_agent = available_agents[0]
                rationale_lines.append(f"Selected {selected_agent.name} as best available general agent.")

        # Build alternatives list
        alternatives = [a.agent_id for a in available_agents if a.agent_id != selected_agent.agent_id]

        return RoutingDecision(
            selected_agent_id=selected_agent.agent_id,
            selected_agent_name=selected_agent.name,
            reasoning_tier=selected_agent.reasoning_tier,
            rationale=" ".join(rationale_lines),
            recommended_reviewers=reviewers,
            reviewer_selection_reasons=reviewer_reasons,
            is_override=False,
            alternatives=alternatives,
        )

    def _select_reviewers(self, task: TaskSpec) -> Tuple[List[str], Dict[str, str]]:
        """
        Determines the specialized reviewer roles required for the task and records explicit rationales.
        """
        reviewers: List[str] = []
        reasons: Dict[str, str] = {}

        def add_reviewer(role: str, reason: str):
            if role not in reviewers:
                reviewers.append(role)
                reasons[role] = reason

        # If the task explicitly lists required reviewers, honor that as the
        # authoritative starting set instead of the default baseline. This lets
        # special tasks (e.g., the live acceptance canary, which produces an
        # evidence artifact rather than production code) avoid reviewers that
        # are inappropriate for their scope, while preserving the security and
        # risk-based additions below.
        if task.reviewer_requirements:
            for req in task.reviewer_requirements:
                add_reviewer(req, "Explicitly required by TaskSpec reviewer_requirements.")
        else:
            # Baseline reviewers for ordinary tasks
            add_reviewer("correctness-reviewer", "Baseline correctness, edge cases, and acceptance contract verification.")
            add_reviewer("test-falsifier", "Adversarial test verification, missing negative branch and vacuous assertion checks.")

        skills = set(task.required_skills)
        if task.risk_level in ("high", "critical") or skills.intersection(
            {"cyber_security", "blue_team", "red_team", "bug_bounty_hunter"}
        ):
            add_reviewer("security-reviewer", "Triggered by high/critical risk level or security-related skill requirements.")

        if skills.intersection({"architectural_guardrails", "systems_logic", "technical_writing"}) or task.recommended_reasoning_tier == "tier_1":
            add_reviewer("architecture-reviewer", "Triggered by Tier 1 reasoning requirement or architectural skill requirements.")

        if task.risk_level in ("medium", "high", "critical"):
            add_reviewer("regression-reviewer", "Triggered by medium/high/critical risk scope to protect existing contracts and call sites.")

        # Simplicity reviewer for refactoring or multi-file work
        add_reviewer("simplicity-reviewer", "Minimal diff review to prevent overengineering and eliminate dead code.")

        return reviewers, reasons
