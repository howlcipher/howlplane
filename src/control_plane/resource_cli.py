"""Read-only and targeted operator views for the shared AI resource pool."""

from typing import Any, Dict, List

from src.control_plane.resource_models import AI_RESOURCE_INVENTORY_SCHEMA_VERSION


def inventory_document(pool: Any) -> Dict[str, Any]:
    """Returns the stable machine-readable `ai providers` contract."""
    return {
        "schema": AI_RESOURCE_INVENTORY_SCHEMA_VERSION,
        "operating_mode": pool.operating_mode,
        "provider_policy": pool.policy.model_dump(),
        "resources": pool.inventory(),
    }


def render_inventory(pool: Any) -> str:
    """Renders resource inventory without guessed capacity percentages."""
    document = inventory_document(pool)
    policy = document["provider_policy"]
    lines = [
        "AI RESOURCE POOL",
        f"Operating mode: {document['operating_mode']}",
        f"Strategy: {policy['strategy']}",
        f"Subscription first: {str(policy['subscription_first']).lower()}",
        f"Paid API allowed: {str(policy['allow_paid_api']).lower()}",
        f"External before local: {str(policy['external_before_local']).lower()}",
        f"Independent review: {str(policy['preserve_independent_review']).lower()}",
        "",
        f"{'RESOURCE':<20} {'TYPE':<14} {'ENABLED':<8} {'READINESS':<20} CAPACITY",
    ]
    for row in document["resources"]:
        lines.append(
            f"{row['name']:<20} {row['economic_class']:<14} "
            f"{('yes' if row['enabled'] else 'no'):<8} "
            f"{row['readiness']:<20} {row['capacity']}"
        )
    return "\n".join(lines)


def render_route(decision: Any) -> str:
    """Explains one non-generative selection decision."""
    recommendation = decision.cognitive_recommendation
    selected = decision.selected
    lines = [
        f"Task class: {decision.task_class}",
        f"Role: {decision.role}",
        "Required capabilities: " + (
            ", ".join(decision.required_capabilities) or "none"
        ),
        "Eligible:",
    ]
    if decision.eligible_resources:
        lines.extend(
            f"  {identity.resource_id} ({identity.interface_id}; model={identity.model_id or 'unknown'})"
            for identity in decision.eligible_resources
        )
    else:
        lines.append("  none")
    lines.append("Excluded:")
    if decision.exclusions:
        lines.extend(
            f"  {item.resource_id}: {item.reason} [{item.stage}]"
            for item in decision.exclusions
        )
    else:
        lines.append("  none")
    lines.extend([
        f"Economic policy: {decision.economic_policy}",
        "Recommendation: " + (
            recommendation.resource_id if recommendation and recommendation.resource_id
            else "none"
        ),
        "Reason: " + (
            recommendation.reason if recommendation else "No eligible candidate."
        ),
        "Likely selected: " + (
            f"{selected.resource_id} / {selected.interface_id} / {selected.model_id or 'unknown'}"
            if selected else "BLOCKED: NO_ELIGIBLE_AI_RESOURCE"
        ),
    ])
    return "\n".join(lines)


def resource_diagnostic_rows(pool: Any) -> List[Dict[str, str]]:
    """Builds lightweight doctor facts without provider generation."""
    rows: List[Dict[str, str]] = [{
        "name": "AI Resource Configuration",
        "status": "ok",
        "message": "Configuration valid; runtime readiness is reported separately.",
    }]
    unavailable = {
        "MISSING_EXECUTABLE", "AUTH_REQUIRED", "UNREACHABLE", "UNAVAILABLE",
    }
    for item in pool.inventory():
        if not item["configured"]:
            status = "ok"
            message = "registered but not configured; not eligible or probed"
        elif not item["enabled"]:
            status = "ok"
            message = f"not probed: {item['reason']}"
        elif item["readiness"] in unavailable:
            status = "warning"
            message = f"{item['readiness']}: {item['reason'] or 'unavailable'}"
        else:
            status = "ok"
            message = (
                f"readiness={item['readiness']}; capacity={item['capacity']}; "
                "generation capacity is not actively consumed by doctor"
            )
        rows.append({
            "name": f"AI Resource: {item['name']}",
            "status": status,
            "message": message,
        })
    return rows
