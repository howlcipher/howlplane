"""
test_howlforge_roles.py

Covers the vendored HowlForge role definitions and the capability derivation
they drive in `ProviderPoolManager._required_capabilities`.

Two properties carry the weight here:

* **Parity.** Adopting the vendored definitions changes where the answer comes
  from, not what the answer is. Every lifecycle role must derive exactly the
  capability set the previous hard-coded branches produced.
* **Fallback.** HowlForge is an optional sibling component. With the contracts
  directory absent, unreadable or malformed, derivation must return the previous
  result unchanged -- a missing sibling must never narrow what HowlPlane can
  select.
"""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from src.control_plane.howlforge_roles import (
    CAPABILITY_LEVELS,
    HOWLFORGE_ROLE_SCHEMA_VERSION,
    LIFECYCLE_BY_HOWLFORGE_ROLE,
    HowlForgeRole,
    capabilities_for_lifecycle_role,
    level_at_least,
    load_howlforge_roles,
)
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from src.control_plane.task_spec import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "contracts" / "howlforge"
ROLES_DIR = CONTRACT_DIR / "roles"


def _task(allowed_tools=None, metadata=None) -> TaskSpec:
    return TaskSpec(
        task_id="TASK-HF",
        repository="howlplane",
        objective="exercise role capability derivation",
        acceptance_criteria=["derivation is correct"],
        risk_level="low",
        allowed_tools=allowed_tools or [],
        metadata=metadata or {},
    )


def _legacy_required_capabilities(pool, task, role):
    """The derivation this change replaced, kept verbatim as the parity oracle.

    If this and the live implementation ever disagree for an existing role, the
    change has altered selection behavior rather than relocating its source.
    """
    required = set(task.metadata.get("required_resource_capabilities", []))
    if role in ("implementation", "remediation"):
        required.update({"file_editing", "repository_access"})
    elif pool._is_review_role(role):
        required.add("code_review")
    else:
        required.add("code_generation")
    if "terminal_execution" in task.allowed_tools:
        required.add("command_execution")
    return sorted(required)


# --------------------------------------------------------------------------
# The vendored contract itself
# --------------------------------------------------------------------------


def test_vendored_contract_is_present_and_documented():
    assert CONTRACT_DIR.is_dir(), "contracts/howlforge is missing"
    source = CONTRACT_DIR / "SOURCE.md"
    assert source.exists(), "vendored contracts must carry a SOURCE.md"
    text = source.read_text(encoding="utf-8")
    assert "Pinned commit:" in text, "SOURCE.md must pin an exact commit, never a moving ref"
    assert "howlcipher/howlforge" in text


def _resolved_role_schema():
    """Load the vendored role schema with its sibling $refs inlined.

    The vendored schemas reference each other by bare `$id` strings such as
    `howlforge.capability_level/v1`, which are identifiers rather than
    resolvable URIs. Rather than rewrite the vendored files -- they must stay
    byte-for-byte identical to the pinned upstream commit -- the two references
    are spliced into local `$defs` here, so the resolution quirk lives in the
    test and never in the contract.
    """
    schema = json.loads((CONTRACT_DIR / "howlforge.role.v1.schema.json").read_text(encoding="utf-8"))
    siblings = {
        "howlforge.capability_level/v1": json.loads(
            (CONTRACT_DIR / "howlforge.capability_level.v1.schema.json").read_text(encoding="utf-8")
        ),
        "howlforge.runtime_ref/v1": json.loads(
            (CONTRACT_DIR / "howlforge.runtime_ref.v1.schema.json").read_text(encoding="utf-8")
        ),
    }
    local_names = {
        "howlforge.capability_level/v1": "capability_level",
        "howlforge.runtime_ref/v1": "runtime_ref",
    }

    defs = schema.setdefault("$defs", {})
    for ref_id, document in siblings.items():
        inlined = {k: v for k, v in document.items() if k not in ("$schema", "$id")}
        defs[local_names[ref_id]] = inlined

    def rewrite(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref in local_names:
                node = dict(node)
                node["$ref"] = f"#/$defs/{local_names[ref]}"
            return {k: rewrite(v) for k, v in node.items()}
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        return node

    resolved = rewrite(schema)
    Draft202012Validator.check_schema(resolved)
    return resolved


def test_vendored_schemas_are_valid_draft_2020_12():
    for name in (
        "howlforge.role.v1.schema.json",
        "howlforge.capability_level.v1.schema.json",
        "howlforge.runtime_ref.v1.schema.json",
    ):
        document = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)


