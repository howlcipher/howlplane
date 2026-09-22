"""Role definitions vendored from HowlForge.

HowlForge (https://github.com/howlcipher/howlforge) is the workforce definition
layer for HowlFutureWorks: it states what each role requires, independently of
which model happens to fill it. HowlPlane reads those statements here instead of
deriving them from hard-coded branches.

Three boundaries are deliberate and load bearing:

* **Data, not a dependency.** The definitions are vendored YAML pinned to a
  commit (see ``contracts/howlforge/SOURCE.md``). Nothing here imports or
  executes HowlForge. No Go toolchain, no binary, no network.
* **Definitions only.** HowlForge's ``preferred_runtimes`` ordering is read and
  discarded. ``ProviderPoolManager.select_resource`` ranks on live capacity,
  economics and egress policy, none of which HowlForge models, and a static
  preference list must not override them.
* **Fallback is the floor.** If the vendored directory is missing, unreadable or
  malformed, capability derivation returns ``None`` and the caller keeps its
  previous behavior unchanged. A sibling component being absent must never
  narrow what HowlPlane can select.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

HOWLFORGE_ROLE_SCHEMA_VERSION = "howlforge.role/v1"

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "howlforge"
ROLES_DIR = CONTRACT_DIR / "roles"

# A role document is a few hundred bytes. This cap exists because vendored files
# are still untrusted input, not because any legitimate file approaches it.
MAX_ROLE_FILE_BYTES = 64 * 1024
MAX_ROLE_FILES = 256

# The ordinal capability scale, ascending. Mirrors
# contracts/howlforge/howlforge.capability_level.v1.schema.json.
CAPABILITY_LEVELS: Tuple[str, ...] = ("none", "low", "medium", "high", "very_high")
_LEVEL_ORDINAL: Dict[str, int] = {name: i for i, name in enumerate(CAPABILITY_LEVELS)}


def level_at_least(declared: Optional[str], required: str) -> bool:
    """Ordinal comparison over the capability scale.

    An undeclared capability reads as ``none``, so it fails every real
    requirement without the caller special casing absence.
    """
    return _LEVEL_ORDINAL.get(declared or "none", 0) >= _LEVEL_ORDINAL[required]


# HowlForge role ids are not HowlPlane lifecycle roles, and they must not be
# adopted verbatim: select_resource excludes a candidate with ROLE_NOT_SUPPORTED
# unless the requested role is an exact member of profile.roles, so introducing
# "implementer" as a role name would make every provider ineligible for it.
#
# Roles absent from this mapping (qa, security, devops, sre, auditor, researcher)
# are still loaded and available as definitions; they simply do not drive
# lifecycle capability derivation yet. Adding one is a mapping entry, not code.
LIFECYCLE_BY_HOWLFORGE_ROLE: Dict[str, Tuple[str, ...]] = {
    "implementer": ("implementation", "remediation"),
    "reviewer": ("review",),
    "architect": ("planning",),
    "foreman": ("synthesis",),
}


@dataclass(frozen=True)
class HowlForgeRole:
    """One vendored role definition, reduced to what HowlPlane uses."""

    role_id: str
    description: str = ""
    required_capabilities: Dict[str, str] = field(default_factory=dict)
    verifier_role: Optional[str] = None
    independent_runtime: Optional[str] = None
    source_path: str = ""

    def requires(self, capability: str, level: str) -> bool:
        """Whether this role demands at least ``level`` of ``capability``."""
        return level_at_least(self.required_capabilities.get(capability), level)


def _parse_role(path: Path) -> Optional[HowlForgeRole]:
    """Parse one vendored role file, or return None if it is unusable.

    Returning None rather than raising is deliberate: one malformed vendored file
    must degrade that role to the previous hard-coded behavior, not take down
    role derivation for every other role.
    """
    try:
        if path.stat().st_size > MAX_ROLE_FILE_BYTES:
            logger.warning("howlforge role %s exceeds the size cap; ignoring", path.name)
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("howlforge role %s is unreadable (%s); ignoring", path.name, exc)
        return None

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("howlforge role %s is not valid YAML (%s); ignoring", path.name, exc)
        return None

    if not isinstance(document, dict):
        logger.warning("howlforge role %s is not a mapping; ignoring", path.name)
        return None
    if document.get("schema_version") != HOWLFORGE_ROLE_SCHEMA_VERSION:
        logger.warning(
            "howlforge role %s declares schema_version %r, expected %r; ignoring",
            path.name,
            document.get("schema_version"),
            HOWLFORGE_ROLE_SCHEMA_VERSION,
        )
        return None

    role_id = document.get("id")
    if not isinstance(role_id, str) or not role_id:
        logger.warning("howlforge role %s has no usable id; ignoring", path.name)
        return None

    declared = document.get("required_capabilities") or {}
    if not isinstance(declared, dict):
        logger.warning("howlforge role %s has a malformed required_capabilities; ignoring", path.name)
        return None
    capabilities: Dict[str, str] = {}
    for name, level in declared.items():
        if not isinstance(name, str) or not isinstance(level, str):
            continue
        if level not in _LEVEL_ORDINAL:
            logger.warning(
                "howlforge role %s declares unknown level %r for %r; ignoring that capability",
                path.name,
                level,
                name,
            )
            continue
        capabilities[name] = level

    verification = document.get("verification") or {}
    if not isinstance(verification, dict):
        verification = {}

    return HowlForgeRole(
        role_id=role_id,
        description=document.get("description") or "",
        required_capabilities=capabilities,
        verifier_role=verification.get("verifier_role") or None,
        independent_runtime=verification.get("independent_runtime") or None,
        source_path=f"contracts/howlforge/roles/{path.name}",
    )


def load_howlforge_roles(roles_dir: Optional[Path] = None) -> Dict[str, HowlForgeRole]:
    """Load every vendored role definition, keyed by HowlForge role id.

    A missing directory yields an empty mapping rather than an error: HowlForge
    is an optional sibling component, and its absence must leave HowlPlane
    working exactly as before.
    """
    directory = roles_dir or ROLES_DIR
    try:
        if not directory.is_dir():
            return {}
        entries = sorted(p for p in directory.iterdir() if p.suffix in (".yaml", ".yml"))
    except OSError as exc:
        logger.warning("howlforge role directory %s is unreadable (%s)", directory, exc)
        return {}

    if len(entries) > MAX_ROLE_FILES:
        logger.warning("howlforge role directory holds %d files, over the cap", len(entries))
        return {}

    roles: Dict[str, HowlForgeRole] = {}
    for path in entries:
        parsed = _parse_role(path)
        if parsed is None:
            continue
        if parsed.role_id in roles:
            logger.warning("duplicate howlforge role id %r; keeping the first", parsed.role_id)
            continue
        roles[parsed.role_id] = parsed
    return roles


# Derivation from HowlForge's aptitude vocabulary to HowlPlane's skill
# capability strings. The mapping is intentionally narrow: it reproduces the
# capability sets the hard-coded derivation already produced, so adopting these
# definitions changes where the answer comes from without changing the answer.
# Widening it narrows provider eligibility, so each addition needs its own
# evidence.
def _derive_capabilities(role: HowlForgeRole) -> FrozenSet[str]:
    """Project a role's declared aptitudes onto HowlPlane capability strings."""
    # Review is tested first, and the order is the whole point. HowlForge's
    # reviewer role declares coding aptitude too, because reading a diff well
    # requires it -- but a reviewer must not be required to hold repository
    # write capability. Testing coding first would hand reviewers
    # file_editing and repository_access and narrow reviewer eligibility,
    # which is exactly the independent-review collapse this repository has
    # already been bitten by.
    if role.requires("review", "high"):
        return frozenset({"code_review"})
    # Strong coding with no review duty implies the role mutates a repository.
    if role.requires("coding", "high"):
        return frozenset({"file_editing", "repository_access"})
    return frozenset({"code_generation"})


_CACHE: Optional[Dict[str, HowlForgeRole]] = None


def cached_howlforge_roles() -> Dict[str, HowlForgeRole]:
    """Load once per process.

    Capability derivation runs on every selection, and the vendored files are
    pinned data that cannot change under a running process, so re-reading them
    per call would buy nothing. Tests reset the cache with ``reset_cache``.
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = load_howlforge_roles()
    return _CACHE


def reset_cache() -> None:
    """Drop the process cache. For tests."""
    global _CACHE
    _CACHE = None


def capabilities_for_lifecycle_role(
    lifecycle_role: str,
    roles: Optional[Dict[str, HowlForgeRole]] = None,
) -> Optional[FrozenSet[str]]:
    """Required capabilities for a HowlPlane lifecycle role, or None.

    None means "no vendored definition covers this role", and the caller must
    keep its previous behavior. It is never an error.
    """
    available = cached_howlforge_roles() if roles is None else roles
    if not available:
        return None
    for howlforge_id, lifecycle_roles in LIFECYCLE_BY_HOWLFORGE_ROLE.items():
        if lifecycle_role not in lifecycle_roles:
            continue
        definition = available.get(howlforge_id)
        if definition is None:
            continue
        return _derive_capabilities(definition)
    return None
