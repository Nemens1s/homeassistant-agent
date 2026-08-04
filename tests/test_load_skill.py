"""load_skill exposes the available skill names as a Literal enum built from the
skill files themselves — so the model can't invent a name, and adding a skill file
updates the schema with no code change."""

import pytest

from app.config import Settings
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.load_skill",))
    yield
    registry._reset_for_tests()


def _skills_dir(tmp_path, names):
    for n in names:
        (tmp_path / f"{n}.md").write_text(f"---\nname: {n}\ndescription: d\n---\nbody")
    return tmp_path


def _allowed_values(name_prop: dict) -> list | None:
    """Allowed values for a field, regardless of JSON-schema shape: pydantic renders
    a single-value Literal as `const` and a multi-value one as `enum`."""
    if "enum" in name_prop:
        return name_prop["enum"]
    if "const" in name_prop:
        return [name_prop["const"]]
    return None


def test_dynamic_params_enum_reflects_skill_files(tmp_path):
    _skills_dir(tmp_path, ["alpha_skill", "beta_skill"])
    defn = registry.get("load_skill")
    model = defn.dynamic_params(ToolContext(settings=Settings(_env_file=None), rest=None, skills_dir=tmp_path))
    schema = model.model_json_schema()
    assert _allowed_values(schema["properties"]["name"]) == ["alpha_skill", "beta_skill"]


def test_no_skills_falls_back_to_free_string(tmp_path):
    defn = registry.get("load_skill")
    model = defn.dynamic_params(ToolContext(settings=Settings(_env_file=None), rest=None, skills_dir=tmp_path))
    # empty dir → plain string field, no enum (never a broken empty Literal)
    assert "enum" not in model.model_json_schema()["properties"]["name"]


def test_adapter_uses_dynamic_schema_for_load_skill(tmp_path):
    _skills_dir(tmp_path, ["only_skill"])
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None, skills_dir=tmp_path)
    tool = next(t for t in build_tools(ctx, max_tier=1) if t.name == "load_skill")
    allowed = _allowed_values(tool.args_schema.model_json_schema()["properties"]["name"])
    assert allowed == ["only_skill"]


async def test_invalid_skill_name_is_rejected_by_schema(tmp_path):
    _skills_dir(tmp_path, ["only_skill"])
    defn = registry.get("load_skill")
    model = defn.dynamic_params(ToolContext(settings=Settings(_env_file=None), rest=None, skills_dir=tmp_path))
    with pytest.raises(Exception):  # ValidationError — not a real skill name
        model(name="made_up")
