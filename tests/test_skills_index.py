import json
import os

import pytest

from howlplane.control_plane.skill_router import SkillRouter
from howlplane.control_plane.config_loader import default_loader

REPO_ROOT = default_loader.get_repo_root()
INDEX_PATH = os.path.join(REPO_ROOT, ".agents", "skills.json")


@pytest.fixture(scope="module")
def index():
    with open(INDEX_PATH, "r", encoding="utf8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def live_skills():
    return SkillRouter().skills


def test_index_exists_and_versioned(index):
    assert index["version"] == 1
    assert isinstance(index["skills"], list)
    assert len(index["skills"]) > 0


def test_index_matches_skill_frontmatter(index, live_skills):
    """
    The committed skills.json must match the SKILL.md frontmatter on disk.
    If this fails, rerun: python scripts/generate_skills_manifest.py
    """
    indexed = {entry["name"]: entry for entry in index["skills"]}
    live = {skill.name: skill for skill in live_skills}

    assert set(indexed) == set(live), (
        "skills.json is out of sync with .agents/skills/; "
        "rerun scripts/generate_skills_manifest.py"
    )
    for name, skill in live.items():
        entry = indexed[name]
        assert entry["description"] == skill.description, name
        assert entry["triggers"] == skill.triggers, name
        assert entry["path"] == os.path.relpath(skill.path, REPO_ROOT), name


def test_index_paths_resolve(index):
    for entry in index["skills"]:
        assert os.path.isfile(os.path.join(REPO_ROOT, entry["path"])), entry["path"]
