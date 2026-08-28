#!/usr/bin/env python3
"""
project_adapter.py

Boundary adapter that interfaces between the control plane and target project repositories.
The control plane provides orchestration; the project supplies local truth.
"""

from dataclasses import dataclass, field, asdict
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Union
import tomllib
import yaml

from src.control_plane.hygiene_policy import HygienePolicyClassifier
from src.control_plane.verification import VerificationPlan


from src.control_plane.task_spec import DataClassSerializationMixin


@dataclass
class ProjectContext(DataClassSerializationMixin):
    """Represents discovered local truth for a specific project repository."""

    project_root: str
    name: str
    project_types: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    test_commands: List[List[str]] = field(default_factory=list)
    build_commands: List[List[str]] = field(default_factory=list)
    lint_commands: List[List[str]] = field(default_factory=list)
    format_commands: List[List[str]] = field(default_factory=list)
    hygiene_commands: List[List[str]] = field(default_factory=list)
    hygiene_status: str = "not_configured"
    capabilities: List[str] = field(default_factory=list)
    has_manifest: bool = False
    has_agents_md: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


IGNORED_SCAN_DIRS = {
    ".git",
    ".deps",
    "node_modules",
    "venv",
    ".venv",
    ".chroma",
    ".scratch_venv_test",
    "build",
    "dist",
    "__pycache__",
    ".slop",
    ".system_generated",
    ".telemetry",
}

ROOT_FORM_PATTERN = re.compile(
    r'^\s*\(\s*(cli_app|http_server|web_app|wasm_app|module)\b',
    re.MULTILINE,
)


def _find_howl_sources(root: Path) -> List[Path]:
    """Finds all .howl source files in root, pruning ignored directories."""
    sources: List[Path] = []
    try:
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in IGNORED_SCAN_DIRS and not d.startswith(".")]
            for file in files:
                if file.endswith(".howl"):
                    sources.append(Path(current_root) / file)
    except Exception:
        pass
    return sorted(sources)


def _extract_apparent_targets(howl_sources: List[Path]) -> List[str]:
    """Extracts root form target identifiers from discovered .howl source files."""
    targets = set()
    for src in howl_sources:
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
            match = ROOT_FORM_PATTERN.search(text)
            if match:
                targets.add(match.group(1))
        except Exception:
            pass
    return sorted(targets)


