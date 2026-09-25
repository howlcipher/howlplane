#!/usr/bin/env python3
"""
reviewers.py

Specialized independent reviewer roles and prompt definitions.
Designed to challenge assumptions, falsify correctness, and identify subtle defects.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from howlplane.control_plane.task_spec import TaskSpec


@dataclass
class ReviewerRole:
    """Represents a specialized independent reviewer persona and falsification strategy."""

    role_id: str
    name: str
    focus_areas: List[str]
    falsification_goal: str
    prompt_template: str

    def render_brief(
        self,
        task: TaskSpec,
        diff_content: str,
        context: Optional[str] = None,
    ) -> str:
        """
        Renders a standardized, non-leading review brief for this reviewer role.
        """
        criteria_str = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "None specified."
        constraints_str = "\n".join(f"- {c}" for c in task.constraints) or "None specified."
        verif_str = "\n".join(f"- {v}" for v in task.verification_requirements) or "Standard project test suite."
        context_str = f"\n### Relevant Architecture & Context\n{context}\n" if context else ""

        brief = f"""# Independent Review Request: {self.name} (`{self.role_id}`)

## Falsification Directive
> **MANDATE:** Attempt to prove that this implementation does NOT satisfy the requirements, breaks existing contracts, introduces risk, or contains defects.

**Target Role Goal:** {self.falsification_goal}

## Task Objective
{task.objective}

## Acceptance Criteria
{criteria_str}

## Constraints
{constraints_str}

## Verification Expectations
{verif_str}
{context_str}
## Proposed Changes (Diff / Implementation)
```diff
{diff_content}
```

## Reviewer Instructions & Adversarial Criteria
{self.prompt_template}

