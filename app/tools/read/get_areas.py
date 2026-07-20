from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area list needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")
    return ToolResult.ok({"areas": [a["name"] for a in areas]})


register(
    ToolDefinition(
        name="get_areas",
        description="List all area/room names in the home. Use before calling get_area_devices when you need to know what rooms exist.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
