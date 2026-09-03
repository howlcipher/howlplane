"""Behavioral tests for the conservative changed-code test selector."""

from scripts.select_relevant_tests import (
    Selection,
    changed_files_from_name_status,
    module_names,
    print_selection,
    select_tests,
    select_tests_with_reasons,
)

TEST_SOURCES = {
    "tests/test_provider_preflight.py": "from src.core.provider_preflight import run_preflight",
    "tests/test_config.py": "import yaml\nfrom src.core import config",
    "tests/test_authority_profile.py": "from src.control_plane.authority_profile import AuthorityProfile",
    "tests/test_backlog_marathon.py": "from src.control_plane.backlog_source import select_next_item",
    "tests/test_rules_and_prompts.py": "def test_rules():\n    pass",
    "tests/test_skills_index.py": "def test_skills():\n    pass",
    "tests/test_generate_skills_manifest.py": "def test_manifest():\n    pass",
    "tests/test_select_relevant_tests.py": "from scripts import select_relevant_tests",
    "tests/test_tools.py": "def test_helpers():\n    pass",
}


def test_module_names_src_layout():
    names = module_names("src/core/provider_preflight.py")
    assert "provider_preflight" in names
    assert "src.core.provider_preflight" in names
    assert "core.provider_preflight" in names


def test_module_names_package_init_uses_package():
    names = module_names("src/core/__init__.py")
    assert "core" in names
    assert "__init__" not in names


def test_changed_source_selects_referencing_tests():
    selected, uncovered, trigger = select_tests(
        ["src/core/provider_preflight.py"], TEST_SOURCES
    )
    assert selected == ["tests/test_provider_preflight.py"]
    assert uncovered == []
    assert trigger is None


def test_direct_source_to_test_mapping_is_selected_even_without_an_import():
    selected, uncovered, trigger = select_tests(
        ["src/control_plane/authority_profile.py"], TEST_SOURCES
    )

    assert selected == ["tests/test_authority_profile.py"]
    assert uncovered == []
    assert trigger is None


def test_multiple_sources_union_direct_and_dependency_mappings():
    selected, uncovered, trigger = select_tests(
        ["src/core/provider_preflight.py", "src/control_plane/backlog_source.py"],
        TEST_SOURCES,
    )

    assert selected == [
        "tests/test_backlog_marathon.py",
        "tests/test_provider_preflight.py",
    ]
    assert uncovered == []
    assert trigger is None


def test_changed_test_file_selects_itself():
    selected, uncovered, trigger = select_tests(
        ["tests/test_config.py"], TEST_SOURCES
    )
    assert selected == ["tests/test_config.py"]
    assert trigger is None


def test_unreferenced_source_is_reported_uncovered():
    selected, uncovered, trigger = select_tests(
        ["src/core/brand_new_module.py"], TEST_SOURCES
    )
    assert selected == []
    assert uncovered == ["src/core/brand_new_module.py"]
    assert trigger == "uncertain impact: src/core/brand_new_module.py"


def test_broad_surface_change_triggers_full_suite():
    for path in (
        "Makefile",
        "pyproject.toml",
        "tests/conftest.py",
        "config/settings.yaml",
        "cmd/installer/main.go",
        "src/control_plane/orchestrator.py",
    ):
        _, _, trigger = select_tests([path], TEST_SOURCES)
        assert trigger == path, path


def test_docs_only_change_selects_nothing():
    selected, uncovered, trigger = select_tests(
        ["documentation/some_doc.md", "README.md"], TEST_SOURCES
    )
    assert selected == []
    assert uncovered == []
    assert trigger is None


def test_full_suite_trigger_prefix_must_be_directory():
    # "configure.py" must not match the "config/" directory trigger.
    selected, uncovered, trigger = select_tests(["configure.py"], TEST_SOURCES)
    assert trigger == "uncertain impact: configure.py"
    assert uncovered == ["configure.py"]


