"""
test_howlframe_app_skill.py

Deterministic tests verifying Improvement #54:
1. General HowlFrame application development skill exists with valid frontmatter.
2. Skill is indexed in skills manifest and .agents/skills.json.
3. ProjectAdapter automatically assigns 'howlframe-app-development' to discovered HowlFrame projects.
4. ReviewRunner exposes HowlFrame application rules in reviewer briefs.
5. GovernedTaskOrchestrator provides HowlFrame guidance in implementation prompts.
6. Real HowlNotes repository discovers 'howlframe-app-development' skill.
"""

import json
from pathlib import Path
import yaml

from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from howlplane.control_plane.project_adapter import ProjectAdapter
from howlplane.control_plane.review_runner import ReviewRunner
from howlplane.control_plane.reviewers import get_reviewer_role
from howlplane.control_plane.task_spec import TaskSpec


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_howlframe_app_skill_file_and_frontmatter():
    """Proves that .agents/skills/howlframe-app-development/SKILL.md exists and has valid metadata."""
    skill_file = REPO_ROOT / ".agents" / "skills" / "howlframe-app-development" / "SKILL.md"
    assert skill_file.is_file(), f"Skill file missing at {skill_file}"

    content = skill_file.read_text(encoding="utf-8")
    assert content.startswith("---")
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Frontmatter not properly bounded by ---"

    meta = yaml.safe_load(parts[1])
    assert meta["name"] == "howlframe-app-development"
    assert meta["tier"] == 2
    assert "HowlFrame" in meta["description"]
    assert "(http_server" in content
    assert "(web_app" in content
    assert "(cli_app" in content
    assert "(store_open" in content
    assert "scripts/build.sh" in content
    assert "scripts/test.sh" in content


def test_skills_manifest_and_index_include_howlframe_app_dev():
    """Proves skills manifest in AGENTS.md and .agents/skills.json include the new skill."""
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "howlframe-app-development" in agents_md
    assert ".agents/skills/howlframe-app-development/SKILL.md" in agents_md

    skills_json_path = REPO_ROOT / ".agents" / "skills.json"
    if skills_json_path.is_file():
        data = json.loads(skills_json_path.read_text(encoding="utf-8"))
        skills_list = data.get("skills", []) if isinstance(data, dict) else data
        skill_names = [s.get("name") for s in skills_list]
        assert "howlframe-app-development" in skill_names


def test_project_adapter_auto_selects_howlframe_skill(tmp_path: Path):
    """Proves ProjectAdapter auto-assigns howlframe-app-development skill on .howl detection."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "backend.howl").write_text('(http_server 8080 (print "ok"))\n', encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    assert "howlframe" in ctx.project_types
    assert "howlframe-app-development" in ctx.skills


def test_review_runner_context_exposure():
    """Proves ReviewRunner passes HowlFrame application rules in context to reviewer briefs."""
    task = TaskSpec(
        task_id="TASK-HF-01",
        repository="howlnotes",
        objective="Add tag filtering to notes backend",
        required_skills=["howlframe-app-development"],
    )

    captured_briefs = []

    def mock_reviewer_fn(role_id, diff, t):
        role = get_reviewer_role(role_id)
        # Capture the brief that was rendered
        skill_context_parts = []
        if t.required_skills:
            skill_context_parts.append(f"Required Skills: {', '.join(t.required_skills)}")
        if "howlframe-app-development" in (t.required_skills or []):
            skill_context_parts.append("HowlFrame Application Rules & Invariants")
        brief = role.render_brief(task=t, diff_content=diff, context="\n\n".join(skill_context_parts))
        captured_briefs.append((role_id, brief))
        return "findings: []"

    ReviewRunner.execute_review_cycle(
        task=task,
        diff_content="+ (route '/tags' (lambda (req) (res_json 200 (list))))",
        reviewer_roles=["correctness-reviewer"],
        cwd=REPO_ROOT,
        custom_reviewer_fn=mock_reviewer_fn,
    )

    assert len(captured_briefs) == 1
    role_id, brief = captured_briefs[0]
    assert "howlframe-app-development" in brief
    assert "HowlFrame Application Rules & Invariants" in brief


def test_orchestrator_implementation_prompt_guidance():
    """Proves GovernedTaskOrchestrator includes HowlFrame application guidance in implementation prompt."""
    orch = GovernedTaskOrchestrator(REPO_ROOT, config=OrchestrationConfig(record_evidence=False))
    task = TaskSpec(
        task_id="TASK-HF-02",
        repository="howlnotes",
        objective="Implement note deletion route",
        required_skills=["howlframe-app-development"],
    )

    prompt = orch._build_implementation_prompt(task)
    assert "HowlFrame Application Guidance" in prompt
    assert "bash scripts/build.sh" in prompt
    assert "bash scripts/test.sh" in prompt
    assert "capability gates" in prompt


def test_real_howlnotes_discovery():
    """Proves discovery against real HowlNotes repo (if present) includes howlframe-app-development."""
    howlnotes_path = Path("/run/media/system/tallgeese/dev/howlnotes")
    if howlnotes_path.is_dir():
        ctx = ProjectAdapter.discover(howlnotes_path)
        assert "howlframe" in ctx.project_types
        assert "howlframe-app-development" in ctx.skills
        assert ctx.build_commands == [["bash", "scripts/build.sh"]]
        assert ctx.test_commands == [["bash", "scripts/test.sh"]]