## Output Format
Return your findings as a YAML list of finding objects (or state that zero defects were found):
```yaml
findings:
  - id: "F001"
    reviewer_role: "{self.role_id}"
    title: "Short descriptive title"
    severity: "blocker" # blocker | high | medium | low | informational
    category: "{self.focus_areas[0]}" # correctness | regression | security | test_gap | architecture | simplicity | other
    component: "module or file path"
    claim: "Specific claim being made"
    location: "path/to/file.py:123"
    evidence: "Concrete snippet or proof demonstrating the failure"
    suggested_fix: "Concrete remediating code"
```
"""
        return brief


CORRECTNESS_PROMPT = """You are an independent Correctness Reviewer. Your role is strictly adversarial to buggy code.
Do NOT assume the code works just because it appears clean or passes basic tests.
Your primary objective is to produce a concrete input, state, or execution path where behavior violates requirements or logic:
1. Logic bugs, off-by-one errors, inverse condition checks, incorrect boolean logic.
2. Unhandled edge cases (None/null, empty collections, negative numbers, timeouts, unexpected types).
3. Mismatches where the code fails to strictly fulfill an acceptance criterion.
4. Broken error handling or swallowed exceptions that mask underlying failures.
5. Incomplete state transitions or unhandled error recovery states.
"""

REGRESSION_PROMPT = """You are an independent Regression Reviewer. Your job is to protect existing functionality from breakage.
Do NOT evaluate whether the new feature is nice; evaluate what previously valid behavior is now broken.
Search specifically for:
1. Breaking changes to function/method signatures, interfaces, or return types.
2. Deleted or renamed attributes/variables still referenced elsewhere in the repository.
3. Behavior changes for existing callers under default parameters or existing workflows.
4. Schema migrations, configuration drifts, or altered environment assumptions.
5. Inadvertently altered public contracts or library APIs.
"""

SECURITY_PROMPT = """You are an independent Security Reviewer. Your job is to identify security vulnerabilities, credential leaks, and unsafe patterns.
Attempt to identify trust-boundary violations, unsafe execution, secret exposure, authorization gaps, injection surfaces, or fail-open behavior:
1. Hardcoded secrets, API keys, credentials, PII, or internal tokens.
2. Unsafe command execution, unescaped shell interpolations (`shell=True`, bash backtick injection, subprocess without list args).
3. Missing input validation, path traversal (`../`), unconstrained deserialization, or unvalidated file writes.
4. Authorization flaws, bypasses of human approval gates, or fail-open authorization logic.
5. Insecure defaults or sensitive data leakage in logs or exceptions.
"""

TEST_FALSIFIER_PROMPT = """You are an independent Test Falsifier. Your mission is to prove that the tests are insufficient, illusory, or vacuous.
Do NOT celebrate green tests. Attempt to create a broken implementation that would still pass the current tests:
1. Tests that pass vacuously (e.g. asserting `True`, asserting on mocked objects without calling real code).
2. Mock paths that don't match reality (e.g. patching an object where it's defined rather than where it's imported, or over-mocking).
3. Missing negative test cases: error paths, timeouts, boundary inputs, malformed schemas, fail-closed assertions.
4. Code implementations that pass the test suite while violating the true task requirements.
"""

ARCHITECTURE_PROMPT = """You are an independent Architecture Reviewer. Your role is to maintain system integrity and prevent architectural erosion.
Identify boundary violations or coupling that materially raises maintenance or correctness risk:
1. Unnecessary coupling between unrelated modules or circular dependencies.
2. Leaky abstractions or single-caller abstractions that add indirection without value.
3. Violations of the repository's established layer boundaries (e.g. domain vs infrastructure vs orchestration).
4. Code that should live in a reusable module but was copy-pasted across multiple locations.
NOTE: Do NOT complain about style unless it materially affects architecture or correctness.
"""

SIMPLICITY_PROMPT = """You are an independent Simplicity Reviewer. Your mission is to eliminate needless complexity and minimize code volume.
Try to prove the solution contains unnecessary moving parts:
1. Overengineered solutions where a simple built-in or shorter function suffices.
2. Dead code, unused arguments, premature generalizations, or speculative features.
3. Opportunities to solve the same problem with significantly fewer lines of diff.
4. Redundant boilerplate that obscures business logic.
NOTE: Do NOT propose rewrites merely for elegance; focus on removing actual excess complexity.
"""

REVIEWER_ROLES: Dict[str, ReviewerRole] = {
    "correctness-reviewer": ReviewerRole(
        role_id="correctness-reviewer",
        name="Correctness Reviewer",
        focus_areas=["correctness", "edge_cases", "contract_compliance", "error_handling"],
        falsification_goal="Produce a concrete input or state where code violates requirements, fails on edge cases, or contains logic bugs.",
        prompt_template=CORRECTNESS_PROMPT,
    ),
    "regression-reviewer": ReviewerRole(
        role_id="regression-reviewer",
        name="Regression Reviewer",
        focus_areas=["regression", "backwards_compatibility", "call_sites", "contract_drift"],
        falsification_goal="Identify previously valid behavior, caller contracts, or configurations broken by this change.",
        prompt_template=REGRESSION_PROMPT,
    ),
    "security-reviewer": ReviewerRole(
        role_id="security-reviewer",
        name="Security Reviewer",
        focus_areas=["security", "vulnerabilities", "credentials", "injection", "authorization"],
        falsification_goal="Identify trust-boundary violations, unsafe execution, secret exposure, authorization gaps, or fail-open logic.",
        prompt_template=SECURITY_PROMPT,
    ),
    "test-falsifier": ReviewerRole(
        role_id="test-falsifier",
        name="Test Falsifier",
        focus_areas=["test_gap", "vacuous_tests", "mock_fidelity", "untested_branches"],
        falsification_goal="Prove the test suite is inadequate or construct a broken implementation that still passes tests.",
        prompt_template=TEST_FALSIFIER_PROMPT,
    ),
    "architecture-reviewer": ReviewerRole(
        role_id="architecture-reviewer",
        name="Architecture Reviewer",
        focus_areas=["architecture", "coupling", "abstraction_quality", "boundary_discipline"],
        falsification_goal="Identify structural coupling, leaky abstractions, and layering violations that materially raise risk.",
        prompt_template=ARCHITECTURE_PROMPT,
    ),
    "simplicity-reviewer": ReviewerRole(
        role_id="simplicity-reviewer",
        name="Simplicity Reviewer",
        focus_areas=["simplicity", "minimal_diff", "complexity_elimination", "dead_code"],
        falsification_goal="Prove the solution contains unnecessary moving parts and find smaller, simpler alternatives.",
        prompt_template=SIMPLICITY_PROMPT,
    ),
}


def get_reviewer_role(role_id: str) -> Optional[ReviewerRole]:
    """Retrieves a reviewer role definition by ID."""
    return REVIEWER_ROLES.get(role_id)


def list_reviewer_roles() -> List[ReviewerRole]:
    """Returns all available specialized reviewer roles."""
    return list(REVIEWER_ROLES.values())


def build_skill_context(task: TaskSpec) -> Optional[str]:
    """Constructs relevant domain guidance based on task required skills."""
    parts: List[str] = []
    if task.required_skills:
        parts.append(f"Required Skills: {', '.join(task.required_skills)}")
    if "howlframe-app-development" in (task.required_skills or []) or "howlframe" in (task.required_skills or []):
        parts.append(
            "HowlFrame Application Rules & Invariants:\n"
            "- Exactly one root form (`http_server`, `web_app`, `cli_app`, or `module`) per `.howl` file.\n"
            "- Standalone bytecode VM enforces finite instruction budgets and capability gates (`network`, `database`, `filesystem`).\n"
            "- Fail closed on invalid inputs, missing keys, or parse failures with `try_let`.\n"
            "- Deterministic verification requires `bash scripts/build.sh` and `bash scripts/test.sh` to pass."
        )
    return "\n\n".join(parts) if parts else None
