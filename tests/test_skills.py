from pathlib import Path

import pytest

from app.config import Settings
from app.skills import list_skills, read_skill
from app.tools import registry
from app.tools.context import ToolContext

SKILL = """---
name: test_skill
description: A test playbook
---
# Steps
Do the thing.
"""


@pytest.fixture
def skills_dir(tmp_path):
    (tmp_path / "test_skill.md").write_text(SKILL)
    return tmp_path


def test_list_skills(skills_dir):
    metas = list_skills(skills_dir)
    assert len(metas) == 1
    assert metas[0].name == "test_skill"
    assert metas[0].description == "A test playbook"


def test_read_skill(skills_dir):
    body = read_skill(skills_dir, "test_skill")
    assert body.startswith("# Steps")


def test_read_skill_missing(skills_dir):
    assert read_skill(skills_dir, "nope") is None


def test_frontmatter_survives_horizontal_rule_in_body(tmp_path):
    (tmp_path / "hr.md").write_text(
        "---\nname: hr_skill\ndescription: has a rule\n---\n# Top\n\n---\n\nBelow the rule.\n"
    )
    metas = list_skills(tmp_path)
    assert metas[0].name == "hr_skill"
    assert metas[0].description == "has a rule"
    body = read_skill(tmp_path, "hr_skill")
    assert "Below the rule." in body


def test_seed_skill_is_valid():
    real_dir = Path(__file__).resolve().parent.parent / "app" / "skills"
    metas = list_skills(real_dir)
    names = [m.name for m in metas]
    assert "diagnosing_automations" in names
    assert all(m.description for m in metas)


async def test_load_skill_tool(skills_dir):
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.load_skill",))
    defn = registry.get("load_skill")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None, ws=None, skills_dir=skills_dir)

    ok = await defn.handler(defn.params_model(name="test_skill"), ctx)
    assert ok.status == "ok"
    assert "Do the thing." in ok.data["content"]

    missing = await defn.handler(defn.params_model(name="nope"), ctx)
    assert missing.status == "error"
    assert missing.error_code == "skill_not_found"
    assert missing.data["available"] == ["test_skill"]
    registry._reset_for_tests()


def test_no_frontmatter_falls_back_to_stem(tmp_path):
    (tmp_path / "bare.md").write_text("# Just a body\n")
    metas = list_skills(tmp_path)
    assert metas[0].name == "bare"
    assert metas[0].description == ""
    assert read_skill(tmp_path, "bare").startswith("# Just a body")


def test_seed_dir_resolved_relative_to_repo():
    # replaces the cwd-dependent Path("app/skills") in test_seed_skill_is_valid
    real_dir = Path(__file__).resolve().parent.parent / "app" / "skills"
    assert list_skills(real_dir)
