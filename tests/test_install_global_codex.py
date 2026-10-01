import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_global.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf8")
    path.chmod(0o755)


def test_posix_installer_registers_codex_globally(tmp_path):
    home = tmp_path / "home"
    codex_home = tmp_path / "custom_codex"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    codex_home.mkdir()
    fake_bin.mkdir()

    _write_executable(fake_bin / "pip3", "#!/usr/bin/env bash\nexit 0\n")
    codex_agents = codex_home / "AGENTS.md"
    codex_agents.write_text("# Existing personal guidance\n", encoding="utf8")
    existing_skill = home / ".agents" / "skills" / "software_development"
    existing_skill.mkdir(parents=True)
    (existing_skill / "owner.txt").write_text("user", encoding="utf8")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    for _ in range(2):
        subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    installed = codex_agents.read_text(encoding="utf8")
    canonical = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf8")
    assert installed.startswith("# Existing personal guidance")
    assert installed.count("<!-- ai_knowledge_library:start -->") == 1
    assert installed.count("<!-- ai_knowledge_library:end -->") == 1
    assert canonical in installed

    user_skills = home / ".agents" / "skills"
    assert not existing_skill.is_symlink()
    assert (existing_skill / "owner.txt").read_text(encoding="utf8") == "user"
    command_link = user_skills / "work_next_item"
    assert command_link.is_symlink()
    assert (command_link / "SKILL.md").is_file()


def test_posix_installer_aborts_on_unterminated_marker_and_links_launcher(tmp_path):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    fake_bin = tmp_path / "bin"
    for d in (home, codex_home, fake_bin):
        d.mkdir()
    _write_executable(fake_bin / "pip3", "#!/usr/bin/env bash\nexit 0\n")
    broken = "keep me\n<!-- ai_knowledge_library:start -->\nuser text after\n"
    (codex_home / "AGENTS.md").write_text(broken, encoding="utf8")

    env = os.environ.copy()
    env.update(HOME=str(home), CODEX_HOME=str(codex_home))
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT)], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "without a matching" in result.stderr
    assert (codex_home / "AGENTS.md").read_text(encoding="utf8") == broken
    assert (home / ".local" / "bin" / "howlplane").is_symlink()
