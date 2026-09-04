#!/usr/bin/env python3
"""Select a conservative, explainable pytest subset for a Git change set.

The selector runs direct test counterparts, changed tests, tests that name an
affected module, and tests that name any first-party module transitively
importing it. Evidence is static text, not execution: a test reaching a module
without naming it or any of its importers is not detected. Shared test
infrastructure, central control-plane seams, and changes with no evidence at
all fall back to the full Python suite rather than risking a false negative.
"""

import argparse
import ast
import functools
import re
import subprocess  # nosec B404 - fixed Git and Python commands below.
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# These files affect test execution, packaging, all generated behavior, or a
# system-wide authority/orchestration seam. A subset would be misleading.
FULL_SUITE_PATHS = {"Makefile", "pyproject.toml", "tests/conftest.py"}
FULL_SUITE_PREFIXES = (".github/workflows/", "config/")
CENTRAL_CONTROL_PLANE_MODULES = {
    "src/control_plane/agent_registry.py",
    "src/control_plane/authority_envelope.py",
    "src/control_plane/durable_store.py",
    "src/control_plane/human_boundary.py",
    "src/control_plane/launcher.py",
    "src/control_plane/orchestrator.py",
    "src/control_plane/task_spec.py",
    "src/core/config.py",
}
DOCUMENTATION_PREFIXES = ("docs/", "documentation/")
DOCUMENTATION_FILES = {"README.md", "CHANGELOG.md", "change_log.md", "LICENSE"}
AGENT_POLICY_PREFIXES = (".agents/", ".claude/skills/", ".devin/skills/", ".gemini/")
AGENT_POLICY_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "DEVIN.md"}
AGENT_POLICY_TESTS = (
    "tests/test_rules_and_prompts.py",
    "tests/test_skills_index.py",
    "tests/test_generate_skills_manifest.py",
)


@dataclass(frozen=True)
class Selection:
    """Selected tests and the evidence for a conservative decision."""

    selected: dict[str, list[str]]
    uncovered: list[str]
    fallback_reason: Optional[str]