def test_every_vendored_role_validates_against_the_vendored_schema():
    """A re-vendor that breaks the contract must fail CI, not misroute silently."""
    validator = Draft202012Validator(_resolved_role_schema())

    files = sorted(ROLES_DIR.glob("*.yaml"))
    assert files, "no vendored role definitions found"
    for path in files:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        assert not errors, f"{path.name}: " + "; ".join(
            f"{list(e.path)}: {e.message}" for e in errors
        )


def test_the_vendored_schema_actually_rejects_a_bad_role():
    """Guards the guard: an always-passing validator would prove nothing."""
    validator = Draft202012Validator(_resolved_role_schema())
    bad = {
        "schema_version": "howlforge.role/v1",
        "id": "Bad Id With Spaces",
        "required_capabilities": {"coding": "extreme"},
    }
    assert list(validator.iter_errors(bad)), "the vendored schema accepted an invalid role"


def test_vendored_roles_load():
    roles = load_howlforge_roles()
    assert len(roles) >= 11, f"expected the full role set, loaded {sorted(roles)}"
    for expected in ("foreman", "architect", "implementer", "reviewer", "qa", "security"):
        assert expected in roles, f"{expected} is missing from the vendored definitions"
    implementer = roles["implementer"]
    assert implementer.requires("coding", "high")
    assert implementer.verifier_role == "reviewer"
    assert implementer.source_path.startswith("contracts/howlforge/roles/")


def test_every_mapped_role_exists_in_the_vendored_set():
    """The mapping must not point at a role the vendored data does not define."""
    roles = load_howlforge_roles()
    for howlforge_id in LIFECYCLE_BY_HOWLFORGE_ROLE:
        assert howlforge_id in roles, f"mapping references undefined role {howlforge_id!r}"


# --------------------------------------------------------------------------
# Parity: the answer must not change
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [
        "implementation",
        "remediation",
        "review",
        "planning",
        "synthesis",
        "verification",
        "security-reviewer",
        "correctness-reviewer",
        "test-falsifier",
        "some-unmapped-role",
    ],
)
@pytest.mark.parametrize("allowed_tools", [[], ["terminal_execution"]])
def test_derivation_matches_the_previous_behavior_exactly(role, allowed_tools):
    pool = ProviderPoolManager()
    task = _task(allowed_tools=allowed_tools)
    assert pool._required_capabilities(task, role) == _legacy_required_capabilities(
        pool, task, role
    ), f"capability derivation changed for role {role!r}"


def test_task_metadata_capabilities_are_still_unioned_in():
    pool = ProviderPoolManager()
    task = _task(metadata={"required_resource_capabilities": ["structured_output"]})
    assert "structured_output" in pool._required_capabilities(task, "implementation")


def test_command_execution_stays_task_scoped():
    """A role's tool_use aptitude must not imply command execution.

    command_execution is granted by the task's allowed_tools. Deriving it from a
    role would require it for tasks that never permitted it, narrowing selection
    on a permission the task did not ask for.
    """
    pool = ProviderPoolManager()
    assert "command_execution" not in pool._required_capabilities(_task(), "implementation")
    assert "command_execution" in pool._required_capabilities(
        _task(allowed_tools=["terminal_execution"]), "implementation"
    )


# --------------------------------------------------------------------------
# The ordering bug this derivation was written around
# --------------------------------------------------------------------------


def test_reviewer_never_requires_repository_write():
    """Review is tested before coding, and it must stay that way.

    HowlForge's reviewer role declares coding aptitude, because reading a diff
    well requires it. Deriving capabilities in the other order would demand
    file_editing and repository_access from every reviewer candidate and shrink
    the reviewer pool -- the independent-review collapse this repository has
    already been bitten by (issues.md #13).
    """
    derived = capabilities_for_lifecycle_role("review")
    assert derived == frozenset({"code_review"})
    assert "repository_access" not in derived
    assert "file_editing" not in derived


def test_implementer_requires_repository_write():
    assert capabilities_for_lifecycle_role("implementation") == frozenset(
        {"file_editing", "repository_access"}
    )
    assert capabilities_for_lifecycle_role("remediation") == frozenset(
        {"file_editing", "repository_access"}
    )


# --------------------------------------------------------------------------
# Fallback: a missing sibling must change nothing
# --------------------------------------------------------------------------


def test_missing_contract_directory_yields_no_definitions(tmp_path):
    assert load_howlforge_roles(tmp_path / "absent") == {}


def test_derivation_falls_back_when_no_definitions_exist():
    pool = ProviderPoolManager()
    task = _task()
    for role in ("implementation", "remediation", "review", "planning", "synthesis"):
        with_definitions = pool._required_capabilities(task, role)
        without = _legacy_required_capabilities(pool, task, role)
        assert with_definitions == without

    # And with the definitions explicitly empty, derivation declines rather than
    # returning an empty requirement set, which would make every provider match.
    assert capabilities_for_lifecycle_role("implementation", roles={}) is None


