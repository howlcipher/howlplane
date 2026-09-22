"""
test_control_plane_schemas.py

Validates control plane JSON schemas against Draft 2020-12 and checks example payloads.
"""

import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator, validate, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schemas"

CONTROL_PLANE_SCHEMAS = [
    "task-spec.schema.json",
    "agent-registry.schema.json",
    "review-finding.schema.json",
    "verification-plan.schema.json",
    "evidence-entry.schema.json",
    "project-context-contract.schema.json",
    "project-context-audit.schema.json",
]


@pytest.mark.parametrize("schema_name", CONTROL_PLANE_SCHEMAS)
def test_control_plane_schema_is_valid_draft_2020_12(schema_name):
    schema_path = SCHEMA_DIR / schema_name
    assert schema_path.exists(), f"Missing schema file: {schema_path}"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_json = json.load(f)
    Draft202012Validator.check_schema(schema_json)


@pytest.mark.parametrize(
    "schema_file, valid_payload, invalid_payload",
    [
        (
            "task-spec.schema.json",
            {
                "schema": "ai.task_spec/v1",
                "task_id": "TASK-100",
                "repository": "howlplane",
                "objective": "Test objective",
                "acceptance_criteria": ["All tests pass"],
                "risk_level": "medium",
                "current_state": "discovered",
            },
            {"schema": "ai.task_spec/v1", "task_id": "TASK-100"},
        ),
        (
            "agent-registry.schema.json",
            {
                "schema": "ai.agent_registry/v1",
                "agents": [
                    {
                        "agent_id": "claude_code",
                        "name": "Claude Code",
                        "provider": "anthropic",
                        "interface": "cli",
                        "capabilities": ["code_generation"],
                        "reasoning_tier": "tier_1",
                        "cost_class": "subscription_included",
                        "availability": "available",
                        "roles": ["planning", "implementation", "remediation", "review"],
                    }
                ],
            },
            {"schema": "ai.agent_registry/v1"},
        ),
        (
            "review-finding.schema.json",
            {
                "schema": "ai.review_finding/v1",
                "id": "F001",
                "reviewer_role": "security-reviewer",
                "title": "Unsafe command execution",
                "severity": "high",
                "category": "security",
                "description": "shell=True used with untrusted input",
                "status": "open",
            },
            {"schema": "ai.review_finding/v1"},
        ),
        (
            "verification-plan.schema.json",
            {
                "schema": "ai.verification_plan/v1",
                "task_id": "TASK-100",
                "overall_status": "passed",
                "steps": [
                    {
                        "step_id": "step-01",
                        "name": "Run tests",
                        "command": "pytest",
                        "category": "unit_test",
                        "status": "verified",
                        "required": True,
                    }
                ],
            },
            {"schema": "ai.verification_plan/v1"},
        ),
        (
            "evidence-entry.schema.json",
            {
                "schema": "ai.evidence_entry/v1",
                "entry_id": "ev-123456",
                "task_id": "TASK-100",
                "agent_id": "claude_code",
                "action": "task_created",
                "timestamp": "2026-08-18T12:00:00Z",
            },
            {"schema": "ai.evidence_entry/v1"},
        ),
        (
            "project-context-contract.schema.json",
            {
                "schema": "howlplane.project_context/v1",
                "project_name": "howlplane",
                "project_types": ["go", "python"],
                "has_agents_md": True,
                "has_manifest": False,
                "verification": {
                    "test_count": 1,
                    "build_count": 1,
                    "lint_count": 1,
                    "hygiene_count": 1,
                },
                "hygiene_status": "configured_and_passed",
                "declared_capabilities": [],
            },
            {"schema": "howlplane.project_context/v1"},
        ),
        (
            "project-context-audit.schema.json",
            {
                "schema": "howlplane.project_context_audit/v1",
                "status": "PASS",
                "findings": [],
                "observed": {
                    "project_name": "howlplane",
                    "project_types": ["go", "python"],
                    "has_agents_md": True,
                    "has_manifest": False,
                    "verification_surfaces": 4,
                    "hygiene_status": "configured_and_passed",
                    "capabilities_count": 0,
                },
            },
            {"schema": "howlplane.project_context_audit/v1", "status": "INVALID_STATUS"},
        ),
    ],
)
def test_schema_valid_and_invalid_payloads(schema_file, valid_payload, invalid_payload):
    schema_path = SCHEMA_DIR / schema_file
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    validate(instance=valid_payload, schema=schema)
    with pytest.raises(ValidationError):
        validate(instance=invalid_payload, schema=schema)


def test_agent_registry_serialization_conforms_to_its_own_schema():
    """The registry's real output must validate against the schema it stamps.

    The example payload above is hand written, so it conformed by construction and
    could never catch drift in the serializer. `AgentRegistry.to_dict()` stamps
    `ai.agent_registry/v1` while emitting fields the schema forbade under
    `additionalProperties: false` -- `roles` among them, which is the field
    `ProviderPoolManager.select_resource` gates every candidate on via
    `ROLE_NOT_SUPPORTED`. A schema that rejects its own artifact proves nothing
    about it, so this test validates the serializer, not a fixture.
    """
    from src.control_plane.agent_registry import AgentRegistry

    schema_path = SCHEMA_DIR / "agent-registry.schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_json = json.load(f)

    document = AgentRegistry().to_dict()
    errors = sorted(
        Draft202012Validator(schema_json).iter_errors(document),
        key=lambda err: list(err.path),
    )
    assert not errors, "registry output violates ai.agent_registry/v1: " + "; ".join(
        f"{list(e.path)}: {e.message}" for e in errors
    )
    assert document["agents"], "registry serialized no agents, so conformance proves nothing"


def test_agent_registry_schema_covers_every_serialized_field():
    """No field may be emitted that the schema does not describe.

    `additionalProperties: false` already makes an undescribed field a validation
    error, but this asserts the same property directly so the failure message names
    the drifted fields instead of pointing at one agent entry.
    """
    from src.control_plane.agent_registry import AgentRegistry

    schema_path = SCHEMA_DIR / "agent-registry.schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_json = json.load(f)

    described = set(schema_json["properties"]["agents"]["items"]["properties"])
    emitted = set()
    for agent in AgentRegistry().to_dict()["agents"]:
        emitted |= set(agent)

    undescribed = sorted(emitted - described)
    assert not undescribed, f"serialized fields missing from the schema: {undescribed}"


def test_agent_registry_schema_requires_roles():
    """`roles` must be required, not optional.

    An agent document that omits `roles` is silently filled from the code side
    default factory in `AgentProfile`, which would widen a deliberately narrowed
    agent -- `local_ollama` is restricted to planning, review and synthesis
    specifically so it is never selected for mutating work.
    """
    schema_path = SCHEMA_DIR / "agent-registry.schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_json = json.load(f)

    item = schema_json["properties"]["agents"]["items"]
    assert "roles" in item["required"]

    without_roles = {
        "schema": "ai.agent_registry/v1",
        "agents": [
            {
                "agent_id": "a",
                "name": "A",
                "provider": "p",
                "interface": "cli",
                "capabilities": ["code_generation"],
                "reasoning_tier": "tier_1",
                "cost_class": "subscription_included",
                "availability": "available",
            }
        ],
    }
    with pytest.raises(ValidationError):
        validate(instance=without_roles, schema=schema_json)
