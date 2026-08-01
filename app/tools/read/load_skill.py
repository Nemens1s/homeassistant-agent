from pydantic import BaseModel, Field

from app.skills import list_skills, read_skill
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    name: str = Field(description="Skill name exactly as listed in the system prompt")


async def handler(params: Params, ctx) -> ToolResult:
    body = read_skill(ctx.skills_dir, params.name)
    if body is None:
        available = [m.name for m in list_skills(ctx.skills_dir)]
        return ToolResult.error(
            "skill_not_found", f"No skill {params.name!r}.", data={"available": available}
        )
    return ToolResult.ok({"skill": params.name, "content": body})


register(
    ToolDefinition(
        name="load_skill",
        description="Load a skill playbook by name. Call this FIRST for any diagnosis or troubleshooting request. Available skill names are listed in the system prompt — pass the exact name from that list.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