class ProjectAdapter:
    """
    Discovers project configuration and constructs project-specific verification plans
    while respecting local project sovereignty.
    """

    @classmethod
    def discover(cls, project_dir: Union[str, Path] = ".") -> ProjectContext:
        """
        Scans a directory for .ai-project.toml, project_manifest.yaml, .slop/ configuration,
        or project stack markers.
        """
        root = Path(project_dir).resolve()
        name = root.name
        project_types: List[str] = []
        skills: List[str] = []
        test_commands: List[List[str]] = []
        build_commands: List[List[str]] = []
        lint_commands: List[List[str]] = []
        format_commands: List[List[str]] = []
        hygiene_commands: List[List[str]] = []
        hygiene_status = "not_configured"
        capabilities: List[str] = []
        metadata: Dict[str, Any] = {}
        has_manifest = False
        has_agents_md = (root / "AGENTS.md").exists()

        def _extract_manifest_cmds(cmds: Dict[str, Any]) -> None:
            for k in ("test", "build", "lint", "format", "fmt"):
                if k in cmds:
                    val = cmds[k]
                    cmd_list = val if isinstance(val, list) else [val]
                    if k == "test":
                        test_commands.append(cmd_list)
                    elif k == "build":
                        build_commands.append(cmd_list)
                    elif k == "lint":
                        lint_commands.append(cmd_list)
                    else:
                        format_commands.append(cmd_list)
            for hk in ("hygiene", "repository_hygiene"):
                if hk in cmds:
                    hval = cmds[hk]
                    hygiene_commands.append(hval if isinstance(hval, list) else [hval])
                    break

        # 1. Check for .ai-project.toml
        ai_toml = root / ".ai-project.toml"
        if ai_toml.exists():
            has_manifest = True
            with open(ai_toml, "rb") as f:
                data = tomllib.load(f)
            name = data.get("name", name)
            project_types = data.get("project_type", [])
            skills = data.get("skills", [])
            _extract_manifest_cmds(data.get("commands", {}))
            sec = data.get("security", {})
            capabilities = sec.get("capabilities", [])
            metadata.update(data.get("metadata", {}))

        # 2. Check for project_manifest.yaml
        manifest_yaml = root / "project_manifest.yaml"
        if manifest_yaml.exists() and not has_manifest:
            has_manifest = True
            with open(manifest_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            name = data.get("name", name)
            project_types = data.get("project_types", [])
            skills = data.get("skills", [])
            _extract_manifest_cmds(data.get("commands", {}))
            capabilities = data.get("capabilities", [])
            metadata.update(data.get("metadata", {}))

        # 3. Discover SlopsLint repository hygiene configuration
        slop_config = root / ".slop" / "config.yml"
        slop_ceilings = root / ".slop" / "ceilings.yml"
        if slop_config.exists():
            if not slop_ceilings.exists():
                hygiene_status = "invalid_configuration"
            else:
                ok, _, pmeta = HygienePolicyClassifier.verify_provider_integrity("slopslint")
                if ok:
                    hygiene_status = "configured_and_passed"
                elif pmeta.get("status") == "version_mismatch":
                    hygiene_status = "invalid_provider_version"
                else:
                    hygiene_status = "configured_tool_missing"

            if not hygiene_commands:
                hygiene_commands.append(["slopslint", "check", "--classify", "--enforce"])

        # 4. Discover HowlFrame source files and metadata
        howl_sources = _find_howl_sources(root)
        if howl_sources:
            if "howlframe" not in project_types:
                project_types.append("howlframe")
            if "howlframe-app-development" not in skills:
                skills.append("howlframe-app-development")
            metadata["howl_sources"] = [str(p.relative_to(root)) for p in howl_sources]
            metadata["howl_source_count"] = len(howl_sources)
            apparent_targets = _extract_apparent_targets(howl_sources)
            if apparent_targets:
                metadata["apparent_targets"] = apparent_targets

        if "howlframe" in project_types and "howlframe-app-development" not in skills:
            skills.append("howlframe-app-development")

        # Check for HowlFrame bootstrap revision in scripts/bootstrap.sh
        bootstrap_sh = root / "scripts" / "bootstrap.sh"
        if bootstrap_sh.is_file():
            try:
                boot_txt = bootstrap_sh.read_text(encoding="utf-8", errors="ignore")
                rev_match = re.search(r'PINNED_HOWLFRAME_REV\s*=\s*["\']?([a-f0-9]+)["\']?', boot_txt)
                if rev_match:
                    metadata["howlframe_pinned_rev"] = rev_match.group(1)
            except Exception:
                pass

        # Check for nested Go module (e.g. tests/go.mod)
        tests_go_mod = root / "tests" / "go.mod"
        if tests_go_mod.is_file():
            metadata.setdefault("nested_modules", []).append({"type": "go", "path": "tests"})
            metadata["test_module"] = "tests/go.mod"

        # 5. Stack heuristics if commands are not explicitly specified
        if not test_commands or not build_commands or not lint_commands:
            # Check for Makefile
            makefile = root / "Makefile"
            if makefile.exists():
                text = makefile.read_text(encoding="utf-8", errors="ignore")
                if "test:" in text and not test_commands:
                    test_commands.append(["make", "test"])
                if "lint:" in text and not lint_commands:
                    lint_commands.append(["make", "lint"])
                if "build:" in text and not build_commands:
                    build_commands.append(["make", "build"])

            # Check for Go (root module)
            if (root / "go.mod").exists():
                if "go" not in project_types:
                    project_types.append("go")
                if not test_commands:
                    test_commands.append(["go", "test", "./..."])
                if not build_commands:
                    build_commands.append(["go", "build", "./..."])
                if not lint_commands:
                    lint_commands.append(["go", "vet", "./..."])

            # Check for Python
            if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "requirements.txt").exists():
                if "python" not in project_types:
                    project_types.append("python")
                if not test_commands:
                    test_commands.append(["pytest"])
                if not lint_commands:
                    lint_commands.append(["flake8"])

            # Check for Node / TS
            if (root / "package.json").exists():
                if "javascript" not in project_types:
                    project_types.append("javascript")
                if not test_commands:
                    test_commands.append(["npm", "test"])

            # Check for Rust
            if (root / "Cargo.toml").exists():
                if "rust" not in project_types:
                    project_types.append("rust")
                if not test_commands:
                    test_commands.append(["cargo", "test"])
                if not build_commands:
                    build_commands.append(["cargo", "build"])

            # Check for conventional script entrypoints in scripts/
            if not build_commands:
                if (root / "scripts" / "build.sh").is_file():
                    build_commands.append(["bash", "scripts/build.sh"])
                elif (root / "scripts" / "build").is_file():
                    build_commands.append(["bash", "scripts/build"])

            if not test_commands:
                if (root / "scripts" / "test.sh").is_file():
                    test_commands.append(["bash", "scripts/test.sh"])
                elif (root / "scripts" / "test").is_file():
                    test_commands.append(["bash", "scripts/test"])

            if not lint_commands:
                if (root / "scripts" / "lint.sh").is_file():
                    lint_commands.append(["bash", "scripts/lint.sh"])
                elif (root / "scripts" / "lint").is_file():
                    lint_commands.append(["bash", "scripts/lint"])

            # Check for nested test module fallback if test_commands still empty
            if not test_commands and tests_go_mod.is_file():
                test_commands.append(["bash", "-c", "cd tests && go test ./..."])

            # Check for standalone shell test suites
            tests_dir = root / "tests"
            if tests_dir.is_dir() and not test_commands:
                sh_tests = sorted(tests_dir.glob("*.sh"))
                for sh_t in sh_tests:
                    rel_p = str(sh_t.relative_to(root))
                    test_commands.append(["bash", rel_p])

        # 6. Formatting commands.
        #
        # Deliberately discovered outside the block above, which only runs when
        # a test/build/lint command is still missing: a project that specifies
        # all three would otherwise never be offered a formatter at all.
        #
        # Formatting is part of ordinary implementation work rather than a
        # deterministic gate, so these commands are kept separate from the
        # verification surface. They are what an implementer may legitimately
        # run; they are not added to the VerificationPlan and do not change what
        # a change is verified against (HOWLFRAM-SLOPFIX-07, where a Claude
        # implementation attempt was blocked on `gofmt -l` and `go fmt` because
        # no formatting command was discoverable for any language).
        if not format_commands:
            makefile = root / "Makefile"
            if makefile.exists():
                text = makefile.read_text(encoding="utf-8", errors="ignore")
                for target in ("fmt", "format"):
                    if f"{target}:" in text:
                        format_commands.append(["make", target])
                        break

        # Language defaults are added even when a Makefile target was found.
        # A wrapper target is what the project prefers, not the only formatter
        # an implementer will reach for: an agent told to format Go code runs
        # `gofmt`, and granting only `make format` leaves it blocked exactly as
        # in HOWLFRAM-SLOPFIX-07. Both are legitimate and neither widens the
        # bound beyond formatting.
        if not any(cmd[:1] != ["make"] for cmd in format_commands):
            if (root / "go.mod").exists():
                # Both forms: `go fmt` rewrites, `gofmt -l` reports. A mutating
                # role already holds Edit/Write, so the rewriting form grants no
                # new kind of authority, and a provider denied one of the two
                # reports itself blocked exactly as SLOPFIX-07 did.
                format_commands.append(["go", "fmt", "./..."])
                format_commands.append(["gofmt", "-l", "."])
            elif (root / "pyproject.toml").exists():
                pyproject = (root / "pyproject.toml").read_text(
                    encoding="utf-8", errors="ignore"
                )
                # Only grant a formatter the project actually configures.
                if "ruff" in pyproject:
                    format_commands.append(["ruff", "format", "."])
                elif "black" in pyproject:
                    format_commands.append(["black", "."])
            elif (root / "Cargo.toml").exists():
                format_commands.append(["cargo", "fmt"])

        return ProjectContext(
            project_root=str(root),
            name=name,
            project_types=project_types,
            skills=skills,
            test_commands=test_commands,
            build_commands=build_commands,
            lint_commands=lint_commands,
            format_commands=format_commands,
            hygiene_commands=hygiene_commands,
            hygiene_status=hygiene_status,
            capabilities=capabilities,
            has_manifest=has_manifest,
            has_agents_md=has_agents_md,
            metadata=metadata,
        )

    @classmethod
    def create_verification_plan(cls, context: ProjectContext, task_id: str) -> VerificationPlan:
        """
        Creates a VerificationPlan tailored to the project's discovered commands.
        """
        plan = VerificationPlan(task_id=task_id)
        idx = 1

        for cmd in context.lint_commands:
            plan.add_step(
                step_id=f"step-{idx:02d}",
                name=f"Lint check ({' '.join(cmd)})",
                command=cmd,
                category="lint",
                required=True,
            )
            idx += 1

        for cmd in context.build_commands:
            plan.add_step(
                step_id=f"step-{idx:02d}",
                name=f"Build check ({' '.join(cmd)})",
                command=cmd,
                category="build",
                required=True,
            )
            idx += 1

        for cmd in context.test_commands:
            plan.add_step(
                step_id=f"step-{idx:02d}",
                name=f"Automated test suite ({' '.join(cmd)})",
                command=cmd,
                category="unit_test",
                required=True,
            )
            idx += 1

        for cmd in context.hygiene_commands:
            plan.add_step(
                step_id=f"step-{idx:02d}",
                name=f"Repository hygiene gate ({' '.join(cmd)})",
                command=cmd,
                category="repository_hygiene",
                required=True,
            )
            idx += 1

        return plan