def test_agent_and_skill_changes_run_their_executable_validation_contracts():
    selected, uncovered, trigger = select_tests(
        ["AGENTS.md", ".agents/skills/quality_assurance/SKILL.md"], TEST_SOURCES
    )

    assert selected == [
        "tests/test_generate_skills_manifest.py",
        "tests/test_rules_and_prompts.py",
        "tests/test_skills_index.py",
    ]
    assert uncovered == []
    assert trigger is None


def test_deleted_or_renamed_source_selects_destination_and_source_contracts():
    changed = changed_files_from_name_status(
        "R100\tsrc/core/old_module.py\tsrc/core/new_module.py\nD\tsrc/control_plane/authority_profile.py\n"
    )
    sources = {
        **TEST_SOURCES,
        "tests/test_old_module.py": "from src.core.old_module import old_helper",
        "tests/test_new_module.py": "from src.core.new_module import new_helper",
    }
    selected, uncovered, trigger = select_tests(changed, sources)

    assert "tests/test_old_module.py" in selected
    assert "tests/test_new_module.py" in selected
    assert "tests/test_authority_profile.py" in selected
    assert uncovered == []
    assert trigger is None


def test_name_status_expands_renames_and_deletions_for_conservative_selection():
    assert changed_files_from_name_status(
        "R100\tsrc/core/old.py\tsrc/core/new.py\nD\tsrc/core/deleted.py\n"
    ) == ["src/core/deleted.py", "src/core/new.py", "src/core/old.py"]


def test_no_diff_returns_an_empty_explainable_selection():
    selection = select_tests_with_reasons([], TEST_SOURCES)

    assert selection == Selection(selected={}, uncovered=[], fallback_reason=None)


def test_explainable_selection_records_why_each_test_was_selected():
    selection = select_tests_with_reasons(
        ["src/core/provider_preflight.py", "tests/test_config.py"], TEST_SOURCES
    )

    assert selection.fallback_reason is None
    assert selection.uncovered == []
    assert selection.selected["tests/test_config.py"] == ["changed test file"]
    assert selection.selected["tests/test_provider_preflight.py"] == [
        "direct module mapping for src/core/provider_preflight.py",
        "imports or exercises src.core.provider_preflight"
    ]


def test_unknown_non_documentation_change_falls_back_even_when_other_tests_are_selected():
    selected, uncovered, trigger = select_tests(
        ["src/core/provider_preflight.py", "templates/new_template.jinja"], TEST_SOURCES
    )

    assert selected == ["tests/test_provider_preflight.py"]
    assert uncovered == ["templates/new_template.jinja"]
    assert trigger == "uncertain impact: templates/new_template.jinja"


def test_dry_run_output_explains_changed_files_and_conservative_fallback(capsys):
    print_selection(
        ["pyproject.toml"],
        Selection(selected={}, uncovered=[], fallback_reason="pyproject.toml"),
    )

    output = capsys.readouterr().out
    assert "Changed files:" in output
    assert "pyproject.toml" in output
    assert "Conservative fallback: yes" in output
    assert "Selected tests: tests/" in output


def test_dotted_patch_target_selects_referencing_test():
    sources = {
        "tests/test_consumer.py": '@patch("src.core.mcp_client.SyncMCPClient")\ndef test_mcp():\n    pass',
        "tests/test_mcp_client.py": 'def test_direct():\n    pass',
    }
    selected, uncovered, trigger = select_tests(["src/core/mcp_client.py"], sources)
    assert "tests/test_consumer.py" in selected
    assert "tests/test_mcp_client.py" in selected
    assert trigger is None
    assert uncovered == []


def test_central_module_durable_store_triggers_full_suite():
    _, _, trigger = select_tests(["src/control_plane/durable_store.py"], TEST_SOURCES)
    assert trigger == "src/control_plane/durable_store.py"


def test_generated_coverage_artifacts_are_ignored():
    selected, uncovered, trigger = select_tests(
        [".coverage", "coverage.out", ".coverage.node1"], TEST_SOURCES
    )
    assert selected == []
    assert uncovered == []
    assert trigger is None


def test_structural_trigger_takes_precedence_over_uncertain_impact():
    _, _, trigger = select_tests(
        ["Makefile", "some_unknown_scratch.xyz"], TEST_SOURCES
    )
    assert trigger == "Makefile"
