from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

TAIL_LINES = 50


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    text = await ctx.rest.get_error_log()
    lines = text.strip().splitlines()
    return ToolResult.ok({"lines": lines[-TAIL_LINES:], "total_lines": len(lines)})


register(
    ToolDefinition(
        name="get_error_log",
        description="Get the tail of Home Assistant's error log. Use for 'are there any HA errors?', 'is HA healthy?', 'is Home Assistant showing any errors?'. Not for automation events or state history.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
