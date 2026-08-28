"""
test_project_adapter.py

Unit and regression tests for ProjectAdapter discovery, HowlFrame stack recognition,
script convention resolution, nested module awareness, and sovereign command overrides.
"""

from pathlib import Path
import pytest

from src.control_plane.project_adapter import ProjectAdapter, ProjectContext


def test_synthetic_howlframe_repo_discovery(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "backend.howl").write_text(
        '(http_server 8080\n  (route "/" (lambda (req) (print "ok"))))\n',
        encoding="utf-8",
    )
    (app_dir / "frontend.howl").write_text(
        '(web_app\n  (defun render () (print "ui")))\n',
        encoding="utf-8",
    )

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.name == tmp_path.name
    assert "howlframe" in ctx.project_types
    assert ctx.metadata["howl_source_count"] == 2
    assert sorted(ctx.metadata["howl_sources"]) == ["app/backend.howl", "app/frontend.howl"]
    assert sorted(ctx.metadata["apparent_targets"]) == ["http_server", "web_app"]
    assert ctx.test_commands == []
    assert ctx.build_commands == []


def test_howlframe_with_scripts_and_pinned_revision(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "cli.howl").write_text('(cli_app (print "hello"))\n', encoding="utf-8")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "build.sh").write_text("#!/bin/bash\necho build\n", encoding="utf-8")
    (scripts_dir / "test.sh").write_text("#!/bin/bash\necho test\n", encoding="utf-8")
    (scripts_dir / "lint.sh").write_text("#!/bin/bash\necho lint\n", encoding="utf-8")
    (scripts_dir / "bootstrap.sh").write_text(
        '#!/bin/bash\nPINNED_HOWLFRAME_REV="abcdef1234567890"\n',
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "go.mod").write_text("module example.com/tests\n\ngo 1.22\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.project_types == ["howlframe"]
    assert ctx.metadata["howlframe_pinned_rev"] == "abcdef1234567890"
    assert ctx.metadata["test_module"] == "tests/go.mod"
    assert ctx.metadata["nested_modules"] == [{"type": "go", "path": "tests"}]
    assert ctx.build_commands == [["bash", "scripts/build.sh"]]
    assert ctx.test_commands == [["bash", "scripts/test.sh"]]
    assert ctx.lint_commands == [["bash", "scripts/lint.sh"]]

    plan = ProjectAdapter.create_verification_plan(ctx, "TASK-HN")
    assert len(plan.steps) == 3
    assert plan.steps[0].category == "lint"
    assert plan.steps[0].command == ["bash", "scripts/lint.sh"]
    assert plan.steps[1].category == "build"
    assert plan.steps[1].command == ["bash", "scripts/build.sh"]
    assert plan.steps[2].category == "unit_test"
    assert plan.steps[2].command == ["bash", "scripts/test.sh"]


def test_explicit_manifest_overrides_heuristics_and_scripts(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.howl").write_text('(cli_app (print "run"))\n', encoding="utf-8")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "build.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (scripts_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

    (tmp_path / ".ai-project.toml").write_text(
        """
name = "sovereign-app"
project_type = ["howlframe", "custom"]
skills = ["software_development"]

[commands]
build = ["custom-builder", "--release"]
test = ["custom-runner", "--all"]
lint = ["custom-linter"]
""",
        encoding="utf-8",
    )

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.name == "sovereign-app"
    assert ctx.has_manifest is True
    assert ctx.project_types == ["howlframe", "custom"]
    assert ctx.build_commands == [["custom-builder", "--release"]]
    assert ctx.test_commands == [["custom-runner", "--all"]]
    assert ctx.lint_commands == [["custom-linter"]]


def test_unrelated_arbitrary_shell_scripts_not_executed_as_commands(tmp_path: Path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "deploy.sh").write_text("#!/bin/bash\necho deploy\n", encoding="utf-8")
    (scripts_dir / "clean_up.sh").write_text("#!/bin/bash\necho cleanup\n", encoding="utf-8")
    (scripts_dir / "setup_infra.sh").write_text("#!/bin/bash\necho infra\n", encoding="utf-8")

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "helper.sh").write_text("#!/bin/bash\necho helper\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.build_commands == []
    assert ctx.test_commands == []
    assert ctx.lint_commands == []
    assert ctx.project_types == []


def test_nested_go_module_without_scripts_falls_back_to_cd(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "go.mod").write_text("module tests\n\ngo 1.22\n", encoding="utf-8")
    (tests_dir / "suite_test.go").write_text("package main\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.test_commands == [["bash", "-c", "cd tests && go test ./..."]]
    assert ctx.metadata["test_module"] == "tests/go.mod"


def test_standard_stacks_discovery(tmp_path: Path):
    # Python
    py_dir = tmp_path / "py_proj"
    py_dir.mkdir()
    (py_dir / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    ctx_py = ProjectAdapter.discover(py_dir)
    assert "python" in ctx_py.project_types
    assert ctx_py.test_commands == [["pytest"]]
    assert ctx_py.lint_commands == [["flake8"]]

    # Go
    go_dir = tmp_path / "go_proj"
    go_dir.mkdir()
    (go_dir / "go.mod").write_text("module mypkg\n\ngo 1.22\n", encoding="utf-8")
    ctx_go = ProjectAdapter.discover(go_dir)
    assert "go" in ctx_go.project_types
    assert ctx_go.test_commands == [["go", "test", "./..."]]
    assert ctx_go.build_commands == [["go", "build", "./..."]]
    assert ctx_go.lint_commands == [["go", "vet", "./..."]]

    # Node
    node_dir = tmp_path / "node_proj"
    node_dir.mkdir()
    (node_dir / "package.json").write_text('{"name": "test"}\n', encoding="utf-8")
    ctx_node = ProjectAdapter.discover(node_dir)
    assert "javascript" in ctx_node.project_types
    assert ctx_node.test_commands == [["npm", "test"]]

    # Rust
    rust_dir = tmp_path / "rust_proj"
    rust_dir.mkdir()
    (rust_dir / "Cargo.toml").write_text('[package]\nname = "test"\n', encoding="utf-8")
    ctx_rust = ProjectAdapter.discover(rust_dir)
    assert "rust" in ctx_rust.project_types
    assert ctx_rust.test_commands == [["cargo", "test"]]
    assert ctx_rust.build_commands == [["cargo", "build"]]


def test_howlplane_root_discovery_regression():
    repo_root = Path(__file__).resolve().parents[1]
    ctx = ProjectAdapter.discover(repo_root)

    assert "go" in ctx.project_types
    assert "python" in ctx.project_types
    assert "howlframe" in ctx.project_types
    assert ctx.has_agents_md is True
    assert ctx.test_commands == [["make", "test"]]
    assert ctx.build_commands == [["make", "build"]]
    assert ctx.lint_commands == [["make", "lint"]]
    assert "integrations/howlframe/project_context_audit.howl" in ctx.metadata["howl_sources"]
    assert "cli_app" in ctx.metadata["apparent_targets"]


# --- Formatting command discovery (HOWLFRAM-SLOPFIX-07) -----------------------
#
# A Claude implementation attempt was denied `gofmt -l` and `go fmt` because no
# formatting command was discoverable for any language, so the bounded allow
# list could never contain one.


def test_go_project_discovers_both_formatter_forms(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)

    assert ["go", "fmt", "./..."] in ctx.format_commands
    assert ["gofmt", "-l", "."] in ctx.format_commands


def test_format_discovery_survives_fully_specified_command_set(tmp_path: Path):
    """Formatting is discovered even when test/build/lint are all explicit.

    The stack heuristics only run when one of those three is still missing. A
    formatter discovered inside that block would be silently unavailable to
    exactly the mature projects most likely to gate on formatting in CI.
    """
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / ".ai-project.toml").write_text(
        '[commands]\ntest = ["go", "test", "./..."]\n'
        'build = ["go", "build", "./..."]\n'
        'lint = ["go", "vet", "./..."]\n',
        encoding="utf-8",
    )

    ctx = ProjectAdapter.discover(tmp_path)

    assert ctx.test_commands and ctx.build_commands and ctx.lint_commands
    assert ctx.format_commands, "formatter must be discovered outside the heuristics gate"


def test_makefile_format_target_wins_over_language_default(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("fmt:\n\tgo fmt ./...\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)

    assert ctx.format_commands == [["make", "fmt"]]


def test_python_formatter_only_granted_when_configured(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.black]\nline-length = 100\n", encoding="utf-8"
    )

    ctx = ProjectAdapter.discover(tmp_path)

    assert ["black", "."] in ctx.format_commands


def test_project_without_known_formatter_gets_none(tmp_path: Path):
    """No language gets a formatter it has no toolchain for."""
    (tmp_path / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)

    assert ctx.format_commands == []


def test_formatting_never_enters_the_verification_plan(tmp_path: Path):
    """Formatting is implementation work, not a deterministic gate.

    Adding it to the plan would change what every change is verified against.
    """
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    plan = ProjectAdapter.create_verification_plan(ctx, "TASK-FMT-01")

    assert ctx.format_commands
    rendered = [
        step.command if isinstance(step.command, str) else " ".join(step.command)
        for step in plan.steps
    ]
    assert not any("fmt" in command for command in rendered), rendered