def run_git(*args: str) -> str:
    """Run a fixed-argument Git command at the repository root."""
    result = subprocess.run(  # nosec B603 B607 - fixed executable and argv.
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout


def changed_files_from_name_status(output: str) -> list[str]:
    """Expand Git name-status output, retaining both sides of a rename."""
    paths: set[str] = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            paths.update(fields[1:3])
        elif len(fields) >= 2:
            paths.add(fields[1])
    return sorted(paths)


def changed_files(base: Optional[str] = None) -> list[str]:
    """Return changed repo-relative paths, including untracked local files."""
    if base:
        return changed_files_from_name_status(
            run_git("diff", "--name-status", "--find-renames", f"{base}...HEAD")
        )

    paths = set(changed_files_from_name_status(run_git("diff", "--name-status", "HEAD")))
    paths.update(run_git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(paths)


def module_names(path: str) -> set[str]:
    """Return importable and stem names a test can use for a Python file."""
    parts = list(Path(path).with_suffix("").parts)
    if not parts:
        return set()
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()

    names = {parts[-1], ".".join(parts)}
    if parts[0] in {"src", "scripts"} and len(parts) > 1:
        names.add(".".join(parts[1:]))
    return names


def direct_test_candidates(path: str, test_sources: dict[str, str]) -> set[str]:
    """Find conventional ``test_<module>.py`` counterparts for a source file."""
    source = Path(path)
    if source.suffix != ".py" or source.name == "__init__.py":
        return set()
    candidate_name = f"test_{source.stem}.py"
    return {test for test in test_sources if Path(test).name == candidate_name}


# A dotted identifier chain, captured from its start so a name preceded by a
# word character or a dot is never mistaken for a standalone reference.
_DOTTED_TOKEN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


@functools.lru_cache(maxsize=1024)
def _parsed_test_metadata(
    test_source: str,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Extract import targets, string literals, and textual name prefixes.

    The prefix set is what makes matching a set intersection rather than a
    regex scan per candidate name. With transitive dependents the candidate
    list is large enough that the difference is seconds per invocation.
    """
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        tree = None

    imports: set[str] = set()
    string_literals: set[str] = set()
    for node in ast.walk(tree) if tree is not None else ():
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals.add(node.value)

    prefixes: set[str] = set()
    for token in _DOTTED_TOKEN.findall(test_source):
        parts = token.split(".")
        for end in range(1, len(parts) + 1):
            prefixes.add(".".join(parts[:end]))
    return frozenset(imports), frozenset(string_literals), frozenset(prefixes)


def imported_modules(test_source: str) -> set[str]:
    """Extract static import targets without malformed tests breaking selection."""
    imports, _, _ = _parsed_test_metadata(test_source)
    return set(imports)


def test_exercises_module(test_source: str, names: set[str]) -> Optional[str]:
    """Return the strongest static, string-patch, or textual module evidence in a test source."""
    imports, string_literals, prefixes = _parsed_test_metadata(test_source)
    ordered_names = sorted(names, key=len, reverse=True)
    for name in ordered_names:
        if any(
            imported == name
            or imported.startswith(f"{name}.")
            for imported in imports
        ):
            return name

    # Dotted string literals (e.g. mock patches like @patch("src.core.mcp_client.SyncMCPClient"))
    for name in ordered_names:
        if any(
            literal == name
            or literal.startswith(f"{name}.")
            for literal in string_literals
        ):
            return name

    # Behavioral tests may patch a module by string or construct imports in a
    # fixture. Retain textual evidence as a conservative supplement.
    for name in ordered_names:
        if name in prefixes:
            return name
    return None


SOURCE_ROOTS = ("src", "scripts")


def _importers_of(dependent_index: dict[str, set[str]], names: set[str]) -> set[str]:
    """Return source paths whose imports resolve to any of ``names``."""
    importers: set[str] = set()
    for imported, paths in dependent_index.items():
        if any(imported == name or imported.startswith(f"{name}.") for name in names):
            importers.update(paths)
    return importers


def build_dependent_index(source_modules: dict[str, str]) -> dict[str, set[str]]:
    """Map each imported module name to the first-party sources importing it."""
    index: dict[str, set[str]] = {}
    for path, source in source_modules.items():
        for imported in imported_modules(source):
            index.setdefault(imported, set()).add(path)
    return index


def transitive_dependents(path: str, dependent_index: dict[str, set[str]]) -> list[str]:
    """Return first-party sources that reach ``path`` through any import chain.

    A test frequently exercises a module only through an intermediate import
    and never names it. Matching test text against the changed module alone
    silently drops those tests, so the changed module's importers -- and their
    importers, transitively -- are matched too.
    """
    seen = {path}
    frontier = [path]
    dependents: list[str] = []
    while frontier:
        current = frontier.pop()
        for importer in sorted(_importers_of(dependent_index, module_names(current))):
            if importer in seen:
                continue
            seen.add(importer)
            dependents.append(importer)
            frontier.append(importer)
    return dependents


IGNORED_PATHS = {".coverage", "coverage.out"}
IGNORED_PREFIXES = (".coverage.",)


def is_ignored_path(path: str) -> bool:
    """Whether a path is a generated test coverage or cache artifact."""
    return path in IGNORED_PATHS or path.startswith(IGNORED_PREFIXES)


def is_documentation_only(path: str) -> bool:
    """Whether a path cannot change executable validation behavior."""
    return path.endswith(".md") or path.startswith(DOCUMENTATION_PREFIXES) or path in DOCUMENTATION_FILES


def is_agent_policy(path: str) -> bool:
    """Whether a path requires policy/index validation rather than app tests."""
    return path in AGENT_POLICY_FILES or path.startswith(AGENT_POLICY_PREFIXES)


def full_suite_trigger(path: str) -> bool:
    """Whether a path invalidates the safety of targeted Python selection."""
    return (
        path in FULL_SUITE_PATHS
        or path in CENTRAL_CONTROL_PLANE_MODULES
        or path.endswith(".go")
        or path in {"go.mod", "go.sum"}
        or path.startswith(FULL_SUITE_PREFIXES)
    )


def _add_reason(selected: dict[str, list[str]], test: str, reason: str) -> None:
    reasons = selected.setdefault(test, [])
    if reason not in reasons:
        reasons.append(reason)


def tests_naming_path(path: str, test_sources: dict[str, str]) -> set[str]:
    """Return tests whose source names a non-Python path or its file name."""
    name = Path(path).name
    return {
        test
        for test, source in test_sources.items()
        if path in source or (name != path and name in source)
    }


def select_tests_with_reasons(
    changed: list[str],
    test_sources: dict[str, str],
    source_modules: Optional[dict[str, str]] = None,
) -> Selection:
    """Select tests with auditable reasons and a fail-conservative fallback."""
    selected: dict[str, list[str]] = {}
    uncovered: list[str] = []
    structural_trigger: Optional[str] = None
    uncertain_trigger: Optional[str] = None
    dependent_index = build_dependent_index(source_modules or {})

    for path in sorted(set(changed)):
        if is_ignored_path(path):
            continue
        if full_suite_trigger(path):
            structural_trigger = structural_trigger or path
            continue
        if is_agent_policy(path):
            for test in AGENT_POLICY_TESTS:
                if test in test_sources:
                    _add_reason(selected, test, f"agent or skill policy change: {path}")
            continue
        if is_documentation_only(path):
            continue
        if path.startswith("tests/") and path.endswith(".py"):
            if path in test_sources:
                _add_reason(selected, path, "changed test file")
            else:
                uncovered.append(path)
                uncertain_trigger = uncertain_trigger or f"uncertain impact: {path}"
            continue
        if not path.endswith(".py"):
            # A non-Python file is not automatically inert: .gitignore, shell
            # scripts, and docs assets all have executable contracts here. Run
            # the tests that name it, and fall back only when none does.
            naming = tests_naming_path(path, test_sources)
            for test in sorted(naming):
                _add_reason(selected, test, f"references {path}")
            if not naming:
                uncovered.append(path)
                uncertain_trigger = uncertain_trigger or f"uncertain impact: {path}"
            continue

        names = module_names(path)
        matches: set[str] = set()
        for test in direct_test_candidates(path, test_sources):
            _add_reason(selected, test, f"direct module mapping for {path}")
            matches.add(test)
        for test, test_source in test_sources.items():
            evidence = test_exercises_module(test_source, names)
            if evidence:
                _add_reason(selected, test, f"imports or exercises {evidence}")
                matches.add(test)
        for dependent in transitive_dependents(path, dependent_index):
            dependent_names = module_names(dependent)
            for test in direct_test_candidates(dependent, test_sources):
                _add_reason(selected, test, f"direct module mapping for {dependent}, which imports {path}")
                matches.add(test)
            for test, test_source in test_sources.items():
                evidence = test_exercises_module(test_source, dependent_names)
                if evidence:
                    _add_reason(selected, test, f"imports or exercises {evidence}, which imports {path}")
                    matches.add(test)
        if not matches:
            uncovered.append(path)
            uncertain_trigger = uncertain_trigger or f"uncertain impact: {path}"

    fallback_reason = structural_trigger or uncertain_trigger
    return Selection(
        selected={test: selected[test] for test in sorted(selected)},
        uncovered=sorted(uncovered),
        fallback_reason=fallback_reason,
    )


def select_tests(
    changed: list[str],
    test_sources: dict[str, str],
    source_modules: Optional[dict[str, str]] = None,
) -> tuple[list[str], list[str], Optional[str]]:
    """Compatibility wrapper for callers needing paths, gaps, and fallback."""
    selection = select_tests_with_reasons(changed, test_sources, source_modules)
    return list(selection.selected), selection.uncovered, selection.fallback_reason


def load_test_sources() -> dict[str, str]:
    """Load every collected-style Python test, including nested packages."""
    return {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(TESTS_DIR.rglob("test_*.py"))
    }


def load_source_modules() -> dict[str, str]:
    """Load first-party Python sources whose imports form the dependency graph."""
    sources: dict[str, str] = {}
    for root_name in SOURCE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            sources[str(path.relative_to(REPO_ROOT))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
    return sources


def print_selection(changed: list[str], selection: Selection) -> None:
    """Print a stable, human-readable explanation of the selection decision."""
    print("Changed files:")
    for path in changed:
        print(f"  {path}")

    # Printed before the fallback branch returns: every uncovered path also
    # sets the uncertain trigger, so reporting it only in the non-fallback
    # branch made this list unreachable.
    if selection.uncovered:
        print("Uncovered or uncertain changed paths:")
        for path in selection.uncovered:
            print(f"  {path}")

    if selection.fallback_reason:
        print(f"Conservative fallback: yes ({selection.fallback_reason})")
        print("Selected tests: tests/")
        return

    print("Conservative fallback: no")
    if not selection.selected:
        print("Selected tests: none")
    else:
        print("Selected tests:")
        for test, reasons in selection.selected.items():
            print(f"  {test}")
            for reason in reasons:
                print(f"    because: {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-ref", "--base", dest="base", help="diff against this Git ref instead of the working tree"
    )
    parser.add_argument(
        "--dry-run", "--list", dest="dry_run", action="store_true", help="print selection without running tests"
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero when a source has no targeted mapping"
    )
    args = parser.parse_args()

    changed = changed_files(args.base)
    if not changed:
        print("Changed files: none")
        print("Conservative fallback: no")
        print("Selected tests: none")
        return 0

    selection = select_tests_with_reasons(
        changed, load_test_sources(), load_source_modules()
    )
    print_selection(changed, selection)
    if args.dry_run:
        return 1 if args.strict and selection.uncovered else 0

    pytest_args = ["tests/"] if selection.fallback_reason else list(selection.selected)
    if not pytest_args:
        return 1 if args.strict and selection.uncovered else 0
    rc = subprocess.call(  # nosec B603 - executable and paths are controlled here.
        [sys.executable, "-m", "pytest", "-v", *pytest_args], cwd=REPO_ROOT
    )
    if rc == 0 and args.strict and selection.uncovered:
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
