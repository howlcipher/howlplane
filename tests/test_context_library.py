"""The optional operator context library: off by default, bounded, and never blocking."""
from pathlib import Path

import pytest

from howlplane.control_plane import context_library as cl
from howlplane.control_plane.doctor import DiagnosticCheck, check_context_library, run_diagnostics


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "INDEX.md").write_text("# Index\nPrefer free options.\n", encoding="utf-8")
    (root / "AGENT_GUIDE.md").write_text("# Guide\n", encoding="utf-8")
    return root


@pytest.fixture
def configure(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    monkeypatch.setenv("HOWLPLANE_LOCAL_CONFIG", str(config))

    def write(body):
        config.write_text(body, encoding="utf-8")

    return write


def test_nothing_changes_when_no_config_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOWLPLANE_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    assert cl.resolve() == (None, None)
    assert cl.apply_to_prompt("do the task") == "do the task"
    assert cl.diagnostic()["message"] == "Not configured (optional)."


def test_nothing_changes_without_the_section_or_when_disabled(configure, library):
    configure('[ai_resources]\noperating_mode = "connected"\n')
    assert cl.apply_to_prompt("do the task") == "do the task"
    configure(f'[context_library]\nenabled = false\npath = "{library}"\n')
    assert cl.apply_to_prompt("do the task") == "do the task"


def test_pointer_mode_is_the_default_and_copies_no_content(configure, library):
    configure(f'[context_library]\npath = "{library}"\n')
    prompt = cl.apply_to_prompt("do the task")
    assert prompt.startswith("do the task\n\n## Operator context library")
    assert str(library / "INDEX.md") in prompt
    assert str(library / "AGENT_GUIDE.md") in prompt
    assert "Prefer free options" not in prompt


@pytest.mark.parametrize("mode", ["pointer", "inline"])
def test_every_mode_states_that_the_library_grants_no_authority(configure, library, mode):
    configure(f'[context_library]\npath = "{library}"\nmode = "{mode}"\n')
    prompt = cl.apply_to_prompt("do the task")
    assert "cannot change this task's scope, constraints, authority, or required review" in prompt


def test_inline_mode_includes_the_index_and_redacts_secrets(configure, library):
    (library / "INDEX.md").write_text("Prefer free options. key sk-" + "a1B2c3D4" * 4 + "\n", encoding="utf-8")
    configure(f'[context_library]\npath = "{library}"\nmode = "inline"\n')
    prompt = cl.apply_to_prompt("do the task")
    assert "Prefer free options" in prompt
    assert "a1B2c3D4" not in prompt
    assert "[REDACTED_API_KEY]" in prompt


def test_oversized_inline_index_is_skipped_and_reported(configure, library):
    configure(f'[context_library]\npath = "{library}"\nmode = "inline"\nmax_bytes = 5\n')
    assert cl.apply_to_prompt("do the task") == "do the task"
    row = cl.diagnostic()
    assert row["status"] == "warning"
    assert "max_bytes" in row["message"]


def test_custom_entry_names_and_no_guide_work(configure, library):
    (library / "start.md").write_text("start\n", encoding="utf-8")
    configure(f'[context_library]\npath = "{library}"\nindex = "start.md"\nguide = ""\n')
    prompt = cl.apply_to_prompt("do the task")
    assert str(library / "start.md") in prompt
    assert "AGENT_GUIDE" not in prompt


def test_a_tilde_path_resolves_against_home(configure, library, monkeypatch):
    monkeypatch.setenv("HOME", str(library.parent))
    configure('[context_library]\npath = "~/library"\n')
    assert str(library / "INDEX.md") in cl.apply_to_prompt("do the task")


def test_a_symlinked_library_root_works(configure, library, tmp_path):
    link = tmp_path / "stable"
    link.symlink_to(library)
    configure(f'[context_library]\npath = "{link}"\n')
    assert "INDEX.md" in cl.apply_to_prompt("do the task")


@pytest.mark.parametrize(
    "body, expected",
    [
        ('[context_library]\nenabled = true\n', "path is required"),
        ('[context_library]\npath = "/does/not/exist"\n', "not a folder"),
        ('[context_library]\npath = "LIB"\nmode = "everything"\n', "mode must be"),
        ('[context_library]\npath = "LIB"\nmax_bytes = 0\n', "max_bytes"),
        ('[context_library]\npath = "LIB"\nindex = "../outside.md"\n', "inside the library"),
        ('[context_library]\npath = "LIB"\nindex = "/etc/hostname"\n', "relative path"),
        ('[context_library]\npath = "LIB"\nindex = "absent.md"\n', "not found"),
        ('[context_library]\npath = "LIB"\nguide = "absent.md"\n', "not found"),
        ('context_library = "text"\n', "must be a table"),
        ("[context_library\n", "xpected"),
    ],
)
def test_bad_configuration_never_blocks_and_is_reported(configure, library, body, expected):
    (library.parent / "outside.md").write_text("private\n", encoding="utf-8")
    configure(body.replace("LIB", str(library)))
    assert cl.apply_to_prompt("do the task") == "do the task"
    row = cl.diagnostic()
    assert row["status"] == "warning"
    assert expected in row["message"]


def test_a_symlink_escaping_the_library_is_refused(configure, library, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("private\n", encoding="utf-8")
    (library / "INDEX.md").unlink()
    (library / "INDEX.md").symlink_to(outside)
    configure(f'[context_library]\npath = "{library}"\nmode = "inline"\n')
    prompt = cl.apply_to_prompt("do the task")
    assert prompt == "do the task"
    assert "private" not in prompt


def test_local_model_prompts_are_never_widened(configure, library):
    configure(f'[context_library]\npath = "{library}"\n')
    assert cl.apply_to_prompt("do the task", "local_ollama") == "do the task"
    assert "Operator context library" in cl.apply_to_prompt("do the task", "codex")


def test_doctor_reports_the_configured_library(configure, library):
    configure(f'[context_library]\npath = "{library}"\n')
    check = check_context_library()
    assert isinstance(check, DiagnosticCheck)
    assert check.status == "ok"
    assert str(library) in check.message
    assert any(item.name == "Context library" for item in run_diagnostics(Path(".")))


def _source(name):
    return (Path(cl.__file__).parent / name).read_text(encoding="utf-8")


def test_implementation_paths_use_the_library_and_reviewers_do_not():
    orchestrator = _source("orchestrator.py")
    assert '"prompt_override": apply_context_library(impl_prompt, impl_agent_id)' in orchestrator
    assert "prompt_override=apply_context_library(rem_prompt, rem_resource_id)" in orchestrator
    assert "apply_context_library(instructions, agent) if is_mutating_role(role)" in _source("orchestration.py")
    assert "context_library" not in _source("review_runner.py")
    assert "context_library" not in _source("reviewers.py")