def test_unmapped_role_declines_rather_than_guessing():
    roles = load_howlforge_roles()
    assert capabilities_for_lifecycle_role("verification", roles=roles) is None
    assert capabilities_for_lifecycle_role("not-a-real-role", roles=roles) is None


@pytest.mark.parametrize(
    "content",
    [
        "not a mapping at all",
        "schema_version: howlforge.role/v99\nid: x\n",
        "id: x\n",  # no schema_version
        "schema_version: howlforge.role/v1\n",  # no id
        "schema_version: howlforge.role/v1\nid: x\nrequired_capabilities: [1, 2]\n",
        "{{{ not valid yaml",
    ],
)
def test_malformed_role_files_are_ignored_not_fatal(tmp_path, content):
    """One bad vendored file degrades that role, never the whole derivation."""
    (tmp_path / "broken.yaml").write_text(content, encoding="utf-8")
    (tmp_path / "good.yaml").write_text(
        "schema_version: howlforge.role/v1\nid: good\ndescription: fine\n"
        "required_capabilities:\n  coding: high\n",
        encoding="utf-8",
    )
    roles = load_howlforge_roles(tmp_path)
    assert "good" in roles, "a malformed sibling file suppressed a valid one"
    assert "x" not in roles


def test_oversized_role_file_is_ignored(tmp_path):
    (tmp_path / "huge.yaml").write_text("#" + "a" * (64 * 1024 + 10), encoding="utf-8")
    assert load_howlforge_roles(tmp_path) == {}


def test_unknown_capability_level_is_dropped_not_guessed(tmp_path):
    (tmp_path / "r.yaml").write_text(
        "schema_version: howlforge.role/v1\nid: r\ndescription: d\n"
        "required_capabilities:\n  coding: extreme\n  review: high\n",
        encoding="utf-8",
    )
    roles = load_howlforge_roles(tmp_path)
    assert roles["r"].required_capabilities == {"review": "high"}


# --------------------------------------------------------------------------
# Extensibility: a role defined only in data
# --------------------------------------------------------------------------


def test_fake_future_role_flows_through_without_code_change(tmp_path):
    """A role HowlPlane has never heard of must work through data alone.

    Mirrors the existing fake_future_provider tests, which prove the same
    property for providers.
    """
    (tmp_path / "future.yaml").write_text(
        "schema_version: howlforge.role/v1\n"
        "id: implementer\n"
        "description: A role defined only in vendored data.\n"
        "required_capabilities:\n"
        "  review: high\n",
        encoding="utf-8",
    )
    roles = load_howlforge_roles(tmp_path)
    assert "implementer" in roles
    # The vendored data alone flipped what implementation requires, with no Go
    # change, no Python change and no new branch.
    assert capabilities_for_lifecycle_role("implementation", roles=roles) == frozenset(
        {"code_review"}
    )


# --------------------------------------------------------------------------
# Boundary: what HowlPlane deliberately does not take
# --------------------------------------------------------------------------


def test_runtime_preferences_are_not_consumed():
    """HowlForge's preferred_runtimes must not reach HowlPlane's selection.

    select_resource ranks on live capacity, economics and egress policy, none of
    which HowlForge models. A static preference list must not override them.
    """
    assert not hasattr(HowlForgeRole, "preferred_runtimes")
    for role in load_howlforge_roles().values():
        assert not hasattr(role, "preferred_runtimes")
        assert not hasattr(role, "fallback_runtimes")


def test_no_subprocess_or_import_of_howlforge():
    """The adoption is data, not a dependency."""
    source = (REPO_ROOT / "src" / "control_plane" / "howlforge_roles.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("subprocess", "shutil.which", "import howlforge", "exec("):
        assert forbidden not in source, f"{forbidden} must not appear in the role loader"


# --------------------------------------------------------------------------
# The ordinal scale
# --------------------------------------------------------------------------


def test_capability_scale_is_ordinal():
    for i in range(1, len(CAPABILITY_LEVELS)):
        assert level_at_least(CAPABILITY_LEVELS[i], CAPABILITY_LEVELS[i - 1])
        assert not level_at_least(CAPABILITY_LEVELS[i - 1], CAPABILITY_LEVELS[i])


def test_absent_capability_reads_as_none():
    assert not level_at_least(None, "low")
    assert level_at_least(None, "none")


def test_schema_version_constant_matches_the_vendored_files():
    for path in sorted(ROLES_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == HOWLFORGE_ROLE_SCHEMA_VERSION
