from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.skills import list_skills, read_skill
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    # Static fallback (direct handler calls, registry default). The schema the LLM
    # actually sees is built by build_params_model() from the skill files.
    name: str = Field(description="Skill name exactly as listed in the system prompt")


def build_params_model(skill_names: list[str]) -> type[BaseModel]:
    """Params model whose `name` is a Literal enum of the real skill names, so the
    LLM picks from a menu and can't invent a name. Empty dir → free string."""
    name_type = Literal[tuple(skill_names)] if skill_names else str  # type: ignore[valid-type]
    return create_model(
        "LoadSkillParams",
        name=(name_type, Field(description="Skill playbook to load — pick exactly one.")),
    )


def _params_from_ctx(ctx) -> type[BaseModel]:
    names = []
    for m in list_skills(ctx.skills_dir):
        names.append(m.name)
    return build_params_model(names)


async def handler(params: Params, ctx) -> ToolResult:
    body = read_skill(ctx.skills_dir, params.name)
    if body is None:
        available = []
        for m in list_skills(ctx.skills_dir):
            available.append(m.name)
        return ToolResult.error(
            "skill_not_found", f"No skill {params.name!r}.", data={"available": available}
        )
    return ToolResult.ok({"skill": params.name, "content": body})


register(
    ToolDefinition(
        name="load_skill",
        description="Load a troubleshooting playbook by exact name. Use ONLY when diagnosing a problem (automation didn't fire, device stopped responding). Use the exact name from the enum — do not guess; if the enum is empty, list the available playbooks first.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
        dynamic_params=_params_from_ctx,
    )
)
