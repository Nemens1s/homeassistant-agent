from pydantic import BaseModel

from app.skills import list_skills as _list_skills
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    metas = _list_skills(ctx.skills_dir)
    return ToolResult.ok({
        "skills": [{"name": m.name, "description": m.description} for m in metas]
    })


register(
    ToolDefinition(
        name="list_skills",
        description="List available troubleshooting playbook names and descriptions. Call this when you need to diagnose a problem but are unsure of the exact playbook name to load.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
